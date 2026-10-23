"""Schema contract.

A snapshot of the shape of the database. If a column is renamed or dropped by
accident, this fails — which matters because the Kotlin SDK and the read API
both depend on these names, and a silent rename would break them at runtime
rather than at build time.
"""

from helpers import APP_ID

EXPECTED_COLUMNS = {
    "apps": ["id", "name", "platform", "retention_days", "sampling_rate", "created_at"],
    "api_keys": ["id", "app_id", "key_hash", "scope", "label", "created_at",
                "last_used_at", "revoked_at", "expires_at", "rotated_from"],
    "devices": ["id", "app_id", "model", "manufacturer", "os_version", "app_version",
                "sdk_version", "locale", "first_seen", "last_seen"],
    "sessions": ["id", "app_id", "device_id", "user_ref", "started_at", "ended_at",
                 "duration_ms", "end_reason", "app_version", "os_version", "country"],
    "events": ["id", "app_id", "device_id", "session_id", "ts", "received_at",
               "name", "category", "screen", "properties", "sdk_version"],
    # Observability shape (migration 0003). Separate from `events` on purpose:
    # this is what a browser or backend service reports.
    "log_events": ["id", "event_id", "app_id", "event_time", "received_at", "source",
                   "severity", "event_type", "service_name", "environment", "app_version",
                   "release_id", "request_id", "trace_id", "span_id", "route_template",
                   "http_method", "status_code", "duration_ms", "error_code",
                   "error_fingerprint", "message", "attributes", "redaction_version",
                   "sdk_version",
                   "category", "source_type", "source_id", "platform_version"],
    "logs": ["id", "app_id", "device_id", "session_id", "ts", "received_at",
             "level", "tag", "message", "stacktrace", "context", "sdk_version"],
    "crashes": ["id", "app_id", "device_id", "session_id", "ts", "received_at",
                "kind", "message", "stacktrace", "fingerprint", "handled",
                "app_version", "os_version"],
    "metrics_daily": ["day", "app_id", "metric", "dimension", "value"],
    "ingest_batches": ["batch_id", "app_id", "kind", "received_at", "event_count"],
    "job_runs": ["id", "job", "day", "ran_at", "ok", "detail"],
    "saved_queries": ["id", "app_id", "name", "query_definition", "created_by",
                      "created_at", "updated_at"],
    "export_jobs": ["id", "app_id", "requested_by", "format", "filter_definition",
                    "status", "row_count", "result_bytes", "result_path", "truncated",
                    "created_at", "started_at", "completed_at", "expires_at",
                    "sanitized_error"],
    "audit_events": ["id", "event_time", "app_id", "actor", "event_type", "outcome",
                     "target_type", "target_id", "request_id", "metadata"],
    "alert_incidents": ["id", "dedup_key", "app_id", "rule", "severity", "title",
                        "detail", "context", "status", "occurrences", "first_seen_at",
                        "last_seen_at", "last_notified_at", "notify_count", "acked_by",
                        "acked_at", "resolved_at", "resolved_by"],
    "alert_channels": ["id", "app_id", "kind", "target", "enabled", "min_severity",
                       "created_at"],
    "alert_deliveries": ["id", "incident_id", "channel_id", "kind", "target", "event",
                         "status", "attempts", "next_attempt_at", "last_error",
                         "created_at", "delivered_at"],
    "users": ["id", "email", "display_name", "password_hash", "role", "status",
              "created_at", "last_login_at", "failed_attempts", "locked_until"],
    "user_sessions": ["id", "user_id", "token_hash", "created_at", "expires_at",
                      "revoked_at", "user_agent"],
    "invites": ["id", "email", "role", "app_id", "token_hash", "invited_by",
                "created_at", "expires_at", "accepted_at", "accepted_by"],
    "project_members": ["app_id", "user_id", "role", "created_at"],
    "spans": ["id", "span_id", "trace_id", "parent_span_id", "app_id", "service_name",
              "operation", "span_kind", "status", "status_message", "start_time",
              "end_time", "duration_ms", "route_template", "attributes", "received_at"],
    "metric_points": ["id", "app_id", "metric_name", "metric_kind", "unit",
                      "service_name", "point_time", "value", "count", "attributes",
                      "dimension_hash", "received_at"],
    "log_drains": ["id", "app_id", "name", "kind", "target", "enabled", "batch_size",
                   "min_severity", "cursor_time", "cursor_id", "created_at", "last_run_at"],
    "drain_runs": ["id", "drain_id", "started_at", "finished_at", "sent", "ok",
                   "last_error", "cursor_time", "cursor_id"],
    "schema_migrations": ["version", "name", "applied_at"],
}

EXPECTED_INDEXES = {
    "idx_api_keys_app",
    "idx_devices_app_seen",
    "idx_sessions_device",
    "idx_sessions_app_started",
    "idx_events_app_ts",
    "idx_events_session",
    "idx_events_app_name",
    "idx_logs_app_ts",
    "idx_logs_level_ts",
    "idx_logs_session",
    "idx_crashes_app_ts",
    "idx_crashes_fingerprint",
    "idx_log_events_app_time",
    "idx_log_events_app_sev_time",
    "idx_log_events_app_type_time",
    "idx_log_events_app_source_time",
    "idx_log_events_fingerprint",
    "idx_log_events_trace",
    "idx_log_events_request",
    "idx_log_events_route",
    "idx_log_events_category",
    "idx_log_events_source_type",
    "idx_ingest_batches_app_time",
    "idx_job_runs_job_day",
    "idx_job_runs_ran_at",
    "idx_saved_queries_app",
    "idx_export_jobs_app_time",
    "idx_export_jobs_status",
    "idx_audit_app_time",
    "idx_audit_type_time",
    "idx_audit_time",
    "idx_alert_incidents_open",
    "idx_alert_incidents_app",
    "idx_alert_incidents_status",
    "idx_alert_channels_app",
    "idx_alert_deliveries_due",
    "idx_alert_deliveries_incident",
    "idx_users_role",
    "idx_user_sessions_token",
    "idx_user_sessions_user",
    "idx_invites_email",
    "idx_project_members_user",
    "idx_spans_app_time",
    "idx_spans_trace",
    "idx_spans_service",
    "idx_spans_operation",
    "idx_spans_errors",
    "idx_metric_points_app_time",
    "idx_metric_points_name",
    "idx_metric_points_service",
    "idx_log_drains_enabled",
    "idx_drain_runs_drain",
}


def columns_of(db, table):
    return [row["name"] for row in db.query(f"PRAGMA table_info({table})")]


def test_every_table_has_the_expected_columns(env):
    for table, expected in EXPECTED_COLUMNS.items():
        assert columns_of(env.db, table) == expected, f"{table} columns changed"


def test_no_unexpected_tables(env):
    tables = {
        row["name"]
        for row in env.db.query("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    # FTS5 keeps its own shadow tables; everything else must be accounted for.
    unexpected = {
        name for name in tables
        if not name.startswith("sqlite_") and "_fts" not in name
    } - set(EXPECTED_COLUMNS)
    assert unexpected == set(), f"unexpected tables: {unexpected}"


def test_the_expected_indexes_exist(env):
    indexes = {
        row["name"]
        for row in env.db.query("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    missing = EXPECTED_INDEXES - indexes
    assert missing == set(), f"missing indexes: {missing}"


def test_the_ingest_contract_columns_are_present(env):
    # These are the exact field names the Kotlin SDK writes and the API returns.
    assert {"ts", "device_id", "session_id", "name", "properties"} <= set(columns_of(env.db, "events"))
    assert {"level", "tag", "message", "stacktrace", "context"} <= set(columns_of(env.db, "logs"))
    assert {"kind", "message", "stacktrace", "fingerprint", "handled"} <= set(columns_of(env.db, "crashes"))


def test_foreign_keys_are_enforced(env):
    import sqlite3

    import pytest

    with pytest.raises(sqlite3.IntegrityError):
        with env.db.write() as conn:
            conn.execute(
                "INSERT INTO events (app_id, device_id, ts, received_at, name) "
                "VALUES ('no-such-app', 'no-such-device', '2026-01-01T00:00:00.000000Z', "
                "'2026-01-01T00:00:00.000000Z', 'x')"
            )


def test_the_fts_index_tracks_log_messages(env):
    from powerbase import repository as repo
    from powerbase.models import LogIn, LogsBatch

    repo.write_logs(env.db, APP_ID, LogsBatch(
        batch_id="contract-1", app_id=APP_ID,
        logs=[LogIn(ts="2026-01-01T00:00:00Z", device_id="d1", level="ERROR",
                    message="needle in a haystack")],
    ), redact=True)

    hits = env.db.query("SELECT rowid FROM logs_fts WHERE logs_fts MATCH ?", ("needle",))
    assert len(hits) == 1
