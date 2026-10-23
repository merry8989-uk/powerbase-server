"""The validated query layer.

The contract under test: a client string never reaches SQL as SQL, and a filter
that cannot be honoured is refused rather than silently ignored.
"""

import pytest

from powerbase import logquery, repository
from powerbase.db import QueryTimeout
from powerbase.models import LogEventIn, LogEventsBatch

from helpers import APP_ID, TS


def seed(db, count=6, app_id=APP_ID):
    events = [
        LogEventIn(
            event_id=f"ev-{i}",
            ts=f"2026-10-22T0{i}:00:00Z",
            severity=["INFO", "WARN", "ERROR"][i % 3],
            event_type="http.request",
            service_name=["checkout-api", "auth-api"][i % 2],
            environment="production",
            route_template="/orders/{id}",
            status_code=500 if i % 3 == 2 else 200,
            duration_ms=100 + i * 50,
            trace_id="trace-1" if i < 3 else None,
            error_fingerprint="fp-1" if i % 3 == 2 else None,
            message=f"event {i} for amit@example.com",
        )
        for i in range(count)
    ]
    repository.write_log_events(
        db, app_id, LogEventsBatch(batch_id="seed-obs", app_id=app_id, events=events), redact=True
    )


# ------------------------------------------------------------------ filters
def test_unknown_field_is_refused():
    with pytest.raises(logquery.QueryError):
        logquery.parse_filters([{"field": "secret", "operator": "eq", "value": "x"}])


@pytest.mark.parametrize(
    "spec",
    [
        [{"field": "severity; DROP TABLE log_events", "operator": "eq", "value": "x"}],
        [{"field": "severity) OR 1=1 --", "operator": "eq", "value": "x"}],
        [{"field": "attributes", "operator": "eq", "value": "x"}],
        [{"field": None, "operator": "eq", "value": "x"}],
    ],
)
def test_a_field_name_that_is_not_on_the_allowlist_never_becomes_sql(spec):
    with pytest.raises(logquery.QueryError):
        logquery.parse_filters(spec)


def test_operator_must_suit_the_field_type():
    # `contains` makes no sense on an integer.
    with pytest.raises(logquery.QueryError):
        logquery.parse_filters([{"field": "status_code", "operator": "contains", "value": "5"}])
    # `gt` makes no sense on free text.
    with pytest.raises(logquery.QueryError):
        logquery.parse_filters([{"field": "severity", "operator": "gt", "value": "A"}])
    assert logquery.parse_filters([{"field": "severity", "operator": "in", "value": ["ERROR"]}])


def test_structural_mistakes_are_refused():
    for spec in (
        "not-a-list",
        {"field": "severity"},
        [{"field": "severity", "operator": "eq"}],
        [{"field": "severity", "operator": "eq", "value": "A", "extra": 1}],
        [{"field": "severity", "operator": "eq", "value": "x" * 500}],
    ):
        with pytest.raises(logquery.QueryError):
            logquery.parse_filters(spec)


def test_too_many_filters_are_refused():
    many = [{"field": "severity", "operator": "eq", "value": "INFO"}] * 25
    with pytest.raises(logquery.QueryError):
        logquery.parse_filters(many)


def test_a_non_integer_for_an_integer_field_is_refused():
    with pytest.raises(logquery.QueryError):
        logquery.parse_filters([{"field": "status_code", "operator": "gte", "value": "abc"}])
    # A numeric string is fine, because query strings arrive as strings.
    assert logquery.parse_filters(
        [{"field": "status_code", "operator": "gte", "value": "500"}]
    )[0].value == 500


def test_values_are_bound_never_interpolated():
    filters = logquery.parse_filters(
        [{"field": "severity", "operator": "eq", "value": "ERROR'; DROP TABLE log_events --"}]
    )
    where, params = logquery.build_where(
        app_id=APP_ID, since=None, until=None, filters=filters, q=None
    )
    assert "DROP TABLE" not in where
    assert "?" in where
    assert "DROP TABLE" in params[1]


def test_like_wildcards_in_a_value_are_escaped():
    filters = logquery.parse_filters([{"field": "message", "operator": "contains", "value": "100%"}])
    where, params = logquery.build_where(
        app_id=APP_ID, since=None, until=None, filters=filters, q=None
    )
    assert "ESCAPE" in where
    assert params[1] == "%100\\%%"


# --------------------------------------------------------------------- sort
def test_sort_is_allowlisted():
    assert logquery.parse_sort(None) == ("event_time", "desc")
    assert logquery.parse_sort({"field": "duration_ms", "direction": "asc"}) == (
        "duration_ms",
        "asc",
    )
    for bad in (
        {"field": "id; DROP TABLE log_events"},
        {"field": "attributes"},
        {"field": "event_time", "direction": "sideways"},
        {"field": "event_time", "nope": 1},
        "not-an-object",
    ):
        with pytest.raises(logquery.QueryError):
            logquery.parse_sort(bad)


def test_limit_is_clamped_and_validated():
    assert logquery.clamp_limit(None) == logquery.DEFAULT_LIMIT
    assert logquery.clamp_limit(10_000) == logquery.MAX_LIMIT
    with pytest.raises(logquery.QueryError):
        logquery.clamp_limit(0)
    with pytest.raises(logquery.QueryError):
        logquery.clamp_limit("many")


# ------------------------------------------------------------------ cursors
def test_a_cursor_round_trips():
    cursor = logquery.encode_cursor("2026-10-22T09:00:00.000000Z", 42)
    assert logquery.decode_cursor(cursor) == ("2026-10-22T09:00:00.000000Z", 42)


@pytest.mark.parametrize("bad", ["", "!!!", "bm90LWEtY3Vyc29y", "MjAyNi0xMC0yMnxub3QtaW50"])
def test_a_malformed_cursor_is_refused(bad):
    with pytest.raises(logquery.QueryError):
        logquery.decode_cursor(bad)


# ------------------------------------------------------------------ queries
def test_pagination_has_no_gaps_and_no_repeats(env):
    seed(env.db, count=7)

    seen: list[str] = []
    cursor = None
    for _ in range(10):
        page = logquery.query_log_events(
            env.db, app_id=APP_ID, limit=3, cursor=cursor, direction="desc"
        )
        seen.extend(item["event_id"] for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert len(seen) == 7
    assert len(set(seen)) == 7
    # Newest first, and the sequence is exactly the reverse of insertion order.
    assert seen == [f"ev-{i}" for i in range(6, -1, -1)]


def test_ascending_pagination_walks_the_other_way(env):
    seed(env.db, count=5)
    first = logquery.query_log_events(
        env.db, app_id=APP_ID, limit=2, direction="asc"
    )
    second = logquery.query_log_events(
        env.db, app_id=APP_ID, limit=2, direction="asc", cursor=first.next_cursor
    )
    assert [i["event_id"] for i in first.items] == ["ev-0", "ev-1"]
    assert [i["event_id"] for i in second.items] == ["ev-2", "ev-3"]


def test_filters_actually_filter(env):
    seed(env.db, count=6)
    filters = logquery.parse_filters([{"field": "severity", "operator": "eq", "value": "ERROR"}])
    page = logquery.query_log_events(env.db, app_id=APP_ID, filters=filters)
    assert {item["severity"] for item in page.items} == {"ERROR"}
    assert len(page.items) == 2  # indices 2 and 5


def test_an_integer_range_filter_works(env):
    seed(env.db, count=6)
    filters = logquery.parse_filters(
        [
            {"field": "status_code", "operator": "gte", "value": 500},
            {"field": "duration_ms", "operator": "gte", "value": 200},
        ]
    )
    page = logquery.query_log_events(env.db, app_id=APP_ID, filters=filters)
    # ev-2 (500, exactly 200 ms) and ev-5 (500, 350 ms) both qualify; ev-5 is newer.
    assert [i["event_id"] for i in page.items] == ["ev-5", "ev-2"]


def test_one_project_cannot_see_another(env):
    seed(env.db, count=3)
    page = logquery.query_log_events(env.db, app_id="com.someone.else")
    assert page.items == []


def test_attributes_come_back_decoded(env):
    seed(env.db, count=1)
    page = logquery.query_log_events(env.db, app_id=APP_ID)
    assert isinstance(page.items[0]["attributes"], dict)


def test_free_text_search_only_touches_allowlisted_columns(env):
    seed(env.db, count=6)
    page = logquery.query_log_events(env.db, app_id=APP_ID, q="orders/{id}")
    assert len(page.items) == 6
    assert logquery.query_log_events(env.db, app_id=APP_ID, q="nothing-matches").items == []


def test_stats_agree_with_the_list(env):
    seed(env.db, count=6)
    page = logquery.query_log_events(env.db, app_id=APP_ID)
    stats = logquery.log_event_stats(env.db, app_id=APP_ID)
    assert stats["total"] == len(page.items)
    assert stats["by_severity"] == {"INFO": 2, "WARN": 2, "ERROR": 2}


def test_stats_reject_an_unknown_bucket(env):
    with pytest.raises(logquery.QueryError):
        logquery.log_event_stats(env.db, app_id=APP_ID, bucket="fortnight")


def test_related_events_follow_the_trace_and_the_fingerprint(env):
    seed(env.db, count=6)
    event = logquery.get_log_event(env.db, APP_ID, "ev-0")
    related = logquery.related_events(env.db, APP_ID, event)
    ids = {item["event_id"] for item in related}
    # ev-0 has trace-1 (shared with ev-1, ev-2) but no fingerprint.
    assert ids == {"ev-1", "ev-2"}


def test_an_event_with_no_correlation_has_no_related_rows(env):
    seed(env.db, count=6)
    event = logquery.get_log_event(env.db, APP_ID, "ev-3")
    assert logquery.related_events(env.db, APP_ID, event) == []


# ------------------------------------------------------------------ timeout
def test_a_query_that_overruns_its_budget_is_aborted(env):
    """A real timeout, not a measurement.

    `op_limit=1` makes SQLite call the progress handler after every step, and a
    zero budget means the first call aborts. Without the handler this query
    would simply finish, so a pass here proves the abort path is wired.
    """
    seed(env.db, count=4)
    with pytest.raises(QueryTimeout):
        env.db.query_timed("SELECT * FROM log_events", (), timeout_ms=0, op_limit=1)


def test_the_timeout_handler_does_not_leak_into_later_queries(env):
    seed(env.db, count=2)
    with pytest.raises(QueryTimeout):
        env.db.query_timed("SELECT * FROM log_events", (), timeout_ms=0, op_limit=1)
    # The same connection must be usable again immediately.
    assert env.db.query("SELECT COUNT(*) AS n FROM log_events")[0]["n"] == 2
