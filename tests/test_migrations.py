"""Migration tooling: status, verify, and the rollback drill."""

from powerbase.db import Database
from powerbase.migrations import REQUIRED_TABLES, rollback_drill, run_migrations, status, verify
from helpers import ADMIN_PASSWORD, APP_ID  # noqa: F401  (APP_ID used for provisioning side effects)


def test_status_lists_applied_migrations(seeded_env):
    info = status(seeded_env.db, seeded_env.settings.migrations_dir)
    names = [m["name"] for m in info["applied"]]
    assert "0001_init.sql" in names
    assert "0002_job_runs.sql" in names
    assert info["pending"] == []
    assert info["unknown"] == []


def test_status_reports_pending(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_first.sql").write_text("CREATE TABLE first (id INTEGER PRIMARY KEY);")
    db = Database(tmp_path / "m.db")
    run_migrations(db, migrations)

    (migrations / "0002_second.sql").write_text("CREATE TABLE second (id INTEGER PRIMARY KEY);")
    info = status(db, migrations)
    assert [m["name"] for m in info["pending"]] == ["0002_second.sql"]


def test_status_reports_unknown(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_first.sql").write_text("CREATE TABLE first (id INTEGER PRIMARY KEY);")
    db = Database(tmp_path / "m.db")
    run_migrations(db, migrations)

    (migrations / "0001_first.sql").unlink()
    info = status(db, migrations)
    assert [m["name"] for m in info["unknown"]] == ["0001_first.sql"]


def test_verify_passes_on_a_migrated_database(seeded_env):
    result = verify(seeded_env.db, seeded_env.settings.migrations_dir)
    assert result["ok"] is True
    assert result["integrity"] == "ok"
    assert result["missing_tables"] == []
    assert result["pending_migrations"] == []
    assert result["foreign_key_violations"] is None


def test_verify_catches_a_missing_table(seeded_env):
    with seeded_env.db.write() as conn:
        conn.execute("DROP TABLE crashes")
    result = verify(seeded_env.db, seeded_env.settings.migrations_dir)
    assert result["ok"] is False
    assert "crashes" in result["missing_tables"]


def test_verify_catches_a_pending_migration(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_first.sql").write_text("CREATE TABLE first (id INTEGER PRIMARY KEY);")
    db = Database(tmp_path / "m.db")
    run_migrations(db, migrations)
    (migrations / "0002_second.sql").write_text("CREATE TABLE second (id INTEGER PRIMARY KEY);")

    result = verify(db, migrations)
    assert result["ok"] is False
    assert result["pending_migrations"] == ["0002_second.sql"]


def test_required_tables_are_the_ones_we_expect():
    assert REQUIRED_TABLES == {
        "apps", "api_keys", "devices", "sessions", "events", "logs",
        "crashes", "metrics_daily", "ingest_batches", "schema_migrations", "job_runs",
    }


# ------------------------------------------------------------ rollback drill

def test_rollback_drill_succeeds(seeded_env, tmp_path):
    result = rollback_drill(seeded_env.settings.db_path, tmp_path / "drill")
    assert result["ok"] is True
    assert result["migration_changed_schema"] is True
    assert result["schema_restored"] is True
    assert result["rows_preserved"] is True
    assert result["rows"] == 1


def test_rollback_drill_leaves_the_live_database_alone(seeded_env, tmp_path):
    before = seeded_env.db.scalar("SELECT COUNT(*) FROM events")
    rollback_drill(seeded_env.settings.db_path, tmp_path / "drill")
    assert seeded_env.db.scalar("SELECT COUNT(*) FROM events") == before
    tables = {r["name"] for r in seeded_env.db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "drill_marker" not in tables


def test_rollback_drill_uses_a_custom_migration(seeded_env, tmp_path):
    result = rollback_drill(
        seeded_env.settings.db_path,
        tmp_path / "drill",
        synthetic_sql="ALTER TABLE apps ADD COLUMN drill_note TEXT;",
    )
    assert result["ok"] is True


def test_rollback_drill_detects_a_migration_that_did_nothing(seeded_env, tmp_path):
    # If the "migration" changes nothing, the drill cannot claim to have proven
    # anything — it must fail rather than pass vacuously.
    result = rollback_drill(
        seeded_env.settings.db_path, tmp_path / "drill", synthetic_sql="SELECT 1;"
    )
    assert result["ok"] is False
    assert result["migration_changed_schema"] is False


# ---------------------------------------------------- migration atomicity

def test_a_failing_migration_rolls_back_and_aborts(tmp_path):
    """A broken migration must leave no partial schema and must not be recorded.

    It also propagates, which is deliberate: a server must not come up on a
    half-migrated database.
    """
    import sqlite3

    import pytest

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_ok.sql").write_text("CREATE TABLE good (id INTEGER PRIMARY KEY);")
    (migrations / "0002_bad.sql").write_text(
        "CREATE TABLE partial (id INTEGER PRIMARY KEY);\nTHIS IS NOT VALID SQL;"
    )

    db = Database(tmp_path / "m.db")
    with pytest.raises(sqlite3.OperationalError):
        run_migrations(db, migrations)

    tables = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "good" in tables, "the migration before the failure should be applied"
    assert "partial" not in tables, "a failed migration must roll back entirely"
    assert db.scalar("SELECT COUNT(*) FROM schema_migrations") == 1

    # And it is still pending, so a fix will be picked up on the next start.
    (migrations / "0002_bad.sql").write_text("CREATE TABLE second (id INTEGER PRIMARY KEY);")
    assert run_migrations(db, migrations) == ["0002_bad.sql"]


def test_migrations_are_idempotent_on_a_fresh_database(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_ok.sql").write_text("CREATE TABLE good (id INTEGER PRIMARY KEY);")
    db = Database(tmp_path / "m.db")
    assert run_migrations(db, migrations) == ["0001_ok.sql"]
    assert run_migrations(db, migrations) == []
    assert db.scalar("SELECT COUNT(*) FROM schema_migrations") == 1
