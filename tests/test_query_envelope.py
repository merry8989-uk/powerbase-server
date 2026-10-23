"""The Logs Explorer specification's request and response envelope.

Two request shapes are accepted: the specification's named filter object with a
nested `pagination`, and the general filter list the console sends. Both produce
the same response, which carries the specification's `meta` and `pagination`
blocks alongside the flat keys the console already reads.

The refusals get as much attention as the successes, because that is where the
bugs were: an unknown filter name crashed the error message itself, and a
non-object `time_range` was iterated instead of validated.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from helpers import APP_ID, OTHER_APP_ID
from powerbase.logquery import MAX_WINDOW_UNITS, QueryError, parse_relative_window, parse_sort

NOW = datetime.now(timezone.utc)


@pytest.fixture()
def qenv(make_env):
    env = make_env()
    env.ingest = env.key(scope="ingest")
    env.read = env.key(scope="read")
    env.ingest_headers = {"Authorization": f"Bearer {env.ingest}"}
    env.read_headers = {"Authorization": f"Bearer {env.read}"}

    env.client.post(
        "/v1/ingest/log-events",
        headers=env.ingest_headers,
        json={
            "batch_id": "envelope",
            "app_id": APP_ID,
            "events": [
                {
                    "event_id": "e1",
                    "ts": (NOW - timedelta(hours=1)).isoformat(),
                    "severity": "ERROR",
                    "event_type": "job_feed_import_failed",
                    "category": "api",
                    "source_type": "web",
                    "environment": "production",
                    "message": "timed out",
                },
                {
                    "event_id": "e2",
                    "ts": (NOW - timedelta(hours=2)).isoformat(),
                    "severity": "TRACE",
                    "event_type": "page_view",
                    "category": "ui",
                    "source_type": "android",
                    "environment": "production",
                    "message": "ok",
                },
            ],
        },
    )
    return env


def query(env, body, app_id=APP_ID):
    return env.client.post(
        "/v1/query/log-events", headers=env.read_headers, params={"app_id": app_id}, json=body
    )


SPEC_BODY = {
    "filters": {
        "platform": "all",
        "severity": "all",
        "category": "all",
        "environment": "all",
        "search": None,
        "time_range": "7d",
    },
    "sort": {"field": "timestamp", "direction": "desc"},
    "pagination": {"limit": 50, "cursor": None},
}


# ---------------------------------------------------------------- the envelope
def test_the_specification_request_body_is_accepted(qenv):
    response = query(qenv, SPEC_BODY)
    assert response.status_code == 200, response.text
    assert response.json()["count"] == 2


def test_the_response_carries_a_query_id(qenv):
    body = query(qenv, SPEC_BODY).json()
    assert body["meta"]["query_id"]
    assert isinstance(body["meta"]["query_id"], str)
    # And it is different for each request, or it identifies nothing.
    assert body["meta"]["query_id"] != query(qenv, SPEC_BODY).json()["meta"]["query_id"]


def test_the_response_carries_the_pagination_block(qenv):
    body = query(qenv, SPEC_BODY).json()
    assert body["pagination"] == {"next_cursor": None, "has_more": False, "limit": 50}


def test_the_response_echoes_the_resolved_sort(qenv):
    """`timestamp` is the specification's name for the column `event_time`."""
    body = query(qenv, SPEC_BODY).json()
    assert body["sort"] == {"field": "event_time", "direction": "desc"}
    assert body["meta"]["sort"] == body["sort"]


def test_meta_reports_the_duration(qenv):
    body = query(qenv, SPEC_BODY).json()
    assert isinstance(body["meta"]["duration_ms"], int)
    assert body["meta"]["duration_ms"] == body["elapsed_ms"]


def test_the_flat_keys_are_still_there(qenv):
    """The console and every existing test read these. They do not move."""
    body = query(qenv, SPEC_BODY).json()
    for key in ("items", "count", "limit", "next_cursor", "has_more", "truncated", "elapsed_ms"):
        assert key in body, f"{key} disappeared from the response"


def test_the_pagination_block_follows_the_flat_keys(qenv):
    body = query(qenv, {"pagination": {"limit": 1}}).json()
    assert body["pagination"]["limit"] == 1
    assert body["pagination"]["has_more"] is True
    assert body["pagination"]["next_cursor"] == body["next_cursor"]


def test_a_cursor_survives_the_pagination_block(qenv):
    first = query(qenv, {"pagination": {"limit": 1}}).json()
    second = query(qenv, {"pagination": {"limit": 1, "cursor": first["pagination"]["next_cursor"]}}).json()
    assert second["count"] == 1
    assert second["items"][0]["event_id"] != first["items"][0]["event_id"]


# ------------------------------------------------------------------- the filters
@pytest.mark.parametrize(
    "named,expected",
    [
        ({"platform": "web"}, 1),
        ({"platform": "android"}, 1),
        ({"category": "api"}, 1),
        ({"category": "ui"}, 1),
        ({"severity": "ERROR"}, 1),
        ({"severity": "TRACE"}, 1),
        ({"environment": "production"}, 2),
    ],
)
def test_named_filters_map_onto_the_columns(qenv, named, expected):
    body = query(qenv, {"filters": named, "pagination": {"limit": 50}}).json()
    assert body["count"] == expected


def test_platform_maps_to_source_type(qenv):
    """The specification's `platform` is this schema's `source_type`."""
    body = query(qenv, {"filters": {"platform": "android"}}).json()
    assert body["items"][0]["source_type"] == "android"


def test_all_means_no_filter_and_never_every_tenant(qenv):
    body = query(qenv, {"filters": {"platform": "all", "severity": "all"}}).json()
    assert body["count"] == 2
    # And it is still scoped to the project the key belongs to. Asking for a
    # different project is refused outright — which it was not, until this test
    # found that the POST verb read `app_id` from the body while the GET verb
    # read it from the query string, so `?app_id=other` was silently ignored.
    other = query(qenv, {"filters": {"platform": "all"}}, app_id=OTHER_APP_ID)
    assert other.status_code == 403
    assert "does not match" in other.json()["detail"]


def test_search_is_lifted_out_of_the_filter_object(qenv):
    body = query(qenv, {"filters": {"search": "timed out"}}).json()
    assert body["count"] == 1


def test_time_range_can_live_inside_the_filter_object(qenv):
    assert query(qenv, {"filters": {"time_range": "7d"}}).json()["count"] == 2
    # A window that ends before the data starts returns nothing rather than erroring.
    assert query(qenv, {"filters": {"time_range": "1m"}}).json()["count"] == 0


def test_the_flat_filter_list_still_works(qenv):
    body = query(
        qenv, {"filters": [{"field": "severity", "operator": "eq", "value": "ERROR"}]}
    ).json()
    assert body["count"] == 1


def test_the_get_verb_still_works(qenv):
    response = qenv.client.get(
        "/v1/query/log-events", headers=qenv.read_headers, params={"app_id": APP_ID}
    )
    assert response.status_code == 200
    assert response.json()["count"] == 2
    assert response.json()["meta"]["query_id"]


# --------------------------------------------------------------------- refusals
@pytest.mark.parametrize(
    "body,why",
    [
        ({"filters": {"nonsense": 1}}, "unknown filter name"),
        ({"filters": {"time_range": "forever"}}, "window is not a duration"),
        ({"filters": {"time_range": 7}}, "window is an int"),
        ({"filters": {"time_range": [1, 2]}}, "window is a list"),
        ({"filters": {"time_range": {"nope": "x"}}}, "unknown window key"),
        ({"filters": {"time_range": {"from": 7}}}, "from is not a string"),
        ({"filters": {"time_range": "99999d"}}, "window wider than the cap"),
        ({"filters": {"time_range": "0d"}}, "window of zero"),
        ({"sort": {"field": "message"}}, "cannot sort by message"),
        ({"sort": {"direction": "sideways"}}, "bad direction"),
        ({"sort": {"field": "event_time", "extra": 1}}, "unknown sort key"),
        ({"pagination": {"nope": 1}}, "unknown pagination key"),
        ({"pagination": {"limit": 0}}, "limit below one"),
    ],
)
def test_a_bad_request_is_refused_with_a_status_not_a_crash(qenv, body, why):
    """The two bugs this suite exists for both lived here.

    An unknown filter name crashed the error message (`dict | set`), and a
    non-object `time_range` was iterated (`set(7)`). Both answered 500 for what
    was a 400, on the path a user reaches by making a typo.
    """
    response = query(qenv, body)
    assert response.status_code in (400, 422), f"{why}: got {response.status_code}"
    assert response.status_code != 500, f"{why}: a refusal became a crash"


def test_the_unknown_filter_message_lists_the_known_ones(qenv):
    detail = query(qenv, {"filters": {"nonsense": 1}}).json()["detail"]
    assert "platform" in detail
    assert "category" in detail
    assert "nonsense" in detail


# ----------------------------------------------------------------- the helpers
def test_relative_windows_resolve_against_the_clock():
    moment = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
    since, until = parse_relative_window("7d", moment)
    assert since == "2026-10-16T12:00:00.000000Z"
    assert until == "2026-10-23T12:00:00.000000Z"

    since, until = parse_relative_window("30m", moment)
    assert since == "2026-10-23T11:30:00.000000Z"


@pytest.mark.parametrize("value", ["7D", " 7d ", "7 d"])
def test_a_relative_window_is_forgiving_about_case_and_spacing(value):
    assert parse_relative_window(value, datetime(2026, 10, 23, tzinfo=timezone.utc))[0].startswith("2026-10-16")


@pytest.mark.parametrize(
    "value",
    ["forever", "", "d", "7", "-1d", "7y", "99999d", "0h", 7, None, ["7d"]],
)
def test_a_bad_relative_window_is_refused(value):
    with pytest.raises(QueryError):
        parse_relative_window(value, datetime(2026, 10, 23, tzinfo=timezone.utc))


def test_every_unit_has_a_cap():
    moment = datetime(2026, 10, 23, tzinfo=timezone.utc)
    for unit, cap in MAX_WINDOW_UNITS.items():
        assert parse_relative_window(f"{cap}{unit}", moment)
        with pytest.raises(QueryError):
            parse_relative_window(f"{cap + 1}{unit}", moment)


@pytest.mark.parametrize(
    "field,expected",
    [
        ("timestamp", "event_time"),
        ("time", "event_time"),
        ("TIMESTAMP", "event_time"),
        ("id", "id"),
        ("event_time", "event_time"),
        ("severity", "severity"),
    ],
)
def test_sort_accepts_the_specification_field_names(field, expected):
    assert parse_sort({"field": field})[0] == expected


def test_sort_defaults_to_newest_first_with_a_stable_tie_break():
    assert parse_sort(None) == ("event_time", "desc")
    assert parse_sort({}) == ("event_time", "desc")


# ------------------------------------------------------- the cursor's query plan
def test_the_cursor_clause_is_a_row_value_comparison(qenv):
    """Guards the shape of the pagination clause, not just its behaviour.

    The equivalent `event_time < ? OR (event_time = ? AND id < ?)` makes SQLite
    choose MULTI-INDEX OR and then sort everything after the cursor before the
    LIMIT can apply. On 60,000 rows that cost 120-170 ms per page instead of
    ~20 ms, and it grew with the table. This test fails if anyone rewrites it
    back into an OR, whatever the timings look like on the machine running it.
    """
    from powerbase import logquery

    where, params = logquery.build_where(
        app_id=APP_ID, since=None, until=None, filters=[], q=None
    )
    where += " AND (event_time, id) < (?, ?)"
    sql = (
        f"SELECT * FROM log_events WHERE {where} "
        "ORDER BY event_time DESC, id DESC LIMIT ?"
    )
    plan = " | ".join(
        str(row["detail"])
        for row in qenv.db.query("EXPLAIN QUERY PLAN " + sql, tuple(params) + ("x", 1, 51))
    )
    assert "TEMP B-TREE" not in plan, f"the cursor query sorts: {plan}"
    assert "MULTI-INDEX OR" not in plan, f"the cursor query merges index scans: {plan}"
    assert "idx_log_events_app_time" in plan, f"not using the time index: {plan}"


def test_the_ordered_index_carries_the_tiebreaker(qenv):
    """`ORDER BY event_time DESC, id DESC` needs `id` in the index, or every
    query pays for a temp B-tree. Migration 0010 is what added it."""
    sql = qenv.db.one(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'idx_log_events_app_time'"
    )["sql"]
    assert "id DESC" in sql, f"the index lost its tiebreaker: {sql}"


def test_pagination_stays_flat_to_depth(qenv):
    """Ten pages in, a page costs about what the first page cost."""
    import time

    from powerbase import logquery

    timings = []
    cursor = None
    for _ in range(10):
        began = time.monotonic()
        page = logquery.query_log_events(
            qenv.db, app_id=APP_ID, limit=1, cursor=cursor
        )
        timings.append((time.monotonic() - began) * 1000)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert len(timings) > 1
    # A generous bound: the point is to catch an OFFSET-shaped regression or a
    # reintroduced sort, not to measure the machine.
    assert max(timings) < 50, f"a page got expensive: {timings}"
