"""Authorisation, in one place.

Every protected route names a **permission**; `team.PERMISSIONS` says which roles
hold it. The dependency refuses by default, so a route that forgets to name one
is a route nobody can reach.

Two ways to be authenticated:

* **A user session** — `X-Powerbase-Session: <token>`, or the `pb_user` cookie.
* **The admin password** — `X-Admin-Password: <password>`. Break-glass, mapping
  to the owner role, so a locked-out owner account is recoverable from the host.

Status codes are decided by a refusal's machine-readable `reason`, never by the
wording of its message. It was once decided by wording, and a lockout answered
401 because the message said "too many failed attempts" rather than "locked".
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request, status

from . import audit, team
from .config import Settings
from .dashboard_auth import check_password
from .db import Database

USER_COOKIE = "pb_user"

#: refusal reason -> HTTP status
STATUS_BY_REASON: dict[str, int] = {
    "credentials": status.HTTP_401_UNAUTHORIZED,
    "locked": status.HTTP_429_TOO_MANY_REQUESTS,
    "disabled": status.HTTP_403_FORBIDDEN,
    "conflict": status.HTTP_409_CONFLICT,
    "expired": status.HTTP_410_GONE,
    "not_found": status.HTTP_404_NOT_FOUND,
    "invalid": status.HTTP_400_BAD_REQUEST,
}


def status_for(exc: team.TeamError) -> int:
    return STATUS_BY_REASON.get(getattr(exc, "reason", "invalid"), status.HTTP_400_BAD_REQUEST)


def build(db: Database, settings: Settings):
    """Return the two callables a route module needs."""

    def actor_for(request: Request) -> dict[str, Any] | None:
        supplied = request.headers.get("x-admin-password")
        if (
            supplied
            and settings.admin_password
            and check_password(settings.admin_password, supplied)
        ):
            return {"role": "owner", "actor": "admin-password", "user": None}

        token = request.headers.get("x-powerbase-session") or request.cookies.get(USER_COOKIE)
        if token:
            user = team.verify_session(db, token)
            if user is not None:
                return {"role": user["role"], "actor": user["email"], "user": user}
        return None

    def require(permission: str):
        def dependency(request: Request) -> dict[str, Any]:
            actor = actor_for(request)
            if actor is None:
                raise HTTPException(
                    status.HTTP_401_UNAUTHORIZED,
                    "sign in, or send the admin password in X-Admin-Password",
                )
            if not team.can(actor["role"], permission):
                # A refusal is worth recording: "who tried and was told no" is
                # the other half of an audit trail.
                audit.record(
                    db, f"denied.{permission}", outcome="denied",
                    actor=actor["actor"], metadata={"role": actor["role"]},
                )
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    f"the {actor['role']} role cannot {permission}",
                )
            return actor

        return dependency

    return actor_for, require
