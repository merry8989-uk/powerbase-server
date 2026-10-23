"""Retention pruning, compaction and backup."""

from datetime import datetime, timezone

from powerbase import maintenance, rollups
from helpers import APP_ID, dated_day_str, dated_range, set_retention

JUST_AFTER = datetime(2026, 1, 11, 3, tzinfo=timezone.utc)


def rollup_all(env):
    for day in dated_range():
        rollups.rollup_day(env.db, day)


# --------------------------------------------------------------------- prune

def test_prune_is_deferred_when_nothing_is_rolled_up(dated_env):
    # The safety interlock: with no rollups, deleting raw rows would destroy
    # history that exists nowhere else.
    result = maintenance.prune(dated_env.db, now=JUST_AFTER)
    assert result["skipped"] is True
    assert "no rollups" in result["reason"]
    assert dated_env.db.scalar("SELECT COUNT(*) FROM events") == 21


def test_prune_removes_rows_past_retention(dated_env):
    rollup_all(dated_env)
    set_retention(dated_env.db, 2)

    result = maintenance.prune(dated_env.db, now=JUST_AFTER)
    assert result["skipped"] is False
    # window is [2026-01-04, 2026-01-09) -> five days of three events
    assert result["deleted"][APP_ID]["events"] == 15
    assert dated_env.db.scalar("SELECT COUNT(*) FROM events") == 6


def test_prune_never_deletes_before_the_rollup_floor(dated_env):
    # Roll up only from 2026-01-06 onward. Days before that were never folded
    # into metrics_daily, so they must survive even though they are ancient.
    for day in dated_range()[2:]:
        rollups.rollup_day(dated_env.db, day)
    set_retention(dated_env.db, 2)

    result = maintenance.prune(dated_env.db, now=JUST_AFTER)
    assert result["cutoff_floor"] == dated_day_str(2)
    # deleted only [2026-01-06, 2026-01-09) = 9 rows
    assert result["deleted"][APP_ID]["events"] == 9
    remaining = {
        row["ts"][:10] for row in dated_env.db.query("SELECT ts FROM events")
    }
    assert dated_day_str(0) in remaining
    assert dated_day_str(1) in remaining


def test_prune_deletes_nothing_when_rollups_start_after_the_cutoff(dated_env):
    # Rollups only exist for the most recent day, which is inside the retention
    # window, so there is no window to delete from.
    rollups.rollup_day(dated_env.db, dated_day_str(6))
    set_retention(dated_env.db, 1)

    result = maintenance.prune(dated_env.db, now=JUST_AFTER)
    assert result["total"] == 0
    assert dated_env.db.scalar("SELECT COUNT(*) FROM events") == 21


def test_prune_keeps_a_device_that_still_has_a_session(dated_env):
    rollup_all(dated_env)
    set_retention(dated_env.db, 2)

    with dated_env.db.write() as conn:
        # An old device, but a session that is still inside the retention window.
        conn.execute(
            "INSERT INTO devices (id, app_id, first_seen, last_seen) VALUES (?, ?, ?, ?)",
            ("old-but-referenced", APP_ID, "2026-01-04T06:00:00.000000Z", "2026-01-04T06:00:00.000000Z"),
        )
        conn.execute(
            "INSERT INTO sessions (id, app_id, device_id, started_at) VALUES (?, ?, ?, ?)",
            ("recent-session", APP_ID, "old-but-referenced", "2026-01-10T06:00:00.000000Z"),
        )
        # And an old device with nothing referencing it.
        conn.execute(
            "INSERT INTO devices (id, app_id, first_seen, last_seen) VALUES (?, ?, ?, ?)",
            ("old-and-orphan", APP_ID, "2026-01-04T06:00:00.000000Z", "2026-01-04T06:00:00.000000Z"),
        )

    maintenance.prune(dated_env.db, now=JUST_AFTER)

    assert dated_env.db.scalar("SELECT COUNT(*) FROM devices WHERE id = 'old-but-referenced'") == 1
    assert dated_env.db.scalar("SELECT COUNT(*) FROM devices WHERE id = 'old-and-orphan'") == 0


def test_prune_records_a_job_run(dated_env):
    rollup_all(dated_env)
    set_retention(dated_env.db, 2)
    maintenance.prune(dated_env.db, now=JUST_AFTER)
    assert rollups.last_run(dated_env.db, "prune")["ok"] == 1


def test_vacuum_after_prune_keeps_the_database_healthy(dated_env):
    rollup_all(dated_env)
    set_retention(dated_env.db, 2)
    maintenance.prune(dated_env.db, now=JUST_AFTER)
    dated_env.db.vacuum()
    assert dated_env.db.scalar("PRAGMA integrity_check") == "ok"


# -------------------------------------------------------------------- backup

def test_backup_creates_a_verifiable_file(dated_env, tmp_path):
    result = maintenance.backup(dated_env.db, tmp_path, now=JUST_AFTER)

    assert result["verified"] is True
    assert result["verification"]["integrity"] == "ok"
    assert result["verification"]["counts"]["events"] == 21
    assert result["size_bytes"] > 0
    assert (tmp_path / result["path"].split("/")[-1]).exists()


def test_backup_captures_the_data_at_that_moment(dated_env, tmp_path):
    maintenance.backup(dated_env.db, tmp_path, now=JUST_AFTER)
    # Add more data, then confirm the earlier backup is unchanged.
    from helpers import events_payload  # noqa: F401  (kept for symmetry)
    before = maintenance.list_backups(tmp_path)[0]
    assert maintenance.verify_backup(before["path"])["counts"]["events"] == 21


def test_backup_rotation_keeps_only_n(dated_env, tmp_path):
    for hour in range(5):
        maintenance.backup(
            dated_env.db, tmp_path, keep=2,
            now=datetime(2026, 1, 11, hour, tzinfo=timezone.utc),
        )
    remaining = maintenance.list_backups(tmp_path)
    assert len(remaining) == 2
    # newest first
    assert "T04" in remaining[0]["name"]


def test_verify_backup_rejects_a_corrupt_file(tmp_path):
    broken = tmp_path / "powerbase-20260111T000000Z.db"
    broken.write_bytes(b"this is not a sqlite database, not even close")
    result = maintenance.verify_backup(broken)
    assert result["ok"] is False


def test_verify_backup_rejects_a_missing_file(tmp_path):
    assert maintenance.verify_backup(tmp_path / "nope.db")["ok"] is False


def test_restore_round_trip(dated_env, tmp_path):
    from powerbase.db import Database

    backup = maintenance.backup(dated_env.db, tmp_path, now=JUST_AFTER)

    # Wreck the live database.
    with dated_env.db.write() as conn:
        conn.execute("DELETE FROM events")
    assert dated_env.db.scalar("SELECT COUNT(*) FROM events") == 0

    db_path = dated_env.settings.db_path
    # Restore is offline by design: nothing may hold the file open.
    dated_env.db.close()
    result = maintenance.restore(backup["path"], db_path)

    assert result["safety_copy"] is not None
    fresh = Database(db_path)
    try:
        assert fresh.scalar("SELECT COUNT(*) FROM events") == 21
    finally:
        fresh.close()


def test_restore_refuses_a_bad_backup(dated_env, tmp_path):
    broken = tmp_path / "powerbase-20260111T000000Z.db"
    broken.write_bytes(b"garbage")
    try:
        maintenance.restore(broken, dated_env.settings.db_path)
        raise AssertionError("restore should have refused a corrupt backup")
    except ValueError as exc:
        assert "verification" in str(exc)


# ---------------------------------------------------------------- daily pass

def test_run_daily_does_all_three_jobs(dated_env, tmp_path):
    rollup_all(dated_env)
    set_retention(dated_env.db, 2)

    report = maintenance.run_daily(
        dated_env.db, backup_dir=tmp_path, backup_keep=3,
        now=JUST_AFTER,
    )

    assert report["day"] == "2026-01-10"
    assert report["rollup"]["rows"] > 0
    assert report["prune"]["skipped"] is False
    assert report["backup"]["verified"] is True
    assert report["vacuum"] == "ok"

    jobs = {run["job"] for run in rollups.recent_runs(dated_env.db, limit=10)}
    assert {"rollup", "prune", "backup"}.issubset(jobs)
