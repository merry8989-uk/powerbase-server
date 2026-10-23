"""Forward-only SQL migration runner.

Each ``migrations/NNNN_name.sql`` file is applied exactly once, inside a single
transaction, and recorded in ``schema_migrations``. Statements are split with
``sqlite3.complete_statement`` so triggers (which contain semicolons inside
``BEGIN ... END``) are handled correctly.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .db import Database


def _quote(identifier: str) -> str:
    # Local, not imported from queries: queries -> rollups -> migrations, so an
    # import back into queries would be a cycle.
    return '"' + identifier.replace('"', '""') + '"'


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def split_statements(script: str) -> list[str]:
    statements: list[str] = []
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            stripped = buf.strip()
            if stripped:
                statements.append(stripped)
            buf = ""
    if buf.strip():
        statements.append(buf.strip())
    return statements


def applied_versions(db: Database) -> set[int]:
    return {row["version"] for row in db.query("SELECT version FROM schema_migrations")}


def status(db: Database, migrations_dir: str | Path) -> dict:
    """Which migrations have been applied, and which are pending."""
    directory = Path(migrations_dir)
    files = {}
    for path in sorted(directory.glob("*.sql")):
        match = re.match(r"(\d+)", path.name)
        if match:
            files[int(match.group(1))] = path.name

    applied_rows = db.query("SELECT version, name, applied_at FROM schema_migrations ORDER BY version")
    applied = {row["version"]: row for row in applied_rows}

    return {
        "applied": [
            {"version": v, "name": applied[v]["name"], "applied_at": applied[v]["applied_at"]}
            for v in sorted(applied)
        ],
        "pending": [{"version": v, "name": files[v]} for v in sorted(files) if v not in applied],
        "unknown": [
            {"version": v, "name": applied[v]["name"]}
            for v in sorted(applied)
            if v not in files
        ],
    }


REQUIRED_TABLES = frozenset(
    {
        "apps", "api_keys", "devices", "sessions", "events", "logs",
        "crashes", "metrics_daily", "ingest_batches", "schema_migrations", "job_runs",
    }
)


def verify(db: Database, migrations_dir: str | Path) -> dict:
    """Is this database in the state the migration files say it should be?"""
    info = status(db, migrations_dir)
    tables = {
        row["name"]
        for row in db.query("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    missing = sorted(REQUIRED_TABLES - tables)
    integrity = db.scalar("PRAGMA integrity_check")
    foreign_keys = db.scalar("PRAGMA foreign_key_check")

    ok = not missing and not info["pending"] and integrity == "ok"
    return {
        "ok": ok,
        "integrity": integrity,
        "missing_tables": missing,
        "pending_migrations": [m["name"] for m in info["pending"]],
        "unknown_migrations": [m["name"] for m in info["unknown"]],
        "foreign_key_violations": foreign_keys,
    }


# --------------------------------------------------------------------------- #
# Rollback drill
# --------------------------------------------------------------------------- #

DEFAULT_DRILL_SQL = (
    "CREATE TABLE drill_marker (id INTEGER PRIMARY KEY, note TEXT);\n"
    "INSERT INTO drill_marker (note) VALUES ('rollback drill');\n"
)


def _copy_database(source: str | Path, destination: str | Path) -> None:
    """Copy via SQLite's backup API, so a WAL is included and no writer races."""
    src = Database(source)
    try:
        with src.exclusive() as connection:
            sink = sqlite3.connect(str(destination))
            try:
                connection.backup(sink)
            finally:
                sink.close()
    finally:
        src.close()


def _schema_snapshot(path: str | Path) -> list[tuple]:
    connection = sqlite3.connect(str(path))
    try:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
            )
        ]
    finally:
        connection.close()


def _row_count(path: str | Path, table: str) -> int:
    connection = sqlite3.connect(str(path))
    try:
        return connection.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0]
    except sqlite3.Error:
        return -1
    finally:
        connection.close()


def rollback_drill(
    db_path: str | Path,
    workdir: str | Path,
    synthetic_sql: str | None = None,
) -> dict:
    """Prove the rollback path actually works, on a scratch copy.

    SQLite has no down-migrations, so the rollback mechanism for a schema change
    is "restore the pre-migration backup". That is a claim worth testing rather
    than assuming, so this: copies the live database, mutates the copy the way a
    migration would, restores the pristine copy over it, and checks the schema
    and the data both came back.

    The live database is never touched.
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    pristine = workdir / "drill-pristine.db"
    scratch = workdir / "drill-scratch.db"

    _copy_database(db_path, pristine)
    shutil.copy2(pristine, scratch)

    before_schema = _schema_snapshot(scratch)
    before_rows = _row_count(scratch, "apps")

    # Mutate the scratch copy the way a forward migration would.
    connection = sqlite3.connect(str(scratch))
    try:
        connection.executescript(synthetic_sql or DEFAULT_DRILL_SQL)
        connection.commit()
    finally:
        connection.close()

    mutated_schema = _schema_snapshot(scratch)
    changed = mutated_schema != before_schema

    # Roll back by restoring the pristine copy.
    shutil.copy2(pristine, scratch)
    restored_schema = _schema_snapshot(scratch)
    restored_rows = _row_count(scratch, "apps")

    schema_restored = restored_schema == before_schema
    rows_restored = restored_rows == before_rows

    return {
        "ok": bool(changed and schema_restored and rows_restored),
        "migration_changed_schema": changed,
        "schema_restored": schema_restored,
        "rows_preserved": rows_restored,
        "rows": before_rows,
        "scratch": str(scratch),
        "pristine": str(pristine),
    }


def run_migrations(db: Database, migrations_dir: str | Path) -> list[str]:
    """Apply any pending migrations. Returns the names of migrations applied."""
    directory = Path(migrations_dir)
    if not directory.is_dir():
        raise FileNotFoundError(f"migrations directory not found: {directory}")

    with db.write() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    INTEGER PRIMARY KEY,
                name       TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )

    already = applied_versions(db)
    applied_now: list[str] = []

    for path in sorted(directory.glob("*.sql")):
        match = re.match(r"(\d+)", path.name)
        if not match:
            continue
        version = int(match.group(1))
        if version in already:
            continue

        statements = split_statements(path.read_text(encoding="utf-8"))
        with db.write() as conn:
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (version, path.name, utcnow_iso()),
            )
        applied_now.append(path.name)

    return applied_now
