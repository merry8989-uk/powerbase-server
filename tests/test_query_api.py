"""Read API: auth scoping, filters, aggregates and the data browser."""

from helpers import APP_ID, EXPECTED, OTHER_APP_ID


def read(env) -> dict:
    return env.headers(env.key("read"))


# --------------------------------------------------------------------- auth

def test_read_key_required(seeded_env):
    assert seeded_env.client.get("/v1/query/events").status_code == 401


def test_ingest_key_cannot_read(seeded_env):
    r = seeded_env.client.get("/v1/query/events", headers=seeded_env.headers(seeded_env.key("ingest")))
    assert r.status_code == 403


def test_read_key_cannot_read_another_app(other_app):
    r = other_app.client.get(
        f"/v1/query/events?app_id={OTHER_APP_ID}", headers=read(other_app)
    )
    assert r.status_code == 403


def test_read_key_defaults_to_its_own_app(seeded_env):
    body = seeded_env.client.get("/v1/query/events", headers=read(seeded_env)).json()
    assert body["total"] == EXPECTED["events"]


# ------------------------------------------------------------------- events

def test_events_total_and_shape(seeded_env):
    body = seeded_env.client.get("/v1/query/events", headers=read(seeded_env)).json()
    assert body["total"] == EXPECTED["events"]
    assert body["count"] == EXPECTED["events"]
    item = body["items"][0]
    assert {"ts", "name", "screen", "device_id", "session_id", "properties"} <= set(item)
    assert isinstance(item["properties"], dict)  # decoded from JSON, not a raw string


def test_events_filter_by_name(seeded_env):
    body = seeded_env.client.get("/v1/query/events?name=screen_view", headers=read(seeded_env)).json()
    assert body["total"] == EXPECTED["screen_view_events"]
    assert all(i["name"] == "screen_view" for i in body["items"])


def test_events_filter_by_screen(seeded_env):
    body = seeded_env.client.get("/v1/query/events?screen=HomeActivity", headers=read(seeded_env)).json()
    assert body["total"] == EXPECTED["home_screen_events"]
    assert all(i["screen"] == "HomeActivity" for i in body["items"])


def test_events_filter_by_session(seeded_env):
    body = seeded_env.client.get("/v1/query/events?session_id=sess-0", headers=read(seeded_env)).json()
    assert body["total"] == 5


def test_events_pagination(seeded_env):
    first = seeded_env.client.get("/v1/query/events?limit=5&offset=0", headers=read(seeded_env)).json()
    second = seeded_env.client.get("/v1/query/events?limit=5&offset=5", headers=read(seeded_env)).json()
    assert len(first["items"]) == 5 and len(second["items"]) == 5
    assert {i["id"] for i in first["items"]}.isdisjoint({i["id"] for i in second["items"]})


def test_events_order_asc(seeded_env):
    body = seeded_env.client.get("/v1/query/events?order=asc&limit=500", headers=read(seeded_env)).json()
    timestamps = [i["ts"] for i in body["items"]]
    assert timestamps == sorted(timestamps)


def test_events_limit_is_capped(seeded_env):
    assert seeded_env.client.get("/v1/query/events?limit=9999", headers=read(seeded_env)).status_code == 422


# --------------------------------------------------------------------- logs

def test_logs_total(seeded_env):
    assert seeded_env.client.get("/v1/query/logs", headers=read(seeded_env)).json()["total"] == EXPECTED["logs"]


def test_logs_level_filter(seeded_env):
    body = seeded_env.client.get("/v1/query/logs?level=ERROR", headers=read(seeded_env)).json()
    assert body["total"] == EXPECTED["error_logs"]
    assert all(i["level"] == "ERROR" for i in body["items"])


def test_logs_tag_filter(seeded_env):
    body = seeded_env.client.get("/v1/query/logs?tag=CheckoutRepo", headers=read(seeded_env)).json()
    assert body["total"] == EXPECTED["error_logs"]


def test_logs_full_text_search(seeded_env):
    body = seeded_env.client.get("/v1/query/logs?q=payment", headers=read(seeded_env)).json()
    assert body["total"] == EXPECTED["logs"]


def test_logs_fts_still_respects_filters(seeded_env):
    body = seeded_env.client.get(
        "/v1/query/logs?q=payment&level=ERROR", headers=read(seeded_env)
    ).json()
    assert body["total"] == EXPECTED["error_logs"]


def test_logs_search_with_hostile_input_is_safe(seeded_env):
    for hostile in ['"; DROP TABLE logs; --', '"', "AND OR NOT", "%", "a' OR '1'='1"]:
        r = seeded_env.client.get("/v1/query/logs", params={"q": hostile}, headers=read(seeded_env))
        assert r.status_code == 200, hostile
    # The table is still there.
    assert seeded_env.db.scalar("SELECT COUNT(*) FROM logs") == EXPECTED["logs"]


def test_log_message_is_redacted_in_reads(seeded_env):
    body = seeded_env.client.get("/v1/query/logs", headers=read(seeded_env)).json()
    assert all("@" not in item["message"] for item in body["items"])
    assert all("[REDACTED_EMAIL]" in item["message"] for item in body["items"])


def test_log_detail(seeded_env):
    listing = seeded_env.client.get("/v1/query/logs", headers=read(seeded_env)).json()
    log_id = listing["items"][0]["id"]
    detail = seeded_env.client.get(f"/v1/query/logs/{log_id}", headers=read(seeded_env))
    assert detail.status_code == 200
    assert detail.json()["id"] == log_id


def test_log_detail_missing(seeded_env):
    assert seeded_env.client.get("/v1/query/logs/999999", headers=read(seeded_env)).status_code == 404


# ------------------------------------------------------------------ crashes

def test_crash_groups_aggregate(seeded_env):
    body = seeded_env.client.get("/v1/query/crash-groups", headers=read(seeded_env)).json()
    assert body["total"] == 1
    group = body["items"][0]
    assert group["occurrences"] == EXPECTED["crashes"]
    assert group["devices"] == EXPECTED["crashes"]
    assert group["first_seen"] <= group["last_seen"]
    assert len(group["fingerprint"]) == 32


def test_crashes_by_fingerprint(seeded_env):
    groups = seeded_env.client.get("/v1/query/crash-groups", headers=read(seeded_env)).json()["items"]
    fp = groups[0]["fingerprint"]
    body = seeded_env.client.get(f"/v1/query/crashes?fingerprint={fp}", headers=read(seeded_env)).json()
    assert body["total"] == EXPECTED["crashes"]


def test_crash_kind_filter(seeded_env):
    body = seeded_env.client.get("/v1/query/crashes?kind=native", headers=read(seeded_env)).json()
    assert body["total"] == 0


# ------------------------------------------------------------------ metrics

def test_summary(seeded_env):
    body = seeded_env.client.get("/v1/metrics/summary?days=365", headers=read(seeded_env)).json()
    assert body["sessions"] == EXPECTED["sessions"]
    assert body["events"] == EXPECTED["events"]
    assert body["logs"] == EXPECTED["logs"]
    assert body["error_logs"] == EXPECTED["error_logs"]
    assert body["crashes"] == EXPECTED["crashes"]
    assert body["active_devices"] == EXPECTED["devices"]
    assert body["crash_free_rate"] == EXPECTED["crash_free_rate"]
    assert body["avg_session_duration_ms"] == 120_000.0


def test_summary_empty_range(seeded_env):
    body = seeded_env.client.get(
        "/v1/metrics/summary?since=2020-01-01&until=2020-01-02", headers=read(seeded_env)
    ).json()
    assert body["sessions"] == 0
    assert body["crash_free_rate"] == 1.0  # no sessions means nothing crashed


def test_timeseries_daily(seeded_env):
    body = seeded_env.client.get(
        "/v1/metrics/timeseries?metric=events&bucket=day&days=365", headers=read(seeded_env)
    ).json()
    assert body["points"]
    assert sum(p["value"] for p in body["points"]) == EXPECTED["events"]
    assert all(len(p["bucket"]) == 10 for p in body["points"])


def test_timeseries_hourly(seeded_env):
    body = seeded_env.client.get(
        "/v1/metrics/timeseries?metric=events&bucket=hour&days=365", headers=read(seeded_env)
    ).json()
    assert body["points"]
    assert sum(p["value"] for p in body["points"]) == EXPECTED["events"]
    assert all(len(p["bucket"]) == 16 for p in body["points"])


def test_timeseries_unknown_metric(seeded_env):
    r = seeded_env.client.get("/v1/metrics/timeseries?metric=nonsense", headers=read(seeded_env))
    assert r.status_code == 400


def test_top_values(seeded_env):
    body = seeded_env.client.get(
        "/v1/metrics/top?dimension=screen&days=365", headers=read(seeded_env)
    ).json()
    assert len(body["items"]) == EXPECTED["screens"]
    assert sum(i["value"] for i in body["items"]) == EXPECTED["events"]


def test_top_unknown_dimension(seeded_env):
    r = seeded_env.client.get("/v1/metrics/top?dimension=nonsense", headers=read(seeded_env))
    assert r.status_code == 400


# -------------------------------------------------------------- data browser

def test_tables_listing(seeded_env):
    body = seeded_env.client.get("/v1/query/tables", headers=read(seeded_env)).json()
    names = {t["name"] for t in body["items"]}
    assert {"apps", "events", "logs", "crashes", "sessions", "devices"} <= names
    assert not any("fts" in n for n in names)  # internal FTS tables stay hidden
    events_row = next(t for t in body["items"] if t["name"] == "events")
    assert events_row["rows"] == EXPECTED["events"]


def test_table_rows_and_sort(seeded_env):
    body = seeded_env.client.get(
        "/v1/query/tables/events?order_by=ts&order=asc&limit=5", headers=read(seeded_env)
    ).json()
    assert body["total"] == EXPECTED["events"]
    assert len(body["items"]) == 5
    assert body["items"][0]["ts"] <= body["items"][-1]["ts"]
    assert "properties" in body["columns"]


def test_table_rows_bad_order_by_is_ignored(seeded_env):
    r = seeded_env.client.get(
        "/v1/query/tables/events?order_by=; DROP TABLE events; --", headers=read(seeded_env)
    )
    assert r.status_code == 200
    assert seeded_env.db.scalar("SELECT COUNT(*) FROM events") == EXPECTED["events"]


def test_table_unknown(seeded_env):
    assert seeded_env.client.get("/v1/query/tables/nope", headers=read(seeded_env)).status_code == 404


def test_table_reports_foreign_keys(seeded_env):
    body = seeded_env.client.get("/v1/query/tables/events", headers=read(seeded_env)).json()
    referenced = {fk["table"] for fk in body["foreign_keys"]}
    assert {"apps", "devices", "sessions"} <= referenced
