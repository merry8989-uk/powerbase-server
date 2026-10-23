"""The log-events read API and saved searches, over HTTP."""

import pytest

from helpers import ADMIN_PASSWORD, APP_ID, OTHER_APP_ID, log_event, log_events_payload
from powerbase.provision import provision_app

LIST = "/v1/query/log-events"
STATS = "/v1/query/log-events/stats"
SAVED = "/v1/saved-queries"


@pytest.fixture()
def obs(make_env):
    """An instance with an ingest key, a read key and some events."""
    env = make_env(admin_password=ADMIN_PASSWORD)
    ingest = env.key(scope="ingest")
    for index in range(6):
        env.post(
            "/v1/ingest/log-events",
            ingest,
            log_events_payload(
                batch_id=f"obs-{index}",
                events=[
                    log_event(
                        event_id=f"ev-{index}",
                        ts=f"2026-10-22T0{index}:00:00Z",
                        severity=["INFO", "WARN", "ERROR"][index % 3],
                        service_name=["checkout-api", "auth-api"][index % 2],
                        status_code=500 if index % 3 == 2 else 200,
                        duration_ms=100 + index * 50,
                        trace_id="trace-1" if index < 3 else None,
                        error_fingerprint="fp-1" if index % 3 == 2 else None,
                    )
                ],
            ),
        )
    env.read = env.key(scope="read")
    env.admin = {"X-Admin-Password": ADMIN_PASSWORD}
    return env


def get(env, path, **params):
    return env.client.get(path, params=params, headers=env.headers(env.read))


# ------------------------------------------------------------------ listing
def test_listing_returns_the_newest_first(obs):
    response = get(obs, LIST)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 6
    assert [item["event_id"] for item in body["items"]] == [f"ev-{i}" for i in range(5, -1, -1)]
    assert body["has_more"] is False
    assert body["next_cursor"] is None


def test_cursor_pagination_walks_every_row_once(obs):
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        params = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        body = get(obs, LIST, **params).json()
        seen.extend(item["event_id"] for item in body["items"])
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert seen == [f"ev-{i}" for i in range(5, -1, -1)]
    assert len(set(seen)) == 6


def test_named_filters_work(obs):
    body = get(obs, LIST, severity="ERROR").json()
    assert {item["severity"] for item in body["items"]} == {"ERROR"}
    assert get(obs, LIST, min_status=500).json()["count"] == 2
    assert get(obs, LIST, service_name="auth-api").json()["count"] == 3


def test_the_post_form_takes_the_roadmap_query_format(obs):
    body = obs.client.post(
        LIST,
        headers=obs.headers(obs.read),
        json={
            "timeRange": {"from": "2026-10-22T00:00:00Z", "to": "2026-10-23T00:00:00Z"},
            "filters": [
                {"field": "severity", "operator": "eq", "value": "ERROR"},
                {"field": "status_code", "operator": "gte", "value": 500},
            ],
            "sort": {"field": "event_time", "direction": "desc"},
            "limit": 10,
        },
    ).json()
    assert body["count"] == 2


def test_the_post_form_refuses_an_unknown_key(obs):
    response = obs.client.post(LIST, headers=obs.headers(obs.read), json={"raw_sql": "SELECT 1"})
    assert response.status_code == 422


def test_the_post_form_refuses_an_unknown_filter_field(obs):
    response = obs.client.post(
        LIST,
        headers=obs.headers(obs.read),
        json={"filters": [{"field": "password", "operator": "eq", "value": "x"}]},
    )
    assert response.status_code == 400
    assert "unknown field" in response.json()["detail"]


def test_an_unknown_time_range_key_is_refused(obs):
    response = obs.client.post(
        LIST, headers=obs.headers(obs.read), json={"timeRange": {"from": "x", "nope": 1}}
    )
    assert response.status_code == 400


def test_a_malformed_cursor_is_a_400_not_an_empty_page(obs):
    response = get(obs, LIST, cursor="!!!")
    assert response.status_code == 400
    assert "cursor" in response.json()["detail"]


def test_a_range_beyond_the_cap_is_refused(obs):
    response = get(obs, LIST, since="2020-01-01", until="2026-01-01")
    assert response.status_code == 400
    assert "92 days" in response.json()["detail"]


def test_reading_another_project_is_refused(obs):
    response = get(obs, LIST, app_id=OTHER_APP_ID)
    assert response.status_code == 403


def test_an_ingest_key_cannot_read(obs):
    ingest = obs.key(scope="ingest")
    response = obs.client.get(LIST, headers=obs.headers(ingest))
    assert response.status_code == 403


def test_a_request_without_a_key_is_refused(obs):
    assert obs.client.get(LIST).status_code == 401


# -------------------------------------------------------------------- stats
def test_stats_summarise_the_same_filter_set(obs):
    body = get(obs, STATS).json()
    assert body["total"] == 6
    assert body["by_severity"] == {"INFO": 2, "WARN": 2, "ERROR": 2}
    assert body["bucket"] == "hour"
    assert len(body["buckets"]) == 6  # one event per hour


def test_stats_honour_a_filter(obs):
    assert get(obs, STATS, severity="ERROR").json()["total"] == 2


def test_stats_reject_an_unknown_bucket(obs):
    assert get(obs, STATS, bucket="fortnight").status_code == 400


# ------------------------------------------------------------------- detail
def test_event_detail_includes_related_events(obs):
    body = get(obs, f"{LIST}/ev-0").json()
    assert body["event"]["route_template"] == "/orders/{id}"
    assert {item["event_id"] for item in body["related"]} == {"ev-1", "ev-2"}


def test_detail_can_skip_related_events(obs):
    body = get(obs, f"{LIST}/ev-0", related="false").json()
    assert "related" not in body


def test_an_unknown_event_is_a_404(obs):
    assert get(obs, f"{LIST}/nope").status_code == 404


def test_the_stats_route_is_not_swallowed_by_the_detail_route(obs):
    # Registration order matters: `stats` must win over `{event_id}`.
    assert get(obs, STATS).status_code == 200


# ------------------------------------------------------------ saved searches
def test_saved_searches_need_the_admin_password(obs):
    assert obs.client.get(SAVED, params={"app_id": APP_ID}).status_code == 401
    assert (
        obs.client.get(
            SAVED, params={"app_id": APP_ID}, headers=obs.headers(obs.read)
        ).status_code
        == 401
    )


def test_saving_and_listing_a_search(obs):
    created = obs.client.post(
        SAVED,
        headers=obs.admin,
        json={
            "app_id": APP_ID,
            "name": "checkout errors",
            "definition": {
                "filters": [{"field": "severity", "operator": "eq", "value": "ERROR"}],
                "sort": {"field": "duration_ms", "direction": "desc"},
            },
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "checkout errors"
    assert body["definition"]["filters"][0]["value"] == "ERROR"
    assert body["definition"]["sort"]["field"] == "duration_ms"

    listing = obs.client.get(SAVED, params={"app_id": APP_ID}, headers=obs.admin).json()
    assert [item["name"] for item in listing["items"]] == ["checkout errors"]


def test_a_saved_search_cannot_hold_an_invalid_definition(obs):
    response = obs.client.post(
        SAVED,
        headers=obs.admin,
        json={
            "app_id": APP_ID,
            "name": "bad",
            "definition": {"filters": [{"field": "nope", "operator": "eq", "value": 1}]},
        },
    )
    assert response.status_code == 400
    assert obs.client.get(SAVED, params={"app_id": APP_ID}, headers=obs.admin).json()[
        "items"
    ] == []


def test_saving_over_a_name_replaces_it(obs):
    payload = {
        "app_id": APP_ID,
        "name": "same name",
        "definition": {"filters": [{"field": "severity", "operator": "eq", "value": "INFO"}]},
    }
    first = obs.client.post(SAVED, headers=obs.admin, json=payload).json()
    payload["definition"] = {
        "filters": [{"field": "severity", "operator": "eq", "value": "ERROR"}]
    }
    second = obs.client.post(SAVED, headers=obs.admin, json=payload).json()

    assert first["id"] == second["id"]
    assert second["definition"]["filters"][0]["value"] == "ERROR"
    assert len(obs.client.get(SAVED, params={"app_id": APP_ID}, headers=obs.admin).json()["items"]) == 1


def test_a_name_is_required_and_bounded(obs):
    for name in ("", "   ", "x" * 200):
        response = obs.client.post(
            SAVED, headers=obs.admin, json={"app_id": APP_ID, "name": name, "definition": {}}
        )
        assert response.status_code == 400


def test_deleting_a_saved_search(obs):
    created = obs.client.post(
        SAVED, headers=obs.admin, json={"app_id": APP_ID, "name": "temp", "definition": {}}
    ).json()
    assert obs.client.delete(f"{SAVED}/{created['id']}", headers=obs.admin).status_code == 200
    assert obs.client.delete(f"{SAVED}/{created['id']}", headers=obs.admin).status_code == 404
    assert obs.client.get(SAVED, params={"app_id": APP_ID}, headers=obs.admin).json()["items"] == []


def test_saved_searches_are_project_scoped(obs):
    provision_app(obs.db, OTHER_APP_ID, "Other App")
    obs.client.post(
        SAVED,
        headers=obs.admin,
        json={"app_id": OTHER_APP_ID, "name": "other", "definition": {}},
    )
    assert obs.client.get(SAVED, params={"app_id": APP_ID}, headers=obs.admin).json()["items"] == []
