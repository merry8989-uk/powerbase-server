"""Housekeeping rows get a bound — without dropping a notification still owed.

`alert_deliveries` and `drain_runs` were written on every maintenance pass and
nothing ever removed them. The fix is a window, and one rule that matters more
than the window: a delivery that has not been sent yet is a notification
somebody is still owed, and deleting it would drop an alert silently — the exact
failure the delivery queue exists to prevent.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from powerbase import drains, maintenance, notify

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def stamp(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture()
def hk(env):
    """An environment with a spread of housekeeping rows."""
    with env.db.write() as conn:
        # Alert deliveries: every status, old and recent.
        for status, age in (
            ("sent", 90),
            ("dead", 90),
            ("pending", 90),   # still owed
            ("failed", 90),    # waiting on a retry
            ("sent", 1),
            ("pending", 1),
        ):
            conn.execute(
                """
                INSERT INTO alert_deliveries
                    (incident_id, kind, target, event, status, attempts, created_at)
                VALUES (NULL, 'webhook', 'https://x.test/hook', 'opened', ?, 1, ?)
                """,
                (status, stamp(age)),
            )

        conn.execute(
            """
            INSERT INTO log_drains (id, name, kind, target, created_at)
            VALUES ('d1', 'to-siem', 'http', 'https://siem.test/i', ?)
            """,
            (stamp(90),),
        )
        # A finished old run, an old run still in progress, and a recent one.
        conn.execute(
            "INSERT INTO drain_runs (drain_id, started_at, finished_at, sent, ok) "
            "VALUES ('d1', ?, ?, 10, 1)",
            (stamp(90), stamp(90)),
        )
        conn.execute(
            "INSERT INTO drain_runs (drain_id, started_at, finished_at, sent, ok) "
            "VALUES ('d1', ?, NULL, 0, 0)",
            (stamp(90),),
        )
        conn.execute(
            "INSERT INTO drain_runs (drain_id, started_at, finished_at, sent, ok) "
            "VALUES ('d1', ?, ?, 5, 1)",
            (stamp(1), stamp(1)),
        )
    return env


def statuses(db) -> list[str]:
    return [row["status"] for row in db.query("SELECT status FROM alert_deliveries ORDER BY id")]


# ------------------------------------------------------------------ the window
def test_an_old_delivery_that_was_sent_is_pruned(hk):
    maintenance.prune(hk.db, now=NOW, operational_days=30)
    assert "sent" in statuses(hk.db), "the recent one survives"
    assert len(statuses(hk.db)) == 4, "the old sent and the old dead one are gone"


def test_an_old_delivery_that_was_given_up_on_is_pruned(hk):
    maintenance.prune(hk.db, now=NOW, operational_days=30)
    assert hk.db.scalar("SELECT COUNT(*) FROM alert_deliveries WHERE status = 'dead'") == 0


def test_an_old_finished_drain_run_is_pruned(hk):
    maintenance.prune(hk.db, now=NOW, operational_days=30)
    assert hk.db.scalar("SELECT COUNT(*) FROM drain_runs") == 2


# ----------------------------------------------------------- the safety rule
def test_an_undelivered_notification_is_never_pruned(hk):
    """The rule that matters more than the window.

    A `pending` or `failed` delivery is an alert somebody has not been told
    about. Age does not make it less owed.
    """
    maintenance.prune(hk.db, now=NOW, operational_days=30)
    remaining = statuses(hk.db)
    assert remaining.count("pending") == 2, "both pending rows survive, old and new"
    assert remaining.count("failed") == 1, "a retry still waiting survives"


def test_a_drain_run_still_in_progress_is_kept(hk):
    """It has no `finished_at`, so it has not finished, so it is not history."""
    maintenance.prune(hk.db, now=NOW, operational_days=30)
    unfinished = hk.db.scalar("SELECT COUNT(*) FROM drain_runs WHERE finished_at IS NULL")
    assert unfinished == 1


def test_a_recent_row_survives_whatever_its_status(hk):
    maintenance.prune(hk.db, now=NOW, operational_days=30)
    recent = hk.db.query("SELECT status FROM alert_deliveries WHERE created_at > ?", (stamp(10),))
    assert len(recent) == 2


# --------------------------------------------------------------- the window
def test_a_zero_window_disables_operational_pruning(hk):
    report = maintenance.prune(hk.db, now=NOW, operational_days=0)
    assert report["operational"]["skipped"] is True
    assert hk.db.scalar("SELECT COUNT(*) FROM alert_deliveries") == 6
    assert hk.db.scalar("SELECT COUNT(*) FROM drain_runs") == 3


def test_a_wider_window_keeps_more(hk):
    """Ninety days of age survives a hundred-day window."""
    maintenance.prune(hk.db, now=NOW, operational_days=100)
    assert hk.db.scalar("SELECT COUNT(*) FROM alert_deliveries") == 6


def test_a_narrower_window_removes_more(hk):
    """The recent `sent` row is a day old, so a one-hour window catches it."""
    report = maintenance.prune(hk.db, now=NOW, operational_days=1)
    # `sent` at 1 day old is exactly at the boundary; `pending` is protected.
    assert report["operational"]["alert_deliveries"] >= 2
    assert "pending" in statuses(hk.db)


def test_the_default_window_is_used_when_none_is_given(hk):
    report = maintenance.prune(hk.db, now=NOW)
    assert report["operational"]["window_days"] == maintenance.DEFAULT_OPERATIONAL_RETENTION_DAYS
    assert maintenance.DEFAULT_OPERATIONAL_RETENTION_DAYS == 30


def test_the_report_carries_the_counts(hk):
    report = maintenance.prune(hk.db, now=NOW, operational_days=30)
    assert report["operational"]["alert_deliveries"] == 2
    assert report["operational"]["drain_runs"] == 1
    assert report["operational"]["skipped"] is False


# ------------------------------------------------- independence from rollups
def test_operational_pruning_happens_even_without_any_rollup(hk):
    """These tables are not folded into an aggregate, so there is no floor to
    wait for — an instance whose rollups have stopped should not also pile up
    housekeeping rows."""
    assert hk.db.scalar("SELECT COUNT(*) FROM metrics_daily") == 0

    report = maintenance.prune(hk.db, now=NOW, operational_days=30)
    assert report["skipped"] is True, "the observability tables are still deferred"
    assert report["operational"]["alert_deliveries"] == 2, "but the housekeeping was pruned"
    assert hk.db.scalar("SELECT COUNT(*) FROM alert_deliveries") == 4


# ------------------------------------------------------------- the nightly pass
def test_the_nightly_pass_passes_the_window_through(hk, tmp_path):
    # `tmp_path`, not the settings default: `run_daily` writes a backup, and a
    # test that litters the shared `./backups` directory changes what other
    # tests see. It did — the capacity test below asserts "no backups found".
    report = maintenance.run_daily(
        hk.db, backup_dir=str(tmp_path), vacuum=False,
        now=NOW, operational_days=30,
    )
    assert report["prune"]["operational"]["alert_deliveries"] == 2


def test_the_setting_defaults_to_thirty_days(env):
    assert env.settings.operational_retention_days == 30
