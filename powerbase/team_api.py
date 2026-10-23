"""The team, incident and alert-channel API.

Authorisation is one idea in one place: every route names a **permission**, and
`team.PERMISSIONS` says which roles hold it. A route that forgets to name one is
a route that cannot be reached, because the dependency refuses by default.

Two ways to be authenticated:

* **A user session** — `X-Powerbase-Session: <token>`, or the `pb_user` cookie
  for the console. Carries the user's role.
* **The admin password** — `X-Admin-Password: <password>`. Break-glass: it maps
  to the owner role, so a locked-out owner account is recoverable from the host.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from . import audit, authz, incidents, notify, team
from .config import Settings
from .dashboard_auth import check_password
from .db import Database

USER_COOKIE = authz.USER_COOKIE


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str
    password: str


class UserIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str
    role: str
    display_name: str | None = None
    password: str | None = None


class RoleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str


class PasswordIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str


class InviteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str
    role: str = "developer"
    app_id: str | None = None


class AcceptIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str
    password: str
    display_name: str | None = None


class MemberIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str
    role: str = "developer"


class ChannelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    target: str
    app_id: str | None = None
    min_severity: str = "info"
    enabled: bool = True


def register_team_routes(
    app, db: Database, settings: Settings, require_admin
) -> None:
    # ---------------------------------------------------------------- auth
    actor_for, require = authz.build(db, settings)

    def fail(exc: team.TeamError) -> HTTPException:
        return HTTPException(authz.status_for(exc), str(exc))

    # --------------------------------------------------------------- login
    @app.post("/v1/auth/login", tags=["team"])
    def login(body: LoginIn, request: Request, response: Response) -> dict:
        try:
            result = team.login(
                db, settings, email=body.email, password=body.password,
                user_agent=request.headers.get("user-agent"),
            )
        except team.TeamError as exc:
            raise HTTPException(authz.status_for(exc), str(exc)) from None

        response.set_cookie(
            USER_COOKIE, result["token"], httponly=True, samesite="lax",
            secure=bool(request.headers.get("x-forwarded-proto") == "https"),
            max_age=settings.user_session_ttl_seconds, path="/",
        )
        return result

    @app.post("/v1/auth/logout", tags=["team"])
    def logout(request: Request, response: Response) -> dict:
        token = request.headers.get("x-powerbase-session") or request.cookies.get(USER_COOKIE)
        actor = actor_for(request)
        revoked = team.logout(db, token or "", actor=(actor or {}).get("actor"))
        response.delete_cookie(USER_COOKIE, path="/")
        return {"revoked": revoked}

    @app.get("/v1/auth/me", tags=["team"])
    def me(actor: dict = Depends(require("read"))) -> dict:
        user = actor["user"]
        return {
            "actor": actor["actor"],
            "role": actor["role"],
            "user": user,
            "permissions": sorted(
                permission for permission in team.PERMISSIONS if team.can(actor["role"], permission)
            ),
            "projects": None if user is None else team.visible_projects(db, user),
        }

    @app.get("/v1/auth/sessions", tags=["team"])
    def my_sessions(actor: dict = Depends(require("read"))) -> dict:
        if actor["user"] is None:
            return {"items": [], "note": "signed in with the admin password, so there is no session"}
        return {"items": team.list_sessions(db, actor["user"]["id"])}

    # --------------------------------------------------------------- users
    @app.get("/v1/users", tags=["team"])
    def users_index(status_filter: str | None = Query(default=None, alias="status"),
                    actor: dict = Depends(require("users:read"))) -> dict:
        return {"items": team.list_users(db, status_filter)}

    @app.post("/v1/users", status_code=status.HTTP_201_CREATED, tags=["team"])
    def users_create(body: UserIn, actor: dict = Depends(require("users:write"))) -> dict:
        if body.role == "admin" and not team.can(actor["role"], "users:write_admin"):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "only an owner may create an admin")
        if body.role == "owner":
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "promote to owner with PATCH /v1/users/{id}, which checks the last-owner rule",
            )
        try:
            return team.create_user(
                db, email=body.email, role=body.role, display_name=body.display_name,
                password=body.password, actor=actor["actor"],
            )
        except team.TeamError as exc:
            raise fail(exc) from None

    @app.patch("/v1/users/{user_id}", tags=["team"])
    def users_role(user_id: str, body: RoleIn, actor: dict = Depends(require("users:write"))) -> dict:
        if body.role == "admin" and not team.can(actor["role"], "users:write_admin"):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "only an owner may grant admin")
        try:
            updated = team.set_role(db, user_id, body.role, actor=actor["actor"])
        except team.TeamError as exc:
            raise fail(exc) from None
        if updated is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such user")
        return updated

    @app.post("/v1/users/{user_id}/disable", tags=["team"])
    def users_disable(user_id: str, actor: dict = Depends(require("users:write"))) -> dict:
        if actor["user"] and actor["user"]["id"] == user_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "you cannot disable yourself")
        try:
            updated = team.disable(db, user_id, actor=actor["actor"])
        except team.TeamError as exc:
            raise fail(exc) from None
        if updated is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such user")
        return updated

    @app.post("/v1/users/{user_id}/enable", tags=["team"])
    def users_enable(user_id: str, actor: dict = Depends(require("users:write"))) -> dict:
        updated = team.enable(db, user_id, actor=actor["actor"])
        if updated is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such user")
        return updated

    @app.post("/v1/users/{user_id}/password", tags=["team"])
    def users_password(
        user_id: str, body: PasswordIn, actor: dict = Depends(require("users:write"))
    ) -> dict:
        try:
            if not team.set_password(db, user_id, body.password):
                raise HTTPException(status.HTTP_404_NOT_FOUND, "no such user")
        except team.TeamError as exc:
            raise fail(exc) from None
        audit.record(
            db, "user.password_set", actor=actor["actor"], target_type="user", target_id=user_id
        )
        return {"user_id": user_id, "password_set": True}

    # ------------------------------------------------------------- invites
    @app.get("/v1/invites", tags=["team"])
    def invites_index(pending: bool = False, actor: dict = Depends(require("users:read"))) -> dict:
        return {"items": team.list_invites(db, pending_only=pending)}

    @app.post("/v1/invites", status_code=status.HTTP_201_CREATED, tags=["team"])
    def invites_create(body: InviteIn, actor: dict = Depends(require("users:write"))) -> dict:
        if body.role == "admin" and not team.can(actor["role"], "users:write_admin"):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "only an owner may invite an admin")
        try:
            return team.create_invite(
                db, settings, email=body.email, role=body.role,
                app_id=body.app_id, invited_by=actor["actor"],
            )
        except team.TeamError as exc:
            raise fail(exc) from None

    @app.delete("/v1/invites/{invite_id}", tags=["team"])
    def invites_revoke(invite_id: str, actor: dict = Depends(require("users:write"))) -> dict:
        if not team.revoke_invite(db, invite_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such invite")
        return {"id": invite_id, "revoked": True}

    @app.post("/v1/invites/accept", tags=["team"])
    def invites_accept(body: AcceptIn) -> dict:
        """Deliberately unauthenticated: the token *is* the credential."""
        try:
            return team.accept_invite(
                db, token=body.token, password=body.password, display_name=body.display_name
            )
        except team.TeamError as exc:
            raise fail(exc) from None

    # ------------------------------------------------------------ members
    @app.get("/v1/projects/{app_id}/members", tags=["team"])
    def members_index(app_id: str, actor: dict = Depends(require("read"))) -> dict:
        if actor["user"] is not None and not team.may_see_project(db, actor["user"], app_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "not your project")
        return {"app_id": app_id, "items": team.list_members(db, app_id)}

    @app.post("/v1/projects/{app_id}/members", status_code=status.HTTP_201_CREATED, tags=["team"])
    def members_add(
        app_id: str, body: MemberIn, actor: dict = Depends(require("users:write"))
    ) -> dict:
        try:
            return team.add_member(
                db, app_id=app_id, user_id=body.user_id, role=body.role, actor=actor["actor"]
            )
        except team.TeamError as exc:
            raise fail(exc) from None

    @app.delete("/v1/projects/{app_id}/members/{user_id}", tags=["team"])
    def members_remove(
        app_id: str, user_id: str, actor: dict = Depends(require("users:write"))
    ) -> dict:
        if not team.remove_member(db, app_id=app_id, user_id=user_id, actor=actor["actor"]):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "not a member")
        return {"app_id": app_id, "user_id": user_id, "removed": True}

    # ------------------------------------------------------------ channels
    @app.get("/v1/alert-channels", tags=["alerts"])
    def channels_index(app_id: str | None = None, actor: dict = Depends(require("read"))) -> dict:
        return {"items": notify.list_channels(db, app_id)}

    @app.post("/v1/alert-channels", status_code=status.HTTP_201_CREATED, tags=["alerts"])
    def channels_create(body: ChannelIn, actor: dict = Depends(require("alerts:write"))) -> dict:
        try:
            channel = notify.add_channel(
                db, kind=body.kind, target=body.target, app_id=body.app_id,
                min_severity=body.min_severity, enabled=body.enabled,
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
        audit.record(
            db, "alert_channel.created", app_id=body.app_id, actor=actor["actor"],
            target_type="alert_channel", target_id=channel["id"],
            metadata={"kind": body.kind, "min_severity": body.min_severity},
        )
        return channel

    @app.delete("/v1/alert-channels/{channel_id}", tags=["alerts"])
    def channels_delete(channel_id: str, actor: dict = Depends(require("alerts:write"))) -> dict:
        if not notify.delete_channel(db, channel_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such channel")
        return {"id": channel_id, "deleted": True}

    # ----------------------------------------------------------- incidents
    @app.get("/v1/incidents", tags=["alerts"])
    def incidents_index(
        app_id: str | None = None,
        status_filter: str | None = Query(default=None, alias="status"),
        severity: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        actor: dict = Depends(require("read")),
    ) -> dict:
        return {
            "summary": incidents.summary(db),
            "items": incidents.list_incidents(
                db, app_id=app_id, status=status_filter, severity=severity, limit=limit
            ),
        }

    @app.get("/v1/incidents/{incident_id}", tags=["alerts"])
    def incidents_get(incident_id: str, actor: dict = Depends(require("read"))) -> dict:
        incident = incidents.get_incident(db, incident_id)
        if incident is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such incident")
        return {
            "incident": incident,
            "deliveries": notify.list_deliveries(db, incident_id=incident_id),
        }

    @app.post("/v1/incidents/{incident_id}/ack", tags=["alerts"])
    def incidents_ack(incident_id: str, actor: dict = Depends(require("alerts:ack"))) -> dict:
        updated = incidents.ack(db, incident_id, actor["actor"])
        if updated is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such incident")
        return updated

    @app.post("/v1/incidents/{incident_id}/resolve", tags=["alerts"])
    def incidents_resolve(incident_id: str, actor: dict = Depends(require("alerts:ack"))) -> dict:
        updated = incidents.resolve(db, incident_id, actor["actor"])
        if updated is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such incident")
        return updated

    @app.post("/v1/incidents/{incident_id}/reopen", tags=["alerts"])
    def incidents_reopen(incident_id: str, actor: dict = Depends(require("alerts:ack"))) -> dict:
        updated = incidents.reopen(db, incident_id, actor["actor"])
        if updated is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such incident")
        return updated

    # ---------------------------------------------------------- deliveries
    @app.get("/v1/alert-deliveries", tags=["alerts"])
    def deliveries_index(
        incident_id: str | None = None,
        status_filter: str | None = Query(default=None, alias="status"),
        limit: int = Query(default=100, ge=1, le=500),
        actor: dict = Depends(require("read")),
    ) -> dict:
        return {
            "items": notify.list_deliveries(db, incident_id, status_filter, limit),
            "queue": notify.queue_health(db),
        }

    @app.post("/v1/alert-deliveries/{delivery_id}/retry", tags=["alerts"])
    def deliveries_retry(delivery_id: int, actor: dict = Depends(require("alerts:write"))) -> dict:
        if not notify.retry_dead(db, delivery_id):
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "no such delivery, or it is not dead"
            )
        return {"id": delivery_id, "requeued": True}

    @app.post("/v1/alert-deliveries/flush", tags=["alerts"])
    def deliveries_flush(actor: dict = Depends(require("alerts:write"))) -> dict:
        """Send whatever is due now, rather than waiting for the next pass."""
        return notify.flush(db, settings)

    @app.get("/v1/alert-queue", tags=["alerts"])
    def alert_queue(actor: dict = Depends(require("read"))) -> dict:
        return notify.queue_health(db)
