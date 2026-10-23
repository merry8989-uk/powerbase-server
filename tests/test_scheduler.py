"""The in-process maintenance scheduler."""

from dataclasses import replace
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from powerbase import rollups
from powerbase.scheduler import MaintenanceScheduler

BEFORE_HOUR = datetime(2026, 1, 11, 0, 30, tzinfo=timezone.utc)
AFTER_HOUR = datetime(2026, 1, 11, 3, 0, tzinfo=timezone.utc)


def scheduler(env, tmp_path, **overrides):
    settings = replace(env.settings, backup_dir=str(tmp_path), **overrides)
    return MaintenanceScheduler(env.db, settings, logger=lambda message: None)


def test_nothing_runs_before_the_rollup_hour(dated_env, tmp_path):
    unit = scheduler(dated_env, tmp_path)  # rollup_hour defaults to 1
    assert unit.tick(now=BEFORE_HOUR) is None
    assert rollups.recent_runs(dated_env.db) == []


def test_tick_runs_the_daily_pass(dated_env, tmp_path):
    unit = scheduler(dated_env, tmp_path)
    report = unit.tick(now=AFTER_HOUR)

    assert report is not None
    assert report["backup"]["verified"] is True
    assert unit.last_report is report
    assert unit.last_error is None

    jobs = {run["job"] for run in rollups.recent_runs(dated_env.db)}
    assert {"rollup", "prune", "backup"}.issubset(jobs)


def test_the_second_tick_on_the_same_day_is_a_no_op(dated_env, tmp_path):
    unit = scheduler(dated_env, tmp_path)
    assert unit.tick(now=AFTER_HOUR) is not None
    assert unit.tick(now=AFTER_HOUR) is None


def test_it_runs_again_the_next_day(dated_env, tmp_path):
    unit = scheduler(dated_env, tmp_path)
    assert unit.tick(now=AFTER_HOUR) is not None
    tomorrow = datetime(2026, 1, 12, 3, 0, tzinfo=timezone.utc)
    assert unit.tick(now=tomorrow) is not None


def test_a_failed_backup_is_retried_on_the_next_tick(dated_env, tmp_path):
    unit = scheduler(dated_env, tmp_path)
    unit.tick(now=AFTER_HOUR)

    # Pretend the backup half failed: the guard must not skip it until tomorrow.
    with dated_env.db.write() as conn:
        conn.execute("UPDATE job_runs SET ok = 0 WHERE job = 'backup'")

    assert unit.tick(now=AFTER_HOUR) is not None


def test_start_and_stop(dated_env, tmp_path):
    unit = scheduler(dated_env, tmp_path, maintenance_interval_seconds=3600)
    assert unit.running is False
    unit.start()
    assert unit.running is True
    unit.stop()
    assert unit.running is False


def test_starting_twice_is_harmless(dated_env, tmp_path):
    unit = scheduler(dated_env, tmp_path, maintenance_interval_seconds=3600)
    unit.start()
    first = unit._thread
    unit.start()
    assert unit._thread is first
    unit.stop()


# ------------------------------------------------------- wiring into the app

def test_no_scheduler_is_started_by_default(make_env):
    env = make_env()
    with TestClient(env.app):
        assert env.app.state.scheduler is None


def test_scheduler_starts_and_stops_with_the_app(make_env, tmp_path):
    env = make_env(
        maintenance_enabled=True,
        backup_dir=str(tmp_path),
        maintenance_interval_seconds=3600,
    )
    with TestClient(env.app):
        assert env.app.state.scheduler is not None
        assert env.app.state.scheduler.running is True

    assert env.app.state.scheduler.running is False
