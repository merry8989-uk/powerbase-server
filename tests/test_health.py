from powerbase.migrations import run_migrations


def test_health_ok(env):
    r = env.client.get("/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["journal_mode"] == "wal"


def test_api_root(env):
    # The JSON service banner lives under /v1/ now; "/" is the dashboard.
    r = env.client.get("/v1/")
    assert r.status_code == 200
    assert r.json()["service"] == "powerbase"
    assert r.json()["dashboard"] is None  # no admin password set in this fixture


def test_schema_has_all_tables(env):
    tables = {
        row["name"]
        for row in env.db.query("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    expected = {
        "apps",
        "api_keys",
        "devices",
        "sessions",
        "events",
        "logs",
        "crashes",
        "metrics_daily",
        "ingest_batches",
        "schema_migrations",
    }
    assert expected.issubset(tables)
    assert "logs_fts" in tables


def test_migrations_are_idempotent(env):
    applied = run_migrations(env.db, env.settings.migrations_dir)
    assert applied == []
    assert env.db.scalar("SELECT COUNT(*) FROM schema_migrations") >= 1
