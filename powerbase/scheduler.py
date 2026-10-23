"""A very small in-process scheduler for the nightly maintenance pass.

Deliberately not APScheduler. The requirement is "run one pass a day at about
01:00 UTC, and don't run it twice" — a background thread with a stop event does
that in forty lines and adds no dependency to a tool whose whole premise is one
process over one file.

The process must be long-lived for this to fire (it is a server, so it is), and
`scripts/maintenance.py` covers the case where it is not: point cron at that
instead and set `POWERBASE_MAINTENANCE_ENABLED=0`.
"""

from __future__ import annotations

import threading
import traceback
from datetime import datetime, timezone

from . import alerts, drains, incidents, notify, maintenance, rollups
from .config import Settings
from .db import Database


class MaintenanceScheduler:
    def __init__(self, db: Database, settings: Settings, logger=None):
        self._db = db
        self._settings = settings
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._logger = logger or (lambda message: None)
        self.last_report: dict | None = None
        self.last_alerts: alerts.AlertReport | None = None
        self.last_error: str | None = None

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="powerbase-maintenance", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------- the loop
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # a failing job must not kill the thread
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._logger(f"maintenance tick failed: {self.last_error}\n{traceback.format_exc()}")
            self._stop.wait(self._settings.maintenance_interval_seconds)

    def tick(self, now: datetime | None = None) -> dict | None:
        """Run the daily pass if it is due. Returns the report, or None."""
        moment = now or datetime.now(timezone.utc)
        if moment.hour < self._settings.rollup_hour:
            return None

        today = moment.strftime("%Y-%m-%d")
        # Guard each half separately so a failed backup is retried on the next
        # tick rather than being skipped until tomorrow.
        rollup_done = rollups.ran_on(self._db, "rollup", today)
        backup_done = rollups.ran_on(self._db, "backup", today)
        if rollup_done and backup_done:
            return None

        report = maintenance.run_daily(
            self._db,
            backup_dir=self._settings.backup_dir,
            backup_keep=self._settings.backup_keep,
            vacuum=self._settings.vacuum_after_prune,
            now=moment,
        )
        self.last_report = report
        self.last_error = None
        self._logger(
            f"maintenance: day={report['day']} "
            f"rollup_rows={report['rollup']['rows']} "
            f"pruned={report['prune'].get('total', 0)} "
            f"backup_ok={report['backup']['verified']}"
        )

        # Alerts are evaluated after the jobs, so a job that just failed is
        # reported in the same pass rather than a day later.
        report_alerts = alerts.evaluate(self._db, self._settings, now=moment)
        self.last_alerts = report_alerts
        if not report_alerts.ok:
            self._logger(
                f"alerts: {report_alerts.worst} — "
                + "; ".join(alert.title for alert in report_alerts.alerts)
            )

        # Fold the evaluation into incidents. This is what stops a continuing
        # problem from re-notifying on every pass: the incident is opened once,
        # then reminded on its own cooldown.
        notify.ensure_legacy_webhook(self._db, self._settings)
        folded = incidents.apply_report(self._db, self._settings, report_alerts, now=moment)
        if folded["opened"] or folded["resolved"]:
            self._logger(
                f"incidents: opened={folded['opened']} resolved={folded['resolved']} "
                f"queued={folded['queued']}"
            )

        # Then send whatever is due. A failure here is retried on a later pass
        # rather than lost, and gives up visibly.
        delivered = notify.flush(self._db, self._settings, now=moment)
        if delivered["dead"]:
            self._logger(f"alert deliveries gave up after retries: {delivered['dead']}")

        report["alerts"] = {
            "report": report_alerts.to_dict(),
            "incidents": folded,
            "deliveries": delivered,
        }

        # Drains forward what arrived to wherever else it needs to be. A failure
        # leaves the cursor where it was, so the next pass retries the same
        # batch rather than skipping it.
        drained = drains.flush(self._db, self._settings, now=moment)
        if drained["failed"]:
            self._logger(f"log drains: {drained['failed']} of {drained['ran']} failed")
        report["drains"] = drained

        return report
