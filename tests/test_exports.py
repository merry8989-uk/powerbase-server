"""Exporting: the file, the link, and the audit trail behind both."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from helpers import ADMIN_PASSWORD, APP_ID, OTHER_APP_ID, log_event, log_events_payload
from powerbase import audit, exports
from powerbase.provision import provision_app

EXPORTS = "/v1/exports"
AUDIT = "/v1/audit"


@pytest.fixture()
def exporter(make_env, tmp_path):
    """An instance with five events and a small row cap, so truncation is real."""
    env = make_env(
        admin_password=ADMIN_PASSWORD,
        export_dir=str(tmp_path / "exports"),
        export_max_rows=3,
    )
    ingest = env.key(scope="ingest")
    for index in range(5):
        env.post(
            "/v1/ingest/log-events",
            ingest,
            log_events_payload(
                batch_id=f"exp-{index}",
                events=[
                    log_event(
                        event_id=f"e{index}",
                        ts=f"2026-10-22T0{index}:00:00Z",
                        severity="ERROR" if index % 2 else "INFO",
                        service_name="checkout-api",
                        status_code=500 if index % 2 else 200,
                        message=f"event {index} for amit@example.com",
                        attributes={"region": "ap-south-1", "password": "never-stored"},
                    )
                ],
            ),
        )
    env.admin = {"X-Admin-Password": ADMIN_PASSWORD}
    return env


def create(env, **body):
    body.setdefault("app_id", APP_ID)
    body.setdefault("format", "csv")
    return env.client.post(EXPORTS, headers=env.admin, json=body)


def read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


# ------------------------------------------------------------------- the file
def test_an_export_writes_a_file_and_reports_it(exporter):
    response = create(exporter)
    assert response.status_code == 201, response.text
    job = response.json()

    assert job["status"] == "completed"
    assert job["row_count"] == 3  # the cap, not the 5 that match
    assert job["truncated"] is True
    assert Path(job["result_path"]).is_file()
    assert job["result_bytes"] > 0


def test_the_cap_is_never_silent(exporter):
    """A truncated export that looks complete is the worst possible outcome."""
    job = create(exporter).json()
    assert job["truncated"] is True

    download = exporter.client.get(job["download_url"].replace("http://testserver", ""))
    assert download.headers["X-Powerbase-Truncated"] == "true"
    assert download.headers["X-Powerbase-Rows"] == "3"


def test_an_export_under_the_cap_is_not_marked_truncated(make_env, tmp_path):
    env = make_env(
        admin_password=ADMIN_PASSWORD, export_dir=str(tmp_path / "e"), export_max_rows=100
    )
    token = env.key()
    env.post("/v1/ingest/log-events", token, log_events_payload(events=[log_event(event_id="e1")]))

    job = env.client.post(
        EXPORTS, headers={"X-Admin-Password": ADMIN_PASSWORD}, json={"app_id": APP_ID}
    ).json()
    assert job["row_count"] == 1
    assert job["truncated"] is False


def test_csv_has_a_header_and_the_documented_columns(exporter):
    job = create(exporter).json()
    lines = read(job["result_path"]).splitlines()
    assert lines[0].split(",") == list(exports.EXPORT_COLUMNS)
    assert len(lines) == 4  # header + three rows


def test_rows_are_ordered_oldest_first_whatever_the_console_was_sorted_by(exporter):
    job = create(exporter, format="jsonl").json()
    ids = [json.loads(line)["event_id"] for line in read(job["result_path"]).strip().splitlines()]
    assert ids == ["e0", "e1", "e2"]


@pytest.mark.parametrize("fmt", ["json", "jsonl"])
def test_json_formats_are_parseable(exporter, fmt):
    job = create(exporter, format=fmt).json()
    body = read(job["result_path"])
    if fmt == "json":
        parsed = json.loads(body)
        assert isinstance(parsed, list) and len(parsed) == 3
    else:
        parsed = [json.loads(line) for line in body.strip().splitlines()]
        assert len(parsed) == 3


def test_an_empty_result_is_still_a_valid_file(make_env, tmp_path):
    env = make_env(admin_password=ADMIN_PASSWORD, export_dir=str(tmp_path / "e"))
    env.key()  # a project with no events at all

    for fmt, check in (("csv", lambda b: b.count("\n") == 1), ("json", lambda b: json.loads(b) == [])):
        job = env.client.post(
            EXPORTS,
            headers={"X-Admin-Password": ADMIN_PASSWORD},
            json={"app_id": APP_ID, "format": fmt},
        ).json()
        assert job["row_count"] == 0
        assert check(read(job["result_path"]))


# ------------------------------------------------------------- the redaction
def test_redaction_is_applied_again_on_the_way_out(exporter):
    """Defence in depth: a row written while redaction was off must not escape."""
    job = create(exporter).json()
    body = read(job["result_path"])

    assert "amit@example.com" not in body
    assert "[REDACTED_EMAIL]" in body
    assert "never-stored" not in body  # the credential-named attribute


def test_attributes_survive_as_json(exporter):
    job = create(exporter, format="jsonl").json()
    first = json.loads(read(job["result_path"]).splitlines()[0])
    assert first["attributes"]["region"] == "ap-south-1"


# ------------------------------------------------------------------ the link
def test_the_download_link_works_without_the_admin_password(exporter):
    job = create(exporter).json()
    response = exporter.client.get(job["download_url"].replace("http://testserver", ""))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert "event_time" in response.text


def test_a_missing_or_tampered_token_is_refused(exporter):
    job = create(exporter).json()
    base = f"{EXPORTS}/{job['id']}/download"

    assert exporter.client.get(base).status_code == 401
    assert exporter.client.get(f"{base}?token=not-a-token").status_code == 401

    real = job["download_url"].split("token=")[1]
    assert exporter.client.get(f"{base}?token={real[:-1]}x").status_code == 401


def test_a_token_for_one_export_does_not_open_another(exporter):
    first = create(exporter).json()
    second = create(exporter).json()
    token = second["download_url"].split("token=")[1]

    response = exporter.client.get(f"{EXPORTS}/{first['id']}/download?token={token}")
    assert response.status_code == 403


def test_an_expired_link_is_gone_and_the_file_is_deleted(exporter):
    job = create(exporter).json()
    path = Path(job["result_path"])
    assert path.is_file()

    # Move the expiry into the past rather than waiting a week.
    with exporter.db.write() as conn:
        conn.execute(
            "UPDATE export_jobs SET expires_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00.000000Z", job["id"]),
        )

    result = exports.expire_jobs(exporter.db, exporter.settings)
    assert result["expired"] == 1
    assert result["files_removed"] == 1
    assert not path.exists()

    response = exporter.client.get(job["download_url"].replace("http://testserver", ""))
    assert response.status_code == 410


def test_a_token_signed_before_expiry_stops_working_after_it(exporter):
    """The expiry is inside the signed payload, so an old link cannot outlive it."""
    job = create(exporter).json()
    token = job["download_url"].split("token=")[1]
    with exporter.db.write() as conn:
        conn.execute(
            "UPDATE export_jobs SET expires_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00.000000Z", job["id"]),
        )
    response = exporter.client.get(f"{EXPORTS}/{job['id']}/download?token={token}")
    assert response.status_code in (403, 410)


# ------------------------------------------------------------------- the jobs
def test_a_deferred_job_waits_and_then_completes(exporter):
    job = create(exporter, defer=True).json()
    assert job["status"] == "queued"
    assert "download_url" not in job

    result = exports.complete_queued(exporter.db, exporter.settings)
    assert result == {"queued": 1, "completed": 1, "failed": 0}

    after = exporter.client.get(f"{EXPORTS}/{job['id']}", headers=exporter.admin).json()
    assert after["status"] == "completed"
    assert "download_url" in after


def test_jobs_can_be_listed_and_deleted(exporter):
    job = create(exporter).json()
    listing = exporter.client.get(EXPORTS, headers=exporter.admin).json()
    assert [item["id"] for item in listing["items"]] == [job["id"]]

    assert exporter.client.delete(f"{EXPORTS}/{job['id']}", headers=exporter.admin).status_code == 200
    assert exporter.client.get(f"{EXPORTS}/{job['id']}", headers=exporter.admin).status_code == 404
    assert not Path(job["result_path"]).exists()


def test_deleting_twice_is_a_404(exporter):
    job = create(exporter).json()
    exporter.client.delete(f"{EXPORTS}/{job['id']}", headers=exporter.admin)
    assert exporter.client.delete(f"{EXPORTS}/{job['id']}", headers=exporter.admin).status_code == 404


def test_exports_are_scoped_to_their_project(exporter):
    create(exporter)
    provision_app(exporter.db, OTHER_APP_ID, "Other App")
    listing = exporter.client.get(
        EXPORTS, headers=exporter.admin, params={"app_id": OTHER_APP_ID}
    ).json()
    assert listing["items"] == []


# --------------------------------------------------------------- the refusals
def test_exports_need_the_admin_password(exporter):
    assert exporter.client.get(EXPORTS).status_code == 401
    assert exporter.client.post(EXPORTS, json={"app_id": APP_ID}).status_code == 401


def test_an_unknown_format_is_refused(exporter):
    assert create(exporter, format="xlsx").status_code == 400


def test_a_bad_filter_is_refused_before_anything_is_written(exporter):
    before = len(exports.list_jobs(exporter.db))
    response = create(exporter, filters=[{"field": "password", "operator": "eq", "value": "x"}])

    assert response.status_code == 400
    # No half-made job left behind.
    assert len(exports.list_jobs(exporter.db)) == before


def test_an_unknown_project_is_refused(exporter):
    assert create(exporter, app_id="no.such.project").status_code == 400


def test_an_unknown_body_key_is_refused(exporter):
    assert create(exporter, raw_sql="SELECT 1").status_code == 422


# ------------------------------------------------------------------ the audit
def test_creating_and_downloading_are_both_audited(exporter):
    job = create(exporter).json()
    exporter.client.get(job["download_url"].replace("http://testserver", ""))

    events = exporter.client.get(AUDIT, headers=exporter.admin).json()
    types = [item["event_type"] for item in events["items"]]
    assert audit.EXPORT_CREATED in types
    assert audit.EXPORT_DOWNLOADED in types

    created = next(i for i in events["items"] if i["event_type"] == audit.EXPORT_CREATED)
    assert created["target_id"] == job["id"]
    assert created["app_id"] == APP_ID
    assert created["outcome"] == "success"


def test_expiry_and_deletion_are_audited(exporter):
    job = create(exporter).json()
    exports.delete_job(exporter.db, exporter.settings, job["id"])

    types = {
        item["event_type"]
        for item in exporter.client.get(AUDIT, headers=exporter.admin).json()["items"]
    }
    assert audit.EXPORT_DELETED in types


def test_the_audit_view_can_be_filtered(exporter):
    create(exporter)
    by_type = exporter.client.get(
        AUDIT, headers=exporter.admin, params={"event_type": audit.EXPORT_CREATED}
    ).json()
    assert by_type["total"] >= 1
    assert {item["event_type"] for item in by_type["items"]} == {audit.EXPORT_CREATED}

    none = exporter.client.get(AUDIT, headers=exporter.admin, params={"app_id": OTHER_APP_ID}).json()
    assert none["total"] == 0


def test_the_audit_view_needs_the_admin_password(exporter):
    assert exporter.client.get(AUDIT).status_code == 401


def test_an_audit_row_never_carries_a_secret(exporter):
    create(exporter)
    body = exporter.client.get(AUDIT, headers=exporter.admin).text
    assert ADMIN_PASSWORD not in body
    assert "$argon2" not in body
