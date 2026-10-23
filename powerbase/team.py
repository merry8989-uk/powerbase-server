"""Users, roles, sessions and invites.

Until now the dashboard had exactly one credential: an admin password shared by
everyone. That is fine for one person and useless for a team — there is no way
to tell who did what, no way to give a colleague read-only access, and no way to
take access away without changing the password for everybody.

Four roles, and the permissions are a plain table rather than a hierarchy of
if-statements, so "what can a developer do" has one answer in one place.

The admin password does not go away. It becomes break-glass: it still works,
still maps to the owner role, and is the thing you use when the owner account is
locked out.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from . import audit
from .config import Settings
from .db import Database
from .migrations import utcnow_iso
from .security import hash_secret, verify_secret

ROLES = ("owner", "admin", "developer", "readonly")

#: permission -> the roles that hold it. Read it as the answer to
#: "who is allowed to do this", which is the question that actually gets asked.
PERMISSIONS: dict[str, set[str]] = {
    "read": {"owner", "admin", "developer", "readonly"},
    "export": {"owner", "admin", "developer"},
    "alerts:ack": {"owner", "admin", "developer"},
    "alerts:write": {"owner", "admin"},
    "keys:write": {"owner", "admin"},
    "users:read": {"owner", "admin"},
    "users:write": {"owner", "admin"},
    # Minting an admin is an owner's decision, not an admin's.
    "users:write_admin": {"owner"},
    "settings:write": {"owner", "admin"},
    "project:write": {"owner", "admin"},
}

MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15
MIN_PASSWORD_LENGTH = 12


class TeamError(ValueError):
    """A refusal the caller should see, not a crash.

    Carries a machine-readable ``reason`` because the HTTP status must not be
    decided by the wording of a message. It was, briefly: the lockout said "too
    many failed attempts" and the caller was matching on the substring "locked",
    so a lockout answered 401 instead of 429. Message text is for people.
    """

    def __init__(self, message: str, reason: str = "invalid") -> None:
        super().__init__(message)
        self.reason = reason


def can(role: str | None, permission: str) -> bool:
    if not role:
        return False
    allowed = PERMISSIONS.get(permission)
    if allowed is None:
        # An unknown permission is a programming error, and refusing is the only
        # safe way to be wrong about it.
        return False
    return role in allowed


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def normalise_email(email: str) -> str:
    """Lower-cased and trimmed, so `Amit@x.com` and `amit@x.com` are one account."""
    return (email or "").strip().lower()


def check_password_strength(password: str) -> None:
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise TeamError(f"password must be at least {MIN_PASSWORD_LENGTH} characters", reason="invalid")


def hash_token(token: str) -> str:
    """Session and invite tokens are hashed like passwords, and for the same reason."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
def create_user(
    db: Database,
    *,
    email: str,
    role: str,
    display_name: str | None = None,
    password: str | None = None,
    actor: str = "system",
) -> dict:
    if role not in ROLES:
        raise TeamError(f"role must be one of {', '.join(ROLES)}", reason="invalid")
    email = normalise_email(email)
    if "@" not in email or len(email) > 254:
        raise TeamError("a valid email address is required", reason="invalid")
    if password is not None:
        check_password_strength(password)
    if db.one("SELECT id FROM users WHERE email = ?", (email,)) is not None:
        raise TeamError(f"{email} already has an account", reason="conflict")

    user_id = uuid.uuid4().hex
    with db.write() as conn:
        conn.execute(
            """
            INSERT INTO users
                (id, email, display_name, password_hash, role, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id, email, display_name,
                hash_secret(password) if password else None,
                role, "active" if password else "invited", utcnow_iso(),
            ),
        )
        audit.record_in(
            conn, audit.USER_CREATED, actor=actor, target_type="user", target_id=user_id,
            metadata={"email": email, "role": role},
        )
    return get_user(db, user_id)  # type: ignore[return-value]


def ensure_owner(db: Database, email: str, password: str) -> dict:
    """Create the first owner if there is not one already. Safe to call twice."""
    existing = db.one("SELECT id FROM users WHERE role = 'owner' AND status = 'active' LIMIT 1")
    if existing is not None:
        return get_user(db, existing["id"])  # type: ignore[return-value]

    by_email = db.one("SELECT id FROM users WHERE email = ?", (normalise_email(email),))
    if by_email is not None:
        set_role(db, by_email["id"], "owner", actor="bootstrap")
        set_password(db, by_email["id"], password)
        enable(db, by_email["id"], actor="bootstrap")
        return get_user(db, by_email["id"])  # type: ignore[return-value]

    return create_user(db, email=email, role="owner", password=password, actor="bootstrap")


def get_user(db: Database, user_id: str) -> dict | None:
    row = db.one("SELECT * FROM users WHERE id = ?", (user_id,))
    return _public(row) if row is not None else None


def find_by_email(db: Database, email: str) -> dict | None:
    row = db.one("SELECT * FROM users WHERE email = ?", (normalise_email(email),))
    return dict(row) if row is not None else None


def list_users(db: Database, status: str | None = None) -> list[dict]:
    if status:
        rows = db.query(
            "SELECT * FROM users WHERE status = ? ORDER BY role, email", (status,)
        )
    else:
        rows = db.query("SELECT * FROM users ORDER BY role, email")
    return [_public(row) for row in rows]


def _public(row) -> dict:
    """A user as the outside world may see it. Never the hash."""
    out = dict(row)
    out.pop("password_hash", None)
    return out


def set_password(db: Database, user_id: str, password: str) -> bool:
    check_password_strength(password)
    with db.write() as conn:
        cursor = conn.execute(
            """
            UPDATE users
               SET password_hash = ?, status = 'active', failed_attempts = 0,
                   locked_until = NULL
             WHERE id = ?
            """,
            (hash_secret(password), user_id),
        )
    return cursor.rowcount > 0


def set_role(db: Database, user_id: str, role: str, actor: str = "system") -> dict | None:
    if role not in ROLES:
        raise TeamError(f"role must be one of {', '.join(ROLES)}", reason="invalid")
    user = get_user(db, user_id)
    if user is None:
        return None
    if user["role"] == "owner" and role != "owner" and _active_owners(db, exclude=user_id) == 0:
        raise TeamError("this is the last owner; promote someone else first", reason="conflict")
    with db.write() as conn:
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        audit.record_in(
            conn, audit.USER_ROLE_CHANGED, actor=actor, target_type="user", target_id=user_id,
            metadata={"from": user["role"], "to": role},
        )
    return get_user(db, user_id)


def _active_owners(db: Database, exclude: str | None = None) -> int:
    sql = "SELECT COUNT(*) FROM users WHERE role = 'owner' AND status = 'active'"
    params: tuple = ()
    if exclude:
        sql += " AND id != ?"
        params = (exclude,)
    return db.scalar(sql, params) or 0


def disable(db: Database, user_id: str, actor: str = "system") -> dict | None:
    user = get_user(db, user_id)
    if user is None:
        return None
    if user["role"] == "owner" and _active_owners(db, exclude=user_id) == 0:
        raise TeamError("this is the last active owner", reason="conflict")
    with db.write() as conn:
        conn.execute("UPDATE users SET status = 'disabled' WHERE id = ?", (user_id,))
        # A disabled user's sessions must die with the account, or "disabled"
        # means "disabled at the next login".
        conn.execute(
            "UPDATE user_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
            (utcnow_iso(), user_id),
        )
        audit.record_in(
            conn, audit.USER_DISABLED, actor=actor, target_type="user", target_id=user_id
        )
    return get_user(db, user_id)


def enable(db: Database, user_id: str, actor: str = "system") -> dict | None:
    with db.write() as conn:
        conn.execute(
            "UPDATE users SET status = 'active', failed_attempts = 0, locked_until = NULL "
            "WHERE id = ?",
            (user_id,),
        )
    return get_user(db, user_id)


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
def login(
    db: Database,
    settings: Settings,
    *,
    email: str,
    password: str,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Authenticate, with a lockout so a password can be guessed only slowly."""
    moment = now or datetime.now(timezone.utc)
    email = normalise_email(email)
    user = find_by_email(db, email)

    # One message for every failure. Saying "no such user" tells an attacker
    # which half of the guess was right.
    refused = TeamError("email or password is incorrect", reason="credentials")

    if user is None:
        audit.record(db, audit.USER_LOGIN_FAILED, actor=email, outcome="denied",
                     metadata={"reason": "unknown_account"})
        raise refused

    locked_until = _parse(user.get("locked_until"))
    if locked_until and locked_until > moment:
        audit.record(db, audit.USER_LOGIN_FAILED, actor=email, outcome="denied",
                     metadata={"reason": "locked"})
        raise TeamError(
            f"too many failed attempts; try again after {locked_until.strftime('%H:%M UTC')}",
            reason="locked",
        )

    if user["status"] == "disabled":
        audit.record(db, audit.USER_LOGIN_FAILED, actor=email, outcome="denied",
                     metadata={"reason": "disabled"})
        raise TeamError("this account is disabled", reason="disabled")

    if not user.get("password_hash") or not verify_secret(user["password_hash"], password):
        attempts = int(user.get("failed_attempts") or 0) + 1
        with db.write() as conn:
            if attempts >= MAX_FAILED_LOGINS:
                conn.execute(
                    "UPDATE users SET failed_attempts = ?, locked_until = ? WHERE id = ?",
                    (attempts, _iso(moment + timedelta(minutes=LOCKOUT_MINUTES)), user["id"]),
                )
            else:
                conn.execute(
                    "UPDATE users SET failed_attempts = ? WHERE id = ?", (attempts, user["id"])
                )
        audit.record(db, audit.USER_LOGIN_FAILED, actor=email, outcome="denied",
                     metadata={"reason": "bad_password", "attempts": attempts})
        raise refused

    token = secrets.token_urlsafe(32)
    session_id = uuid.uuid4().hex
    expires_at = _iso(moment + timedelta(seconds=settings.user_session_ttl_seconds))
    with db.write() as conn:
        conn.execute(
            """
            INSERT INTO user_sessions
                (id, user_id, token_hash, created_at, expires_at, user_agent)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, user["id"], hash_token(token), _iso(moment), expires_at, user_agent),
        )
        conn.execute(
            "UPDATE users SET last_login_at = ?, failed_attempts = 0, locked_until = NULL "
            "WHERE id = ?",
            (_iso(moment), user["id"]),
        )
        audit.record_in(
            conn, audit.USER_LOGIN, actor=email, target_type="user", target_id=user["id"],
            metadata={"session_id": session_id},
        )

    return {
        "token": token,
        "session_id": session_id,
        "expires_at": expires_at,
        "user": get_user(db, user["id"]),
    }


def verify_session(
    db: Database, token: str, now: datetime | None = None
) -> dict | None:
    """The user behind a session token, or None."""
    if not token:
        return None
    row = db.one(
        "SELECT * FROM user_sessions WHERE token_hash = ?", (hash_token(token),)
    )
    if row is None or row["revoked_at"] is not None:
        return None
    if (_parse(row["expires_at"]) or datetime.min.replace(tzinfo=timezone.utc)) <= (
        now or datetime.now(timezone.utc)
    ):
        return None

    user = db.one("SELECT * FROM users WHERE id = ?", (row["user_id"],))
    if user is None or user["status"] != "active":
        return None

    public = _public(user)
    public["session_id"] = row["id"]
    return public


def logout(db: Database, token: str, actor: str | None = None) -> bool:
    row = db.one("SELECT * FROM user_sessions WHERE token_hash = ?", (hash_token(token),))
    if row is None or row["revoked_at"] is not None:
        return False
    with db.write() as conn:
        conn.execute(
            "UPDATE user_sessions SET revoked_at = ? WHERE id = ?", (utcnow_iso(), row["id"])
        )
        audit.record_in(
            conn, audit.USER_LOGOUT, actor=actor, target_type="user", target_id=row["user_id"]
        )
    return True


def list_sessions(db: Database, user_id: str) -> list[dict]:
    rows = db.query(
        """
        SELECT id, created_at, expires_at, revoked_at, user_agent FROM user_sessions
         WHERE user_id = ? ORDER BY created_at DESC LIMIT 50
        """,
        (user_id,),
    )
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------- #
# Invites
# --------------------------------------------------------------------------- #
def create_invite(
    db: Database,
    settings: Settings,
    *,
    email: str,
    role: str,
    app_id: str | None = None,
    invited_by: str | None = None,
    now: datetime | None = None,
) -> dict:
    if role not in ("admin", "developer", "readonly"):
        raise TeamError("an invite may grant admin, developer or readonly", reason="invalid")
    email = normalise_email(email)
    if "@" not in email:
        raise TeamError("a valid email address is required", reason="invalid")
    if app_id and db.one("SELECT id FROM apps WHERE id = ?", (app_id,)) is None:
        raise TeamError(f"unknown project: {app_id}", reason="not_found")

    moment = now or datetime.now(timezone.utc)
    token = secrets.token_urlsafe(32)
    invite_id = uuid.uuid4().hex
    with db.write() as conn:
        conn.execute(
            """
            INSERT INTO invites
                (id, email, role, app_id, token_hash, invited_by, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                invite_id, email, role, app_id, hash_token(token), invited_by,
                _iso(moment),
                _iso(moment + timedelta(seconds=settings.invite_ttl_seconds)),
            ),
        )
        audit.record_in(
            conn, audit.USER_INVITED, actor=invited_by, target_type="invite",
            target_id=invite_id, metadata={"email": email, "role": role, "app_id": app_id},
        )
    return {**get_invite(db, invite_id), "token": token}  # type: ignore[dict-item]


def get_invite(db: Database, invite_id: str) -> dict | None:
    row = db.one("SELECT * FROM invites WHERE id = ?", (invite_id,))
    if row is None:
        return None
    out = dict(row)
    out.pop("token_hash", None)
    return out


def list_invites(db: Database, pending_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM invites"
    if pending_only:
        sql += " WHERE accepted_at IS NULL"
    sql += " ORDER BY created_at DESC LIMIT 200"
    rows = db.query(sql)
    out = []
    for row in rows:
        item = dict(row)
        item.pop("token_hash", None)
        out.append(item)
    return out


def revoke_invite(db: Database, invite_id: str) -> bool:
    with db.write() as conn:
        cursor = conn.execute("DELETE FROM invites WHERE id = ?", (invite_id,))
    return cursor.rowcount > 0


def accept_invite(
    db: Database,
    *,
    token: str,
    password: str,
    display_name: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Turn an invite into an account."""
    moment = now or datetime.now(timezone.utc)
    row = db.one("SELECT * FROM invites WHERE token_hash = ?", (hash_token(token),))
    if row is None:
        raise TeamError("this invite is not valid", reason="not_found")
    if row["accepted_at"] is not None:
        raise TeamError("this invite has already been used", reason="conflict")
    expires = _parse(row["expires_at"])
    if expires and expires <= moment:
        raise TeamError("this invite has expired", reason="expired")

    check_password_strength(password)
    email = normalise_email(row["email"])

    existing = find_by_email(db, email)
    if existing is not None:
        # An invite for someone who already exists is a password reset plus a
        # role change, not a second account.
        set_password(db, existing["id"], password)
        set_role(db, existing["id"], row["role"], actor="invite")
        enable(db, existing["id"], actor="invite")
        user_id = existing["id"]
    else:
        user = create_user(
            db, email=email, role=row["role"], display_name=display_name,
            password=password, actor="invite",
        )
        user_id = user["id"]

    if row["app_id"]:
        add_member(db, app_id=row["app_id"], user_id=user_id, role=row["role"], actor="invite")

    with db.write() as conn:
        conn.execute(
            "UPDATE invites SET accepted_at = ?, accepted_by = ? WHERE id = ?",
            (_iso(moment), user_id, row["id"]),
        )

    return {"user": get_user(db, user_id), "app_id": row["app_id"], "role": row["role"]}


# --------------------------------------------------------------------------- #
# Project membership
# --------------------------------------------------------------------------- #
def add_member(
    db: Database, *, app_id: str, user_id: str, role: str = "developer", actor: str = "system"
) -> dict:
    if role not in ("admin", "developer", "readonly"):
        raise TeamError("a member may be admin, developer or readonly", reason="invalid")
    if db.one("SELECT id FROM apps WHERE id = ?", (app_id,)) is None:
        raise TeamError(f"unknown project: {app_id}", reason="not_found")
    if db.one("SELECT id FROM users WHERE id = ?", (user_id,)) is None:
        raise TeamError("unknown user", reason="not_found")
    with db.write() as conn:
        conn.execute(
            """
            INSERT INTO project_members (app_id, user_id, role, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (app_id, user_id) DO UPDATE SET role = excluded.role
            """,
            (app_id, user_id, role, utcnow_iso()),
        )
        audit.record_in(
            conn, audit.MEMBER_ADDED, app_id=app_id, actor=actor,
            target_type="user", target_id=user_id, metadata={"role": role},
        )
    return {"app_id": app_id, "user_id": user_id, "role": role}


def remove_member(db: Database, *, app_id: str, user_id: str, actor: str = "system") -> bool:
    with db.write() as conn:
        cursor = conn.execute(
            "DELETE FROM project_members WHERE app_id = ? AND user_id = ?", (app_id, user_id)
        )
        if cursor.rowcount:
            audit.record_in(
                conn, audit.MEMBER_REMOVED, app_id=app_id, actor=actor,
                target_type="user", target_id=user_id,
            )
    return cursor.rowcount > 0


def list_members(db: Database, app_id: str) -> list[dict]:
    rows = db.query(
        """
        SELECT m.app_id, m.user_id, m.role, m.created_at, u.email, u.display_name, u.status
          FROM project_members m JOIN users u ON u.id = m.user_id
         WHERE m.app_id = ? ORDER BY m.role, u.email
        """,
        (app_id,),
    )
    return [dict(row) for row in rows]


def visible_projects(db: Database, user: dict) -> list[str] | None:
    """The projects this user may see.

    ``None`` means "all of them", which is how owner and admin read. Everyone
    else is limited to their memberships — and a developer with no membership
    sees nothing, which is the correct default rather than a bug.
    """
    if user.get("role") in ("owner", "admin"):
        return None
    rows = db.query(
        "SELECT app_id FROM project_members WHERE user_id = ? ORDER BY app_id", (user["id"],)
    )
    return [row["app_id"] for row in rows]


def may_see_project(db: Database, user: dict, app_id: str) -> bool:
    allowed = visible_projects(db, user)
    return allowed is None or app_id in allowed
