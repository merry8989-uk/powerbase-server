"""OTLP/HTTP log ingestion.

The protocol's job is to let an off-the-shelf collector send data without anyone
writing a Powerbase-specific exporter. The two things that must survive that are
the ones tested hardest here: a SQL statement must never be stored as written,
and an exporter's retry must not double-write.
"""

from __future__ import annotations

import json

import pytest

from helpers import APP_ID, OTHER_APP_ID
from powerbase import otlp
from powerbase.provision import provision_app

OTLP = "/v1/otlp/v1/logs"
ALIAS = "/v1/otlp/logs"


def record(**overrides):
    base = {
        "timeUnixNano": "1761200400000000000",
        "severityNumber": 17,
        "severityText": "ERROR",
        "body": {"stringValue": "upstream timeout for amit@example.com"},
        "traceId": "4bf92f3577b34da6a3ce929d0e0e4736",
        "spanId": "00f067aa0ba902b7",
        "attributes": [
            {"key": "http.route", "value": {"stringValue": "/orders/{id}"}},
            {"key": "http.method", "value": {"stringValue": "post"}},
            {"key": "http.status_code", "value": {"intValue": "500"}},
        ],
    }
    base.update(overrides)
    return base


def export(records=None, resource_attributes=None):
    return {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": resource_attributes
                    if resource_attributes is not None
                    else [
                        {"key": "service.name", "value": {"stringValue": "checkout-api"}},
                        {"key": "service.version", "value": {"stringValue": "1.4.0"}},
                        {"key": "deployment.environment", "value": {"stringValue": "production"}},
                        {"key": "host.name", "value": {"stringValue": "ip-10-0-1-4"}},
                    ]
                },
                "scopeLogs": [
                    {
                        "scope": {"name": "opentelemetry.instrumentation.fastapi", "version": "0.48b0"},
                        "logRecords": records if records is not None else [record()],
                    }
                ],
            }
        ]
    }


@pytest.fixture()
def otlp_env(env):
    env.ingest = env.key(scope="ingest")
    env.read = env.key(scope="read")
    return env


def post(env, payload, token=None):
    return env.client.post(
        OTLP,
        headers={"Authorization": f"Bearer {token or env.ingest}"},
        json=payload,
    )


# ------------------------------------------------------------------ mapping
def test_an_export_lands_in_log_events(otlp_env):
    response = post(otlp_env, export())
    assert response.status_code == 200, response.text
    assert response.json() == {}

    row = otlp_env.db.one("SELECT * FROM log_events")
    assert row["source"] == "otel"
    assert row["severity"] == "ERROR"
    assert row["event_type"] == "opentelemetry.instrumentation.fastapi"
    assert row["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert row["span_id"] == "00f067aa0ba902b7"
    assert row["sdk_version"] == "0.48b0"
    assert row["redaction_version"] == 1


def test_resource_attributes_become_columns(otlp_env):
    post(otlp_env, export())
    row = otlp_env.db.one("SELECT * FROM log_events")
    assert row["service_name"] == "checkout-api"
    assert row["app_version"] == "1.4.0"
    assert row["environment"] == "production"
    # One the protocol knows nothing about stays in attributes.
    assert "ip-10-0-1-4" in row["attributes"]
    assert "host.name" in row["attributes"]


def test_record_attributes_become_columns(otlp_env):
    post(otlp_env, export())
    row = otlp_env.db.one("SELECT * FROM log_events")
    assert row["route_template"] == "/orders/{id}"
    assert row["http_method"] == "POST", "the method is normalised to upper case"
    assert row["status_code"] == 500


def test_the_timestamp_is_converted_from_nanoseconds(otlp_env):
    post(otlp_env, export())
    row = otlp_env.db.one("SELECT event_time FROM log_events")
    assert row["event_time"] == "2025-10-23T06:20:00.000000Z"


def test_the_message_is_redacted_like_any_other(otlp_env):
    post(otlp_env, export())
    row = otlp_env.db.one("SELECT message FROM log_events")
    assert "amit@example.com" not in row["message"]
    assert "[REDACTED_EMAIL]" in row["message"]


# ----------------------------------------------------------------- severity
@pytest.mark.parametrize(
    "number,expected",
    [
        # The OTLP severity bands: 1-4 TRACE, 5-8 DEBUG, 9-12 INFO, 13-16 WARN,
        # 17-20 ERROR, 21-24 FATAL. The first band used to be reported as DEBUG,
        # which was wrong before TRACE existed in the vocabulary at all.
        (1, "TRACE"),
        (4, "TRACE"),
        (5, "DEBUG"),
        (8, "DEBUG"),
        (9, "INFO"),
        (13, "WARN"),
        (17, "ERROR"),
        (21, "FATAL"),
        (24, "FATAL"),
    ],
)
def test_the_severity_band_is_read_from_the_number(otlp_env, number, expected):
    post(otlp_env, export([record(severityNumber=number, severityText=None)]))
    assert otlp_env.db.one("SELECT severity FROM log_events")["severity"] == expected


def test_severity_text_wins_when_it_is_one_of_ours(otlp_env):
    post(otlp_env, export([record(severityNumber=9, severityText="warn")]))
    assert otlp_env.db.one("SELECT severity FROM log_events")["severity"] == "WARN"


def test_a_severity_we_do_not_recognise_falls_back_to_the_number(otlp_env):
    post(otlp_env, export([record(severityNumber=17, severityText="OH_NO")]))
    assert otlp_env.db.one("SELECT severity FROM log_events")["severity"] == "ERROR"


def test_a_record_with_no_severity_at_all_is_info(otlp_env):
    post(otlp_env, export([record(severityNumber=None, severityText=None)]))
    assert otlp_env.db.one("SELECT severity FROM log_events")["severity"] == "INFO"


# ---------------------------------------------------------------------- SQL
def test_a_sql_statement_is_never_stored_as_written(otlp_env):
    post(
        otlp_env,
        export(
            [
                record(
                    attributes=[
                        {
                            "key": "db.statement",
                            "value": {
                                "stringValue": "SELECT * FROM users WHERE email = 'amit@example.com' AND id = 8842"
                            },
                        }
                    ]
                )
            ]
        ),
    )
    stored = otlp_env.db.one("SELECT attributes FROM log_events")["attributes"]

    assert "amit@example.com" not in stored, "a statement literal must never be indexed"
    assert "8842" not in stored
    assert "email = ?" in stored
    assert "db.statement_shape" in stored
    assert "db.statement" not in stored.replace("db.statement_shape", "")


def test_two_statements_of_the_same_shape_reduce_to_the_same_template():
    a = otlp.sql_shape("SELECT * FROM users WHERE id = 1")
    b = otlp.sql_shape("SELECT * FROM users WHERE id = 99999")
    assert a == b == "SELECT * FROM users WHERE id = ?"


def test_an_embedded_quote_does_not_defeat_the_shape():
    shaped = otlp.sql_shape("SELECT * FROM t WHERE note = 'it''s fine' AND id = 3")
    assert "it''s fine" not in shaped
    assert shaped.endswith("id = ?")


# ----------------------------------------------------------- what is dropped
def test_a_credential_named_attribute_is_dropped(otlp_env):
    post(
        otlp_env,
        export(
            [
                record(
                    attributes=[
                        {"key": "password", "value": {"stringValue": "hunter2"}},
                        {"key": "apiKey", "value": {"stringValue": "sk-live-1"}},
                        {"key": "region", "value": {"stringValue": "ap-south-1"}},
                    ]
                )
            ]
        ),
    )
    stored = otlp_env.db.one("SELECT attributes FROM log_events")["attributes"]
    assert "hunter2" not in stored
    assert "sk-live-1" not in stored
    assert "ap-south-1" in stored


# -------------------------------------------------------------- idempotency
def test_the_same_export_twice_writes_once(otlp_env):
    payload = export()
    first = post(otlp_env, payload)
    second = post(otlp_env, payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert otlp_env.db.scalar("SELECT COUNT(*) FROM log_events") == 1


def test_a_partially_overlapping_export_only_adds_the_new_records(otlp_env):
    post(otlp_env, export([record()]))
    # Same record, plus one that shares a trace id but a different span.
    post(otlp_env, export([record(), record(spanId="1111111111111111")]))

    assert otlp_env.db.scalar("SELECT COUNT(*) FROM log_events") == 2


def test_the_event_id_is_derived_from_the_trace_and_span(otlp_env):
    post(otlp_env, export([record()]))
    post(otlp_env, export([record()]))
    row = otlp_env.db.one("SELECT event_id FROM log_events")
    assert row["event_id"] == "otlp-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7"


# ------------------------------------------------------------------ partial
def test_one_bad_record_does_not_cost_the_others(otlp_env):
    """OTLP has a channel for partial success; use it."""
    response = post(
        otlp_env,
        export(
            [
                record(),
                record(traceId="zz", spanId="yy", severityNumber=9),  # still fine
                record(status_code=999),  # out of range, refused by validation
            ]
        ),
    )
    assert response.status_code == 200
    body = response.json()
    assert "partialSuccess" in body
    assert body["partialSuccess"]["rejectedLogRecords"] >= 1
    # The good ones landed.
    assert otlp_env.db.scalar("SELECT COUNT(*) FROM log_events") >= 2


def test_an_export_with_no_records_is_accepted_quietly(otlp_env):
    response = post(otlp_env, {"resourceLogs": []})
    assert response.status_code == 200
    assert otlp_env.db.scalar("SELECT COUNT(*) FROM log_events") == 0


def test_a_body_that_is_not_an_otlp_export_is_not_a_crash(otlp_env):
    for payload in ({}, {"resourceLogs": "nope"}, {"resourceLogs": [{"scopeLogs": "nope"}]}):
        assert post(otlp_env, payload).status_code == 200


# ------------------------------------------------------------------ refusals
def test_protobuf_is_refused_with_a_useful_message(otlp_env):
    response = otlp_env.client.post(
        OTLP,
        headers={"Authorization": f"Bearer {otlp_env.ingest}", "Content-Type": "application/protobuf"},
        content=b"\x00\x01\x02",
    )
    assert response.status_code == 415
    assert "http/json" in response.json()["detail"]


def test_a_body_that_is_not_json_is_a_400(otlp_env):
    response = otlp_env.client.post(
        OTLP,
        headers={"Authorization": f"Bearer {otlp_env.ingest}", "Content-Type": "application/json"},
        content=b"{nope",
    )
    assert response.status_code == 400
    assert "not valid JSON" in response.json()["detail"]


def test_otlp_needs_an_ingest_key(otlp_env):
    assert otlp_env.client.post(OTLP, json=export()).status_code == 401
    assert post(otlp_env, export(), token=otlp_env.read).status_code == 403


def test_an_oversized_body_is_refused(make_env):
    env = make_env(max_otlp_bytes=200)
    token = env.key(scope="ingest")
    response = env.client.post(
        OTLP, headers={"Authorization": f"Bearer {token}"}, json=export()
    )
    assert response.status_code == 413


# ------------------------------------------------------------ tenant boundary
def test_the_project_comes_from_the_key_not_the_payload(otlp_env):
    """A key is bound to one project, so an exporter cannot retarget it."""
    provision_app(otlp_env.db, OTHER_APP_ID, "Other App")
    other = otlp_env.key(scope="ingest", app_id=OTHER_APP_ID)

    post(otlp_env, export())
    post(otlp_env, export(), token=other)

    assert otlp_env.db.scalar(
        "SELECT COUNT(*) FROM log_events WHERE app_id = ?", (APP_ID,)
    ) == 1
    assert otlp_env.db.scalar(
        "SELECT COUNT(*) FROM log_events WHERE app_id = ?", (OTHER_APP_ID,)
    ) == 1


def test_the_alias_path_behaves_identically(otlp_env):
    response = otlp_env.client.post(
        ALIAS, headers={"Authorization": f"Bearer {otlp_env.ingest}"}, json=export()
    )
    assert response.status_code == 200
    assert otlp_env.db.scalar("SELECT COUNT(*) FROM log_events") == 1


# ------------------------------------------------------------------ unit bits
def test_the_batch_id_is_derived_from_the_payload_and_the_project():
    assert otlp.batch_id_for({"a": 1}, "app") == otlp.batch_id_for({"a": 1}, "app")
    assert otlp.batch_id_for({"a": 1}, "app") != otlp.batch_id_for({"a": 2}, "app")
    # Two projects sending byte-identical exports must not collide.
    assert otlp.batch_id_for({"a": 1}, "app-a") != otlp.batch_id_for({"a": 1}, "app-b")


def test_an_anyvalue_of_every_kind_is_unwrapped():
    assert otlp._value({"stringValue": "s"}) == "s"
    assert otlp._value({"intValue": "42"}) == 42
    assert otlp._value({"doubleValue": 1.5}) == 1.5
    assert otlp._value({"boolValue": True}) is True
    assert otlp._value({"bytesValue": "AAAA"}) == "[bytes]"
    assert otlp._value({"arrayValue": {"values": [{"intValue": 1}]}}) == [1]
    assert otlp._value({"kvlistValue": {"values": [{"key": "k", "value": {"intValue": 2}}]}}) == {"k": 2}
    assert otlp._value(None) is None


def test_a_zero_timestamp_falls_back_to_now_rather_than_1970():
    from datetime import datetime, timezone

    stamped = otlp.ns_to_iso(0)
    assert stamped.startswith(str(datetime.now(timezone.utc).year))


def test_a_non_numeric_timestamp_does_not_raise():
    assert otlp.ns_to_iso("not-a-number").endswith("Z")
    assert otlp.ns_to_iso(None).endswith("Z")
