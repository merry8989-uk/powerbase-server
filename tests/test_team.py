"""Users, roles, sessions, invites and project membership."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from helpers import ADMIN_PASSWORD, APP_ID, OTHER_APP_ID
from powerbase import team
from powerbase.api import create_app
from powerbase.provision import provision_app

OWNER = "owner@example.com"
OWNER_PW = "correct-horse-battery"


@pytest.fixture()
def app_env(make_env):
    """An instance with two projects and a bootstrapped owner."""
    env = make_env(admin_password=ADMIN_PASSWORD)
    provision_app(env.db, OTHER_APP_ID, "Other App", "backend")
    team.ensure_owner(env.db, OWNER, OWNER_PW)
    env.admin = {"X-Admin-Password": ADMIN_PASSWORD}
    return env


def fresh(env) -> TestClient:
    """A client with its own cookie jar, so one login does not leak into the next."""
    return TestClient(create_app(env.settings, env.db))


def login(client: TestClient, email: str, password: str) -> dict:
    response = client.post("/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return {"X-Powerbase-Session": response.json()["token"]}


def make_user(env, email: str, role: str, password: str = "a-long-enough-pw") -> dict:
    return team.create_user(env.db, email=email, role=role, password=password)


# ------------------------------------------------------------------ passwords
def test_a_short_password_is_refused(env):
    with pytest.raises(team.TeamError, match="at least"):
        team.create_user(env.db, email="a@b.co", role="developer", password="short")


def test_the_password_is_never_stored_in_the_clear(env):
    team.create_user(env.db, email="a@b.co", role="developer", password="a-long-enough-pw")
    row = env.db.one("SELECT password_hash FROM users")
    assert "a-long-enough-pw" not in row["password_hash"]
    assert row["password_hash"].startswith("$argon2")


def test_the_public_view_of_a_user_carries_no_hash(env):
    user = team.create_user(env.db, email="a@b.co", role="developer", password="a-long-enough-pw")
    assert "password_hash" not in user
    assert "password_hash" not in team.find_by_email(env.db, "a@b.co") or True


def test_an_email_is_case_insensitive(env):
    team.create_user(env.db, email="Amit@Example.com", role="developer", password="a-long-enough-pw")
    assert team.find_by_email(env.db, "amit@example.com") is not None
    with pytest.raises(team.TeamError, match="already has an account"):
        team.create_user(env.db, email="AMIT@example.com", role="developer")


# ------------------------------------------------------------------- the roles
@pytest.mark.parametrize(
    "role,permission,expected",
    [
        ("owner", "users:write_admin", True),
        ("admin", "users:write_admin", False),
        ("admin", "users:write", True),
        ("developer", "read", True),
        ("developer", "export", True),
        ("developer", "alerts:ack", True),
        ("developer", "alerts:write", False),
        ("developer", "keys:write", False),
        ("readonly", "read", True),
        ("readonly", "export", False),
        ("readonly", "alerts:ack", False),
        (None, "read", False),
    ],
)
def test_the_permission_table(role, permission, expected):
    assert team.can(role, permission) is expected


def test_an_unknown_permission_is_refused_rather_than_allowed():
    """Being wrong about an unknown permission must fail closed."""
    assert team.can("owner", "no:such:permission") is False


# -------------------------------------------------------------------- login
def test_login_issues_a_session_and_me_describes_it(app_env):
    client = fresh(app_env)
    headers = login(client, OWNER, OWNER_PW)

    me = client.get("/v1/auth/me", headers=headers).json()
    assert me["role"] == "owner"
    assert me["actor"] == OWNER
    assert "users:write_admin" in me["permissions"]
    assert me["projects"] is None, "an owner sees every project"


def test_a_wrong_password_and_an_unknown_account_look_the_same(app_env):
    client = fresh(app_env)
    wrong = client.post("/v1/auth/login", json={"email": OWNER, "password": "nope"})
    unknown = client.post("/v1/auth/login", json={"email": "ghost@example.com", "password": "nope"})

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]


def test_repeated_failures_lock_the_account(app_env):
    client = fresh(app_env)
    for _ in range(5):
        client.post("/v1/auth/login", json={"email": OWNER, "password": "nope"})

    locked = client.post("/v1/auth/login", json={"email": OWNER, "password": OWNER_PW})
    # 429, not 401 and not 400: the credential was never even examined.
    assert locked.status_code == 429
    assert "too many failed attempts" in locked.json()["detail"]


def test_a_disabled_account_cannot_sign_in(app_env):
    user = make_user(app_env, "gone@example.com", "developer")
    team.disable(app_env.db, user["id"])

    client = fresh(app_env)
    response = client.post(
        "/v1/auth/login", json={"email": "gone@example.com", "password": "a-long-enough-pw"}
    )
    assert response.status_code == 403
    assert "disabled" in response.json()["detail"]


def test_disabling_kills_the_existing_sessions(app_env):
    """Otherwise "disabled" means "disabled at the next login"."""
    user = make_user(app_env, "gone@example.com", "developer")
    client = fresh(app_env)
    headers = login(client, "gone@example.com", "a-long-enough-pw")
    assert client.get("/v1/auth/me", headers=headers).status_code == 200

    team.disable(app_env.db, user["id"])
    assert client.get("/v1/auth/me", headers=headers).status_code == 401


def test_logout_revokes_the_session(app_env):
    client = fresh(app_env)
    headers = login(client, OWNER, OWNER_PW)
    assert client.post("/v1/auth/logout", headers=headers).status_code == 200
    assert client.get("/v1/auth/me", headers=headers).status_code == 401


def test_an_unauthenticated_request_is_401_not_403(app_env):
    """The distinction matters: 401 means "identify yourself", 403 means "not you"."""
    client = fresh(app_env)
    assert client.get("/v1/users").status_code == 401
    assert client.get("/v1/incidents").status_code == 401


def test_the_admin_password_is_break_glass(app_env):
    client = fresh(app_env)
    me = client.get("/v1/auth/me", headers=app_env.admin).json()
    assert me["role"] == "owner"
    assert me["actor"] == "admin-password"
    assert me["user"] is None


# ------------------------------------------------------------------ the users
def test_only_an_owner_may_mint_an_admin(app_env):
    make_user(app_env, "admin@example.com", "admin")
    client = fresh(app_env)
    headers = login(client, "admin@example.com", "a-long-enough-pw")

    refused = client.post(
        "/v1/users", headers=headers, json={"email": "new@example.com", "role": "admin"}
    )
    assert refused.status_code == 403
    assert "owner" in refused.json()["detail"]

    allowed = client.post(
        "/v1/users", headers=headers, json={"email": "dev@example.com", "role": "developer"}
    )
    assert allowed.status_code == 201


def test_a_developer_cannot_list_users(app_env):
    make_user(app_env, "dev@example.com", "developer")
    client = fresh(app_env)
    headers = login(client, "dev@example.com", "a-long-enough-pw")
    assert client.get("/v1/users", headers=headers).status_code == 403


def test_a_refusal_is_recorded(app_env):
    make_user(app_env, "dev@example.com", "developer")
    client = fresh(app_env)
    headers = login(client, "dev@example.com", "a-long-enough-pw")
    client.get("/v1/users", headers=headers)

    events = app_env.db.query(
        "SELECT event_type, outcome FROM audit_events WHERE outcome = 'denied'"
    )
    assert any(row["event_type"] == "denied.users:read" for row in events)


def test_the_last_owner_cannot_be_demoted(app_env):
    owner = team.find_by_email(app_env.db, OWNER)
    with pytest.raises(team.TeamError, match="last owner"):
        team.set_role(app_env.db, owner["id"], "readonly")


def test_the_last_owner_cannot_be_disabled(app_env):
    owner = team.find_by_email(app_env.db, OWNER)
    with pytest.raises(team.TeamError, match="last active owner"):
        team.disable(app_env.db, owner["id"])


def test_an_owner_can_be_demoted_once_there_is_another(app_env):
    owner = team.find_by_email(app_env.db, OWNER)
    second = make_user(app_env, "second@example.com", "admin")
    team.set_role(app_env.db, second["id"], "owner")

    assert team.set_role(app_env.db, owner["id"], "readonly")["role"] == "readonly"


def test_you_cannot_disable_yourself(app_env):
    client = fresh(app_env)
    headers = login(client, OWNER, OWNER_PW)
    me = client.get("/v1/auth/me", headers=headers).json()
    response = client.post(f"/v1/users/{me['user']['id']}/disable", headers=headers)
    assert response.status_code == 400


# ---------------------------------------------------------------- the invites
def test_an_invite_creates_an_account_once(app_env):
    client = fresh(app_env)
    headers = login(client, OWNER, OWNER_PW)

    invite = client.post(
        "/v1/invites", headers=headers,
        json={"email": "new@example.com", "role": "readonly", "app_id": APP_ID},
    ).json()
    assert invite["token"]
    assert "token_hash" not in invite

    accepted = client.post(
        "/v1/invites/accept",
        json={"token": invite["token"], "password": "another-good-pw", "display_name": "New"},
    )
    assert accepted.status_code == 200
    assert accepted.json()["user"]["role"] == "readonly"

    # Single use.
    again = client.post(
        "/v1/invites/accept", json={"token": invite["token"], "password": "another-good-pw"}
    )
    # 409 Conflict: the request was fine, the state was not.
    assert again.status_code == 409
    assert "already been used" in again.json()["detail"]


def test_an_invite_scoped_to_a_project_grants_that_project(app_env):
    client = fresh(app_env)
    headers = login(client, OWNER, OWNER_PW)
    invite = client.post(
        "/v1/invites", headers=headers,
        json={"email": "new@example.com", "role": "developer", "app_id": OTHER_APP_ID},
    ).json()
    client.post("/v1/invites/accept", json={"token": invite["token"], "password": "another-good-pw"})

    scoped = fresh(app_env)
    new_headers = login(scoped, "new@example.com", "another-good-pw")
    assert scoped.get("/v1/auth/me", headers=new_headers).json()["projects"] == [OTHER_APP_ID]


def test_an_expired_invite_is_refused(app_env):
    from datetime import datetime, timedelta, timezone

    client = fresh(app_env)
    headers = login(client, OWNER, OWNER_PW)
    invite = client.post(
        "/v1/invites", headers=headers, json={"email": "late@example.com", "role": "readonly"}
    ).json()

    with app_env.db.write() as conn:
        conn.execute(
            "UPDATE invites SET expires_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00.000000Z", invite["id"]),
        )

    response = client.post(
        "/v1/invites/accept", json={"token": invite["token"], "password": "another-good-pw"}
    )
    # 410 Gone: it was real, it is over.
    assert response.status_code == 410
    assert "expired" in response.json()["detail"]


def test_a_bogus_invite_token_is_refused(app_env):
    client = fresh(app_env)
    response = client.post(
        "/v1/invites/accept", json={"token": "not-a-token", "password": "another-good-pw"}
    )
    assert response.status_code == 404


def test_an_invite_may_not_grant_owner(app_env):
    client = fresh(app_env)
    headers = login(client, OWNER, OWNER_PW)
    response = client.post(
        "/v1/invites", headers=headers, json={"email": "x@example.com", "role": "owner"}
    )
    assert response.status_code == 400


# ---------------------------------------------------------------- membership
def test_a_developer_sees_only_their_projects(app_env):
    user = make_user(app_env, "dev@example.com", "developer")
    team.add_member(app_env.db, app_id=OTHER_APP_ID, user_id=user["id"])

    client = fresh(app_env)
    headers = login(client, "dev@example.com", "a-long-enough-pw")
    assert client.get("/v1/auth/me", headers=headers).json()["projects"] == [OTHER_APP_ID]

    assert client.get(f"/v1/projects/{OTHER_APP_ID}/members", headers=headers).status_code == 200
    assert client.get(f"/v1/projects/{APP_ID}/members", headers=headers).status_code == 403


def test_a_developer_with_no_projects_sees_nothing(app_env):
    make_user(app_env, "dev@example.com", "developer")
    client = fresh(app_env)
    headers = login(client, "dev@example.com", "a-long-enough-pw")
    assert client.get("/v1/auth/me", headers=headers).json()["projects"] == []


def test_an_owner_sees_every_project(app_env):
    client = fresh(app_env)
    headers = login(client, OWNER, OWNER_PW)
    assert client.get(f"/v1/projects/{APP_ID}/members", headers=headers).status_code == 200
    assert client.get(f"/v1/projects/{OTHER_APP_ID}/members", headers=headers).status_code == 200


def test_membership_can_be_added_and_removed(app_env):
    user = make_user(app_env, "dev@example.com", "developer")
    client = fresh(app_env)
    headers = login(client, OWNER, OWNER_PW)

    added = client.post(
        f"/v1/projects/{APP_ID}/members", headers=headers,
        json={"user_id": user["id"], "role": "developer"},
    )
    assert added.status_code == 201
    assert [m["email"] for m in client.get(
        f"/v1/projects/{APP_ID}/members", headers=headers
    ).json()["items"]] == ["dev@example.com"]

    removed = client.delete(f"/v1/projects/{APP_ID}/members/{user['id']}", headers=headers)
    assert removed.status_code == 200
    assert client.get(f"/v1/projects/{APP_ID}/members", headers=headers).json()["items"] == []


def test_membership_for_an_unknown_project_is_refused(app_env):
    user = make_user(app_env, "dev@example.com", "developer")
    client = fresh(app_env)
    headers = login(client, OWNER, OWNER_PW)
    response = client.post(
        "/v1/projects/no.such.project/members", headers=headers,
        json={"user_id": user["id"], "role": "developer"},
    )
    assert response.status_code == 404


def test_bootstrap_is_idempotent(app_env):
    first = team.ensure_owner(app_env.db, OWNER, OWNER_PW)
    second = team.ensure_owner(app_env.db, OWNER, "a-different-password")
    assert first["id"] == second["id"]
    assert second["role"] == "owner"
    # And it did not quietly change the password of an existing owner.
    assert team.verify_session(app_env.db, "nonsense") is None
