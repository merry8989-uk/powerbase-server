"""Configuration loading.

Everything comes from environment variables so the same image runs in dev and
production without code changes. ``Settings.from_env()`` is the only constructor
the application uses.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    db_path: str = "./powerbase.db"
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"

    rate_burst: int = 120
    rate_per_second: float = 40.0

    # Per-project limits, applied on top of the per-key bucket. One noisy
    # project must not be able to spend another project's headroom.
    project_rate_burst: int = 600
    project_rate_per_second: float = 100.0

    max_batch_size: int = 1000

    redaction_enabled: bool = True

    # Observability ingestion (Step 1). Caps how many attribute keys one log
    # event may carry; anything past the cap is dropped and reported back.
    log_events_max_attributes: int = 64

    # Export (Step 3). Where result files live, how many rows one export may
    # carry, and how long a download link stays good.
    export_dir: str = "./exports"
    export_max_rows: int = 50_000
    export_ttl_seconds: int = 7 * 24 * 60 * 60

    # OTLP/HTTP ingestion (Step 4). The protocol's own body limit is generous;
    # this is ours.
    max_otlp_bytes: int = 4 * 1024 * 1024

    # Dashboard (Phase 3). The dashboard is disabled until a password is set.
    admin_password: str | None = None
    session_ttl_seconds: int = 12 * 60 * 60

    # Maintenance (Phase 4). Off by default in the dataclass so a test or a
    # library embed never starts a background thread; `from_env` turns it on
    # for a real deployment.
    maintenance_enabled: bool = False
    rollup_hour: int = 1  # UTC hour at which the nightly jobs run
    maintenance_interval_seconds: int = 300
    backup_dir: str = "./backups"
    backup_keep: int = 7
    # How long our own housekeeping records are kept: alert deliveries that
    # have been sent or given up on, and log-drain runs. Separate from the
    # per-project retention above, because these are not observability data
    # and are not folded into any rollup.
    operational_retention_days: int = 30

    # How many processes serve this instance. This build expects one, and
    # `powerbase/deployment.py` explains what goes wrong when it is not.
    # Read from POWERBASE_WORKERS, then WEB_CONCURRENCY (what uvicorn and
    # gunicorn set), then 1.
    workers: int = 1
    vacuum_after_prune: bool = True

    # Alerts (Phase 5).
    alerts_enabled: bool = True
    alert_window_hours: int = 24
    crash_free_rate_warning: float = 0.99
    crash_free_rate_critical: float = 0.95
    error_rate_warning: float = 0.05
    error_rate_critical: float = 0.15
    silent_minutes: int = 24 * 60
    rollup_stale_days: int = 2
    min_samples_for_rates: int = 20
    alert_webhook_url: str | None = None
    # Step 5. How long an open incident stays quiet before it reminds you again,
    # and how many times a delivery is retried before it is given up on.
    alert_cooldown_minutes: int = 60
    alert_delivery_attempts: int = 5

    # Email delivery. Unset smtp_host means the email channel is unavailable and
    # a delivery aimed at it fails with a clear reason rather than silently.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_starttls: bool = True

    # Team access. The admin password stays as break-glass, so a lost owner
    # account is recoverable from the host.
    user_session_ttl_seconds: int = 12 * 60 * 60
    invite_ttl_seconds: int = 7 * 24 * 60 * 60

    # Logging (Phase 5).
    log_json: bool = False
    access_log: bool = True

    # How long a successful API-key verification is remembered, so the hot path
    # is not paying Argon2 on every request. 0 disables the cache.
    auth_cache_seconds: int = 300

    # Migrations directory, resolved relative to the package parent by default.
    migrations_dir: str = field(
        default_factory=lambda: str(Path(__file__).resolve().parent.parent / "migrations")
    )

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=os.environ.get("POWERBASE_DB", "./powerbase.db"),
            host=os.environ.get("POWERBASE_HOST", "0.0.0.0"),
            port=_env_int("POWERBASE_PORT", 8080),
            log_level=os.environ.get("POWERBASE_LOG_LEVEL", "INFO"),
            rate_burst=_env_int("POWERBASE_RATE_BURST", 120),
            rate_per_second=float(os.environ.get("POWERBASE_RATE_PER_SECOND", "40")),
            project_rate_burst=_env_int("POWERBASE_PROJECT_RATE_BURST", 600),
            project_rate_per_second=float(
                os.environ.get("POWERBASE_PROJECT_RATE_PER_SECOND", "100")
            ),
            max_batch_size=_env_int("POWERBASE_MAX_BATCH_SIZE", 1000),
            redaction_enabled=not _env_bool("POWERBASE_DISABLE_REDACTION", False),
            log_events_max_attributes=_env_int("POWERBASE_LOG_EVENTS_MAX_ATTRIBUTES", 64),
            export_dir=os.environ.get("POWERBASE_EXPORT_DIR", "./exports"),
            export_max_rows=_env_int("POWERBASE_EXPORT_MAX_ROWS", 50_000),
            export_ttl_seconds=_env_int(
                "POWERBASE_EXPORT_TTL_SECONDS", 7 * 24 * 60 * 60
            ),
            max_otlp_bytes=_env_int("POWERBASE_MAX_OTLP_BYTES", 4 * 1024 * 1024),
            admin_password=os.environ.get("POWERBASE_ADMIN_PASSWORD") or None,
            session_ttl_seconds=_env_int("POWERBASE_SESSION_TTL_SECONDS", 12 * 60 * 60),
            maintenance_enabled=_env_bool("POWERBASE_MAINTENANCE_ENABLED", True),
            rollup_hour=_env_int("POWERBASE_ROLLUP_HOUR", 1),
            maintenance_interval_seconds=_env_int("POWERBASE_MAINTENANCE_INTERVAL_SECONDS", 300),
            backup_dir=os.environ.get("POWERBASE_BACKUP_DIR", "./backups"),
            backup_keep=_env_int("POWERBASE_BACKUP_KEEP", 7),
            operational_retention_days=_env_int(
                "POWERBASE_OPERATIONAL_RETENTION_DAYS", 30
            ),
            workers=_env_int("POWERBASE_WORKERS", 0)
            or _env_int("WEB_CONCURRENCY", 1),
            vacuum_after_prune=_env_bool("POWERBASE_VACUUM_AFTER_PRUNE", True),
            alerts_enabled=_env_bool("POWERBASE_ALERTS_ENABLED", True),
            alert_window_hours=_env_int("POWERBASE_ALERT_WINDOW_HOURS", 24),
            crash_free_rate_warning=_env_float("POWERBASE_CRASH_FREE_WARNING", 0.99),
            crash_free_rate_critical=_env_float("POWERBASE_CRASH_FREE_CRITICAL", 0.95),
            error_rate_warning=_env_float("POWERBASE_ERROR_RATE_WARNING", 0.05),
            error_rate_critical=_env_float("POWERBASE_ERROR_RATE_CRITICAL", 0.15),
            silent_minutes=_env_int("POWERBASE_SILENT_MINUTES", 24 * 60),
            rollup_stale_days=_env_int("POWERBASE_ROLLUP_STALE_DAYS", 2),
            min_samples_for_rates=_env_int("POWERBASE_MIN_SAMPLES_FOR_RATES", 20),
            alert_webhook_url=os.environ.get("POWERBASE_ALERT_WEBHOOK_URL") or None,
            alert_cooldown_minutes=_env_int("POWERBASE_ALERT_COOLDOWN_MINUTES", 60),
            alert_delivery_attempts=_env_int("POWERBASE_ALERT_DELIVERY_ATTEMPTS", 5),
            smtp_host=os.environ.get("POWERBASE_SMTP_HOST") or None,
            smtp_port=_env_int("POWERBASE_SMTP_PORT", 587),
            smtp_user=os.environ.get("POWERBASE_SMTP_USER") or None,
            smtp_password=os.environ.get("POWERBASE_SMTP_PASSWORD") or None,
            smtp_from=os.environ.get("POWERBASE_SMTP_FROM") or None,
            smtp_starttls=not _env_bool("POWERBASE_SMTP_NO_STARTTLS", False),
            user_session_ttl_seconds=_env_int(
                "POWERBASE_USER_SESSION_TTL_SECONDS", 12 * 60 * 60
            ),
            invite_ttl_seconds=_env_int("POWERBASE_INVITE_TTL_SECONDS", 7 * 24 * 60 * 60),
            log_json=_env_bool("POWERBASE_LOG_JSON", False),
            access_log=_env_bool("POWERBASE_ACCESS_LOG", True),
            auth_cache_seconds=_env_int("POWERBASE_AUTH_CACHE_SECONDS", 300),
        )

    @property
    def dashboard_enabled(self) -> bool:
        return bool(self.admin_password)
