"""Retention for observability events.

The property under test is the one the whole prune design exists to protect:
**never delete a row whose day was never rolled up.** Raw rows older than the
retention window go; the days before the first rolled-up day stay, because their
history exists nowhere else.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from helpers import APP_ID, set_retention
from powerbase import maintenance, repository, rollups
from powerbase.models import LogEventIn, LogEventsBatch

START = date(2026, 1, 4)
DAYS = 7
PER_DAY = 3


def day_str(offset: int) -> str:
    return (START + timedelta(days=offset)).isoformat()


def seed_log_events(db, app_id=APP_ID, offsets=range(DAYS)) -> int:
    """Three events a day at 06:00, 12:00 and 18:00 UTC."""
    total = 0
    for offset in offsets:
        day = START + timedelta(days=offset)
        events = [
            LogEventIn(
                event_id=f"le-{offset}-{i}",
                ts=datetime(day.year, day.month, day.day, 6 + i * 6, tzinfo=timezone.utc),
                severity=["INFO", "ERROR", "WARN"][i],
                event_type="http.request",
                service_name="checkout-api",
                status_code=500 if i == 1 else 200,
            )
            for i in range(PER_DAY)
        ]
        repository.write_log_events(
            db,
            app_id,
            LogEventsBatch(batch_id=f"le-batch-{offset}", app_id=app_id, events=events),
            redact=True,
        )
        total += len(events)
    return total


def count_log_events(db, app_id=APP_ID) -> int:
    return db.scalar("SELECT COUNT(*) FROM log_events WHERE app_id = ?", (app_id,)) or 0


# ------------------------------------------------------------------- rollups
def test_the_daily_rollup_folds_log_events_in(env):
    seed_log_events(env.db)
    for offset in range(DAYS):
        rollups.rollup_day(env.db, day_str(offset))

    rows = env.db.query(
        "SELECT day, value FROM metrics_daily WHERE metric = 'log_events' AND app_id = ? ORDER BY day",
        (APP_ID,),
    )
    assert [r["day"] for r in rows] == [day_str(i) for i in range(DAYS)]
    assert all(r["value"] == PER_DAY for r in rows)

    errors = env.db.query(
        "SELECT value FROM metrics_daily WHERE metric = 'log_events_errors' AND app_id = ?",
        (APP_ID,),
    )
    assert [r["value"] for r in errors] == [1] * DAYS


def test_the_rollup_breaks_log_events_down_by_severity(env):
    seed_log_events(env.db)
    rollups.rollup_day(env.db, day_str(0))

    rows = env.db.query(
        """
        SELECT dimension, value FROM metrics_daily
         WHERE metric = 'log_events_by_severity' AND app_id = ? ORDER BY dimension
        """,
        (APP_ID,),
    )
    assert {r["dimension"]: r["value"] for r in rows} == {"ERROR": 1, "INFO": 1, "WARN": 1}


def test_log_events_do_not_disturb_the_analytics_metrics(env):
    seed_log_events(env.db)
    rollups.rollup_day(env.db, day_str(0))

    # No analytics rows were seeded, so those metrics are legitimately zero —
    # but they must exist, and the log_events rollup must not have replaced them.
    metrics = {
        r["metric"]
        for r in env.db.query(
            "SELECT DISTINCT metric FROM metrics_daily WHERE app_id = ?", (APP_ID,)
        )
    }
    assert "log_events" in metrics
    assert "events" in metrics
    assert "sessions" in metrics


# --------------------------------------------------------------------- prune
def test_prune_deletes_log_events_inside_the_window(env):
    seed_log_events(env.db)
    for offset in range(DAYS):
        rollups.rollup_day(env.db, day_str(offset))
    set_retention(env.db, 3)

    report = maintenance.prune(env.db, now=datetime(2026, 1, 11, 12, tzinfo=timezone.utc))

    assert report["skipped"] is False
    # Window is [day 0, day 4), so four days' worth goes.
    assert report["deleted"][APP_ID]["log_events"] == 4 * PER_DAY
    assert count_log_events(env.db) == DAYS * PER_DAY - 4 * PER_DAY


def test_prune_keeps_days_that_were_never_rolled_up(env):
    """The safety property: days before the first rolled-up day survive.

    Those days were never folded into metrics_daily, so deleting them would
    destroy history that exists nowhere else — even though they are older than
    the retention window.
    """
    seed_log_events(env.db)
    # Only the later days are rolled up, so the floor sits at day 3.
    for offset in range(3, DAYS):
        rollups.rollup_day(env.db, day_str(offset))
    set_retention(env.db, 3)

    report = maintenance.prune(env.db, now=datetime(2026, 1, 11, 12, tzinfo=timezone.utc))

    assert report["cutoff_floor"] == day_str(3)
    # Only day 3 is inside [floor, cutoff).
    assert report["deleted"][APP_ID]["log_events"] == PER_DAY

    survivors = {
        row["event_id"].split("-")[1]
        for row in env.db.query("SELECT event_id FROM log_events WHERE app_id = ?", (APP_ID,))
    }
    assert survivors == {"0", "1", "2", "4", "5", "6"}


def test_prune_is_a_no_op_when_nothing_has_been_rolled_up(env):
    seed_log_events(env.db)
    set_retention(env.db, 1)

    report = maintenance.prune(env.db, now=datetime(2026, 1, 11, 12, tzinfo=timezone.utc))

    assert report["skipped"] is True
    assert count_log_events(env.db) == DAYS * PER_DAY


def test_the_long_range_chart_survives_the_prune(env):
    """What the rollup is for: the numbers outlive the rows."""
    seed_log_events(env.db)
    for offset in range(DAYS):
        rollups.rollup_day(env.db, day_str(offset))
    set_retention(env.db, 2)

    maintenance.prune(env.db, now=datetime(2026, 1, 11, 12, tzinfo=timezone.utc))
    assert count_log_events(env.db) == 2 * PER_DAY  # only the newest days remain

    total = env.db.scalar(
        "SELECT SUM(value) FROM metrics_daily WHERE metric = 'log_events' AND app_id = ?",
        (APP_ID,),
    )
    assert total == DAYS * PER_DAY  # the full week is still accounted for


def test_prune_records_what_it_did(env):
    seed_log_events(env.db)
    rollups.rollup_day(env.db, day_str(0))
    set_retention(env.db, 1)

    maintenance.prune(env.db, now=datetime(2026, 1, 11, 12, tzinfo=timezone.utc))

    ledger = env.db.query("SELECT job, ok, detail FROM job_runs WHERE job = 'prune'")
    assert ledger and ledger[-1]["ok"] == 1


# --------------------------------------------------------- the nightly pass
def test_the_nightly_pass_reports_export_housekeeping(env):
    report = maintenance.run_daily(
        env.db,
        backup_dir=str(env.settings.db_path) + "-backups",
        now=datetime(2026, 1, 11, 1, tzinfo=timezone.utc),
    )

    assert "exports" in report
    assert "completed" in report["exports"]
    assert "expired" in report["exports"]
    assert report["exports"]["completed"]["queued"] == 0


def test_the_nightly_pass_expires_stale_exports(env, tmp_path):
    from powerbase import exports

    env.settings.export_dir = str(tmp_path / "exports")
    env.settings.export_max_rows = 10
    repository.write_log_events(
        env.db,
        APP_ID,
        LogEventsBatch(
            batch_id="nightly-1",
            app_id=APP_ID,
            events=[LogEventIn(event_id="n1", ts="2026-01-04T06:00:00Z", event_type="x")],
        ),
        redact=True,
    )

    job = exports.create_job(
        env.db, env.settings, app_id=APP_ID, fmt="csv", definition={"filters": []}
    )
    exports.run_job(env.db, env.settings, job["id"])

    with env.db.write() as conn:
        conn.execute(
            "UPDATE export_jobs SET expires_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00.000000Z", job["id"]),
        )

    report = maintenance.run_daily(
        env.db, backup_dir=str(tmp_path / "backups"), now=datetime(2026, 1, 11, 1, tzinfo=timezone.utc)
    )

    assert report["exports"]["expired"]["expired"] == 1
    assert report["exports"]["expired"]["files_removed"] == 1
    assert not (tmp_path / "exports" / f"{job['id']}.csv").exists()
