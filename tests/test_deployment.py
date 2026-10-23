"""Multi-worker deployment: what this build supports, and what it says when it does not.

Two things are in-process and assume one process — the rate limiter and the
scheduler. Neither is broken in the shape this is built for. The problem was that
nothing *said so*: the rate limiter's docstring mentions it, and a docstring is
not where an operator looks.
"""

from __future__ import annotations

import pytest

from helpers import ADMIN_PASSWORD
from powerbase import deployment
from powerbase.config import Settings


def settings_with(**over) -> Settings:
    base = Settings()
    for key, value in over.items():
        setattr(base, key, value)
    return base


# ------------------------------------------------------------------ the shape
def test_one_process_is_silent():
    """The supported shape must produce no noise, or the warning becomes wallpaper."""
    assert deployment.warnings_for(settings_with(workers=1)) == []


def test_the_summary_says_it_is_supported():
    summary = deployment.summarise(settings_with(workers=1))
    assert summary["single_process"] is True
    assert summary["supported_workers"] == 1
    assert summary["warnings"] == []


@pytest.mark.parametrize("workers", [2, 3, 8, 32])
def test_more_than_one_process_warns(workers):
    warnings = deployment.warnings_for(settings_with(workers=workers))
    assert warnings, f"{workers} workers produced no warning"
    assert str(workers) in warnings[0]


def test_the_warning_names_the_arithmetic_not_just_the_problem():
    """Saying "rate limits are per process" is not enough — the operator needs
    to know what their configured number actually became."""
    settings = settings_with(workers=4, project_rate_per_second=10.0, project_rate_burst=60)
    text = " ".join(deployment.warnings_for(settings))
    assert "40.0/s" in text, text
    assert "240 burst" in text, text


def test_the_summary_reports_the_effective_limits():
    summary = deployment.summarise(
        settings_with(workers=4, project_rate_per_second=10.0, project_rate_burst=60)
    )
    assert summary["rate_limit_scope"] == "process"
    assert summary["effective_project_rate_per_second"] == 40.0
    assert summary["effective_project_rate_burst"] == 240


def test_the_effective_limits_are_never_scaled_below_one():
    """A misconfigured zero must not turn into a zero rate limit."""
    summary = deployment.summarise(settings_with(workers=0, project_rate_per_second=10.0))
    assert summary["effective_project_rate_per_second"] == 10.0
    assert summary["single_process"] is True


# --------------------------------------------------------------- the scheduler
def test_with_several_workers_the_scheduler_must_be_off():
    warnings = deployment.warnings_for(
        settings_with(workers=4, maintenance_enabled=True)
    )
    text = " ".join(warnings)
    assert "4 schedulers" in text
    assert "POWERBASE_MAINTENANCE_ENABLED=0" in text
    assert "cron" in text


def test_with_several_workers_and_no_scheduler_it_says_that_is_right():
    warnings = deployment.warnings_for(
        settings_with(workers=4, maintenance_enabled=False)
    )
    assert any("right choice" in w for w in warnings)


# ------------------------------------------------------------- the environment
def test_the_worker_count_comes_from_the_environment(monkeypatch):
    monkeypatch.delenv("POWERBASE_WORKERS", raising=False)
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    assert Settings.from_env().workers == 1

    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    assert Settings.from_env().workers == 4, "uvicorn and gunicorn set this"


def test_our_variable_wins_over_the_standard_one(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    monkeypatch.setenv("POWERBASE_WORKERS", "2")
    assert Settings.from_env().workers == 2


def test_an_explicit_one_overrides_the_standard_variable(monkeypatch):
    """So an operator can say "I know, it is one process" and be believed."""
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    monkeypatch.setenv("POWERBASE_WORKERS", "1")
    assert Settings.from_env().workers == 1


def test_a_nonsense_worker_count_is_not_fatal(monkeypatch):
    monkeypatch.setenv("POWERBASE_WORKERS", "lots")
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    # Falls through to the default rather than refusing to start.
    assert Settings.from_env().workers == 1


# ------------------------------------------------------------------ the surface
def test_the_status_endpoint_carries_it(make_env):
    """Visible without reading logs, which is the point."""
    env = make_env(admin_password=ADMIN_PASSWORD)
    body = env.client.get("/v1/status", headers={"X-Admin-Password": ADMIN_PASSWORD})
    assert body.status_code == 200, body.text
    deployment_block = body.json()["deployment"]
    assert deployment_block["supported_workers"] == 1
    assert deployment_block["rate_limit_scope"] == "process"
