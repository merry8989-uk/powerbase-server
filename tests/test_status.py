"""The status surface and the operator CLI.

The CLI test that matters most is the identity check: a port answering `200` is
not evidence that Powerbase is up. That bug existed — an unrelated service on
8080 made `powerbase status` say "up" while the server was down — and an
operator tool that lies is worse than no tool.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from helpers import ADMIN_PASSWORD, APP_ID, log_event, log_events_payload

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "scripts" / "powerbase"


# ------------------------------------------------------------------ endpoints
def test_healthz_is_public_and_says_little(env):
    response = env.client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_does_not_disclose_the_version(env):
    """A load balancer needs a yes/no; everyone else should use /v1/health."""
    assert "version" not in env.client.get("/healthz").json()


def test_the_version_endpoint_is_public_and_small(env):
    body = env.client.get("/v1/status/version").json()
    assert body["service"] == "powerbase"
    assert body["version"]


def test_the_operator_view_needs_the_admin_password(make_env):
    env = make_env(admin_password=ADMIN_PASSWORD)
    assert env.client.get("/v1/status").status_code == 401
    assert env.client.get("/v1/status", headers={"X-Admin-Password": "wrong"}).status_code == 401


def test_the_operator_view_is_disabled_rather_than_open_without_a_password(env):
    """`env` has no admin password. Fail closed, do not fall open."""
    assert env.client.get("/v1/status").status_code == 503
    assert env.client.get("/v1/status", headers={"X-Admin-Password": ""}).status_code == 503


def test_the_operator_view_reports_the_instance(make_env):
    env = make_env(admin_password=ADMIN_PASSWORD)
    body = env.client.get("/v1/status", headers={"X-Admin-Password": ADMIN_PASSWORD}).json()

    assert body["service"] == "powerbase"
    assert body["version"]
    assert body["uptime_seconds"] >= 0
    assert body["database"]["reachable"] is True
    assert body["database"]["journal_mode"] == "wal"
    assert "apps" in body["totals"]
    assert "log_events" in body["totals"]
    assert "last_batch_at" in body["ingest"]
    assert "count" in body["backups"]
    assert body["settings"]["redaction_enabled"] is True


def test_the_operator_view_counts_what_was_ingested(make_env):
    env = make_env(admin_password=ADMIN_PASSWORD)
    token = env.key()
    env.post(
        "/v1/ingest/log-events",
        token,
        log_events_payload(batch_id="status-1", events=[log_event(event_id="e1")]),
    )
    body = env.client.get("/v1/status", headers={"X-Admin-Password": ADMIN_PASSWORD}).json()

    assert body["totals"]["log_events"] == 1
    assert body["totals"]["apps"] == 1
    assert body["totals"]["api_keys"] == 1
    assert body["ingest"]["batches"] == 1
    assert body["ingest"]["last_log_event_at"] is not None


def test_the_operator_view_masks_nothing_it_should_not_show(make_env):
    """It is the admin view, so it may show paths — but never a key or a secret."""
    env = make_env(admin_password=ADMIN_PASSWORD)
    token = env.key()
    body = env.client.get("/v1/status", headers={"X-Admin-Password": ADMIN_PASSWORD}).json()
    text = json.dumps(body)

    assert token not in text
    assert "$argon2" not in text
    assert ADMIN_PASSWORD not in text


def test_the_public_status_page_renders_and_stays_thin(make_env):
    env = make_env(admin_password=ADMIN_PASSWORD)
    env.client.get("/v1/status", headers={"X-Admin-Password": ADMIN_PASSWORD})  # noqa: B018
    page = env.client.get("/status")

    assert page.status_code == 200
    assert "Powerbase" in page.text
    assert "operational" in page.text
    # The page is public, so it must not carry the operator's numbers.
    for leak in ("log_events", "api_keys", "last_batch_at", "/var/lib", "db_path"):
        assert leak not in page.text, leak


def test_a_broken_database_is_reported_rather_than_crashing(make_env, monkeypatch):
    env = make_env(admin_password=ADMIN_PASSWORD)

    def boom(*args, **kwargs):
        raise RuntimeError("disk went away")

    monkeypatch.setattr(env.db, "scalar", boom)

    assert env.client.get("/healthz").status_code == 503
    page = env.client.get("/status")
    assert page.status_code == 200
    assert "degraded" in page.text


# ------------------------------------------------------------------------ CLI
def run_cli(*args: str, db: Path, port: str = "8099", env_file: str = "/nonexistent"):
    environment = {
        **os.environ,
        "POWERBASE_DB": str(db),
        "POWERBASE_PORT": port,
        "POWERBASE_ADMIN_PASSWORD": "cli-test-password",
        "POWERBASE_BACKUP_DIR": str(db.parent / "backups"),
        "POWERBASE_ENV_FILE": env_file,
        "PYTHONPATH": str(REPO),
        "NO_COLOR": "1",
    }
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True, text=True, env=environment, timeout=60,
    )


def test_the_cli_creates_a_project_and_prints_both_keys(tmp_path):
    db = tmp_path / "cli.db"
    result = run_cli("app", "create", "com.acme.shop", "Acme Shop", "web", db=db)
    assert result.returncode == 0, result.stderr
    assert "ingest (goes in the app)" in result.stdout
    assert "read (stays server-side)" in result.stdout
    assert result.stdout.count("pb_") >= 2


def test_the_cli_lists_projects_and_keys(tmp_path):
    db = tmp_path / "cli.db"
    run_cli("app", "create", "com.acme.shop", "Acme Shop", "web", db=db)

    projects = run_cli("apps", db=db)
    assert "com.acme.shop" in projects.stdout
    assert "live keys 2" in projects.stdout

    keys = run_cli("keys", "--app", "com.acme.shop", db=db)
    assert "ingest" in keys.stdout and "read" in keys.stdout
    assert "live" in keys.stdout


def test_the_cli_rotates_and_then_revokes(tmp_path):
    db = tmp_path / "cli.db"
    run_cli("app", "create", "com.acme.shop", "Acme Shop", "web", db=db)

    created = run_cli(
        "key", "create", "--app", "com.acme.shop", "--scope", "ingest", "--label", "temp", db=db
    )
    key_id = next(
        line.split()[-1] for line in created.stdout.splitlines() if "key id" in line
    )

    rotated = run_cli("key", "rotate", key_id, db=db)
    assert rotated.returncode == 0
    assert "rotated from" in rotated.stdout

    # The old id is revoked by the rotation, so revoking it again is an error.
    again = run_cli("key", "revoke", key_id, db=db)
    assert again.returncode == 1
    assert "no live key" in again.stderr


def test_the_cli_backs_up_and_lists(tmp_path):
    db = tmp_path / "cli.db"
    run_cli("app", "create", "com.acme.shop", "Acme Shop", "web", db=db)

    backup = run_cli("backup", db=db)
    assert backup.returncode == 0, backup.stderr
    assert "verification" in backup.stdout
    assert "'ok': True" in backup.stdout

    listing = run_cli("backups", db=db)
    assert "powerbase-" in listing.stdout


def test_the_cli_reports_a_port_with_nothing_on_it(tmp_path):
    db = tmp_path / "cli.db"
    result = run_cli("status", db=db, port="8099")
    assert result.returncode == 0
    assert "not answering" in result.stdout


def test_the_cli_refuses_a_200_that_is_not_powerbase(tmp_path):
    """The regression test for the bug this file is named after.

    A plain HTTP server answers `200` to anything. `powerbase status` must not
    read that as "the server is up".
    """

    class Impostor(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Impostor)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        db = tmp_path / "cli.db"
        result = run_cli("status", db=db, port=str(port))
        assert result.returncode == 0
        verdict = next(
            line for line in result.stdout.splitlines() if "server process" in line
        )
        assert "busy with something else" in verdict
        assert "not answering" not in verdict
    finally:
        server.shutdown()
        server.server_close()


def test_the_cli_masks_the_admin_password(tmp_path):
    db = tmp_path / "cli.db"
    result = run_cli("env", db=db)
    assert result.returncode == 0
    assert "cli-test-password" not in result.stdout
    assert "set (" in result.stdout


def test_the_cli_with_no_arguments_prints_help(tmp_path):
    result = run_cli(db=tmp_path / "cli.db")
    assert result.returncode == 0
    assert "usage: powerbase" in result.stdout
    assert "status" in result.stdout
