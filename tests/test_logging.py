"""Structured logging and the access-log middleware."""

import io
import json
import logging

from fastapi.testclient import TestClient

from powerbase.logging_setup import ACCESS_LOGGER_NAME, StructuredFormatter, configure_logging
from helpers import events_payload


def make_record(**fields):
    record = logging.LogRecord(
        name="powerbase.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="something happened", args=(), exc_info=None,
    )
    if fields:
        record.fields = fields
    return record


def test_key_value_format():
    line = StructuredFormatter().format(make_record(app_id="com.example.shop", status=202))
    assert "INFO" in line
    assert "something happened" in line
    assert "app_id=com.example.shop" in line
    assert "status=202" in line
    assert line.endswith("status=202")


def test_key_value_format_without_fields():
    line = StructuredFormatter().format(make_record())
    assert line.endswith("something happened")


def test_json_format_is_one_object_per_line():
    line = StructuredFormatter(json_format=True).format(
        make_record(app_id="com.example.shop", duration_ms=12.5)
    )
    payload = json.loads(line)
    assert payload["level"] == "INFO"
    assert payload["message"] == "something happened"
    assert payload["app_id"] == "com.example.shop"
    assert payload["duration_ms"] == 12.5
    assert payload["ts"].endswith("Z")
    assert "\n" not in line


def test_json_format_includes_tracebacks():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="powerbase.test", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="failed", args=(), exc_info=sys.exc_info(),
        )
    payload = json.loads(StructuredFormatter(json_format=True).format(record))
    assert "ValueError: boom" in payload["error"]


def test_configure_logging_attaches_one_handler():
    stream = io.StringIO()
    logger = configure_logging("DEBUG", json_format=False, stream=stream)
    logger.debug("hello", extra={"fields": {"k": "v"}})
    output = stream.getvalue()
    assert "hello" in output
    assert "k=v" in output
    assert len(logger.handlers) == 1
    assert logger.propagate is False
    # Reset so later tests are not affected.
    logger.handlers = []
    logger.propagate = True


# --------------------------------------------------------- access middleware

def test_access_log_records_the_request(make_env, caplog):
    env = make_env(access_log=True)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(StructuredFormatter())
    logger = logging.getLogger(ACCESS_LOGGER_NAME)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        key = env.key("ingest")
        env.post("/v1/ingest/events", key, events_payload())
    finally:
        logger.removeHandler(handler)

    output = stream.getvalue()
    assert "http_request" in output
    assert "path=/v1/ingest/events" in output
    assert "status=202" in output
    assert "app_id=com.example.shop" in output


def test_response_carries_its_own_timing(env):
    response = env.client.get("/v1/health")
    assert response.status_code == 200
    assert float(response.headers["x-response-time-ms"]) >= 0


def test_static_requests_are_not_logged(make_env):
    env = make_env(access_log=True)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(StructuredFormatter())
    logger = logging.getLogger(ACCESS_LOGGER_NAME)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        env.client.get("/static/app.css")
    finally:
        logger.removeHandler(handler)
    assert "http_request" not in stream.getvalue()


def test_access_log_can_be_switched_off(make_env):
    env = make_env(access_log=False)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(StructuredFormatter())
    logger = logging.getLogger(ACCESS_LOGGER_NAME)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        env.client.get("/v1/health")
    finally:
        logger.removeHandler(handler)
    assert stream.getvalue() == ""
