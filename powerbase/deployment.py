"""Deployment shapes this build does not support, said out loud.

Two things live inside the process and assume there is only one process:

* **Rate limiting** counts per project in memory. With N workers a client gets N
  independent budgets, so the configured limit is effectively N times what it
  says. Nothing breaks — the numbers are just wrong, quietly.
* **The scheduler** runs the nightly rollup, prune and backup. With N workers, N
  schedulers run. The job ledger interlocks the *daily* run so it does not happen
  twice, but the interval passes each run, and the interlock is the only thing
  standing between a multi-worker deployment and duplicated work.

Neither is a bug in a single-process deployment, which is what this build is for.
The problem was that nothing *said so*: the rate limiter's own docstring
mentions it, and a docstring is not where an operator looks.

This module is the thing that says so — a pure function, so it can be tested
without starting a server.
"""

from __future__ import annotations

from .config import Settings

#: Environment variables that mean "more than one worker", in the order they are
#: checked. `WEB_CONCURRENCY` is what uvicorn and gunicorn read; `POWERBASE_WORKERS`
#: is ours, and wins so an operator can be explicit.
WORKER_ENV_VARS = ("POWERBASE_WORKERS", "WEB_CONCURRENCY")


def warnings_for(settings: Settings) -> list[str]:
    """What is wrong with this deployment shape, in plain words.

    Returns an empty list for the supported shape — one process — which is the
    case that must stay silent.
    """
    if settings.workers <= 1:
        return []

    workers = settings.workers
    out = [
        f"{workers} workers are configured. This build expects one process.",
        f"Rate limits are counted per process, so each project gets {workers}x "
        f"the configured budget ({settings.project_rate_per_second}/s and "
        f"{settings.project_rate_burst} burst becomes "
        f"{settings.project_rate_per_second * workers}/s and "
        f"{settings.project_rate_burst * workers} burst in total).",
    ]

    if settings.maintenance_enabled:
        out.append(
            f"The in-process scheduler is on, so {workers} schedulers will run. "
            "The job ledger stops the daily pass happening twice, but every "
            "interval pass runs once per worker. Set "
            "POWERBASE_MAINTENANCE_ENABLED=0 and drive "
            "scripts/maintenance.py from cron, or run one worker."
        )
    else:
        out.append(
            "The in-process scheduler is off, which is the right choice for "
            "this shape — keep driving scripts/maintenance.py from cron."
        )

    return out


def summarise(settings: Settings) -> dict:
    """The same thing, for a status endpoint rather than a log line."""
    return {
        "workers": settings.workers,
        "supported_workers": 1,
        "single_process": settings.workers <= 1,
        "rate_limit_scope": "process",
        "effective_project_rate_per_second": (
            settings.project_rate_per_second * max(settings.workers, 1)
        ),
        "effective_project_rate_burst": (
            settings.project_rate_burst * max(settings.workers, 1)
        ),
        "scheduler_in_process": settings.maintenance_enabled,
        "warnings": warnings_for(settings),
    }
