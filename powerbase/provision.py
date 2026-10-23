"""Provisioning helpers used by the CLI scripts and by tests.

Kept separate from the request path so nothing in the API can create apps or
issue keys — that only happens out-of-band.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .db import Database
from .migrations import utcnow_iso
from .security import format_token, hash_secret, new_key_id, new_secret


def provision_app(
    db: Database,
    app_id: str,
    name: str,
    platform: str = "android",
    retention_days: int = 90,
    sampling_rate: float = 1.0,
) -> None:
    with db.write() as conn:
        conn.execute(
            """
            INSERT INTO apps (id, name, platform, retention_days, sampling_rate, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                platform = excluded.platform,
                retention_days = excluded.retention_days,
                sampling_rate = excluded.sampling_rate
            """,
            (app_id, name, platform, retention_days, sampling_rate, utcnow_iso()),
        )


def _expiry_iso(expires_in_days: int | None) -> str | None:
    if expires_in_days is None:
        return None
    when = datetime.now(timezone.utc) + timedelta(days=expires_in_days)
    return when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def mint_key(
    db: Database,
    app_id: str,
    scope: str,
    label: str | None = None,
    expires_in_days: int | None = None,
    rotated_from: str | None = None,
) -> tuple[str, str]:
    """Create a key and return ``(key_id, plaintext_token)``.

    The token is the only time the secret exists in plaintext; the database
    holds an Argon2 hash of it and nothing else.
    """
    if scope not in {"ingest", "read"}:
        raise ValueError("scope must be 'ingest' or 'read'")
    key_id = new_key_id()
    secret = new_secret()
    with db.write() as conn:
        conn.execute(
            """
            INSERT INTO api_keys
                (id, app_id, key_hash, scope, label, created_at, expires_at, rotated_from)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key_id,
                app_id,
                hash_secret(secret),
                scope,
                label,
                utcnow_iso(),
                _expiry_iso(expires_in_days),
                rotated_from,
            ),
        )
    return key_id, format_token(key_id, secret)


def issue_key(db: Database, app_id: str, scope: str, label: str | None = None) -> str:
    """Create an API key and return the plaintext token (shown exactly once)."""
    return mint_key(db, app_id, scope, label)[1]


def rotate_key(
    db: Database,
    key_id: str,
    expires_in_days: int | None = None,
) -> tuple[str, str] | None:
    """Issue a replacement for a live key, then revoke the old one.

    Mints before revoking so there is no window in which neither key works — a
    deploy can pick up the new token while the old one is still briefly valid.
    Returns ``None`` if the key is unknown or already revoked.
    """
    row = db.one(
        "SELECT app_id, scope, label, revoked_at FROM api_keys WHERE id = ?", (key_id,)
    )
    if row is None or row["revoked_at"] is not None:
        return None
    new_key_id, token = mint_key(
        db,
        row["app_id"],
        row["scope"],
        row["label"],
        expires_in_days=expires_in_days,
        rotated_from=key_id,
    )
    revoke_key(db, key_id)
    return new_key_id, token


def list_keys(db: Database, app_id: str | None = None) -> list[dict]:
    """Key metadata for the console. Never returns a hash or a secret."""
    sql = """
        SELECT id, app_id, scope, label, created_at, last_used_at,
               expires_at, revoked_at, rotated_from
        FROM api_keys
    """
    params: tuple = ()
    if app_id:
        sql += " WHERE app_id = ?"
        params = (app_id,)
    sql += " ORDER BY created_at DESC, id DESC"
    return [dict(row) for row in db.query(sql, params)]


def revoke_key(db: Database, key_id: str) -> bool:
    with db.write() as conn:
        cur = conn.execute(
            "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (utcnow_iso(), key_id),
        )
        return cur.rowcount == 1
