"""The console's half of team access.

`powerbase/team.py` built roles, sessions and membership, and all of it was
reachable over the API only. The dashboard still authenticated with the admin
password alone, so **a developer account could not sign in to the one screen the
roles are meant to govern**. These tests hold the console to the roles.
"""

from __future__ import annotations

import pytest

from helpers import APP_ID
from powerbase import team

OWNER = "owner@example.com"
OWNER_PW = "a-long-enough-password"
DEV = "dev@example.com"
DEV_PW = "dev-password-1234"
ADMIN = "admin@example.com"
ADMIN_PW = "admin-password-1234"


@pytest.fixture()
def console(make_env):
    env = make_env(admin_password="break-glass-pw")
    team.ensure_owner(env.db, OWNER, OWNER_PW)
    env.c = env.client
    return env


def sign_in(client, *, email="", password):
    return client.post(
        "/login", data={"email": email, "password": password, "next": "/"},
        follow_redirects=False,
    )


# ------------------------------------------------------------ both ways in
def test_the_admin_password_still_signs_in(console):
    """Break-glass must survive: an instance whose only owner is locked out has
    to be recoverable from the host, not from a support ticket."""
    r = sign_in(console.c, password="break-glass-pw")
    assert r.status_code == 303
    assert console.c.get("/team").status_code == 200


def test_an_account_signs_in_and_gets_a_user_cookie(console):
    r = sign_in(console.c, email=OWNER, password=OWNER_PW)
    assert r.status_code == 303
    assert "pb_user" in console.c.cookies, "a session token, not the admin cookie"
    assert console.c.get("/team").status_code == 200


def test_a_wrong_account_password_is_refused(console):
    r = sign_in(console.c, email=OWNER, password="not-the-password")
    assert r.status_code == 401
    # `follow_redirects=False`: without it the 303 to /login is followed and the
    # login page answers 200, which looks like being signed in.
    after = console.c.get("/team", follow_redirects=False)
    assert after.status_code == 303, "and still not signed in"
    assert after.headers["location"] == "/login"


def test_the_page_says_who_you_are(console):
    sign_in(console.c, email=OWNER, password=OWNER_PW)
    body = console.c.get("/team").text
    assert OWNER in body
    assert "owner" in body


# ------------------------------------------------------------------- the nav
def test_an_owner_sees_the_team_link(console):
    sign_in(console.c, email=OWNER, password=OWNER_PW)
    assert 'href="/team"' in console.c.get("/").text


def test_a_developer_does_not(console):
    """Hidden rather than shown-then-refused: a link that 403s is worse than no
    link."""
    team.create_user(console.db, email=DEV, role="developer", password=DEV_PW)
    console.c.cookies.clear()
    sign_in(console.c, email=DEV, password=DEV_PW)
    assert 'href="/team"' not in console.c.get("/").text


def test_a_developer_can_still_use_the_rest_of_the_console(console):
    team.create_user(console.db, email=DEV, role="developer", password=DEV_PW)
    console.c.cookies.clear()
    sign_in(console.c, email=DEV, password=DEV_PW)
    assert console.c.get("/").status_code == 200


# --------------------------------------------------------------- the refusal
def test_a_developer_is_refused_the_team_page(console):
    team.create_user(console.db, email=DEV, role="developer", password=DEV_PW)
    console.c.cookies.clear()
    sign_in(console.c, email=DEV, password=DEV_PW)
    r = console.c.get("/team")
    assert r.status_code == 403
    assert "cannot see the team" in r.text


def test_a_developer_cannot_create_a_user(console):
    team.create_user(console.db, email=DEV, role="developer", password=DEV_PW)
    console.c.cookies.clear()
    sign_in(console.c, email=DEV, password=DEV_PW)
    r = console.c.post(
        "/team/users",
        data={"email": "sneaky@example.com", "role": "admin", "display_name": "",
              "password": "a-long-enough-password"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "denied" in r.headers["location"]
    assert team.find_by_email(console.db, "sneaky@example.com") is None


# ------------------------------------------------------------- creating people
def test_an_owner_creates_a_developer(console):
    sign_in(console.c, email=OWNER, password=OWNER_PW)
    r = console.c.post(
        "/team/users",
        data={"email": DEV, "role": "developer", "display_name": "Dev", "password": DEV_PW},
        follow_redirects=False,
    )
    assert "notice=created" in r.headers["location"]
    assert team.find_by_email(console.db, DEV)["role"] == "developer"


def test_only_an_owner_may_mint_an_admin(console):
    """An admin creating an admin is how the owner role quietly stops meaning
    anything."""
    team.create_user(console.db, email=ADMIN, role="admin", password=ADMIN_PW)
    console.c.cookies.clear()
    sign_in(console.c, email=ADMIN, password=ADMIN_PW)
    r = console.c.post(
        "/team/users",
        data={"email": "second-admin@example.com", "role": "admin",
              "display_name": "", "password": "a-long-enough-password"},
        follow_redirects=False,
    )
    assert "only+an+owner" in r.headers["location"]
    assert team.find_by_email(console.db, "second-admin@example.com") is None


def test_an_admin_may_still_create_a_developer(console):
    team.create_user(console.db, email=ADMIN, role="admin", password=ADMIN_PW)
    console.c.cookies.clear()
    sign_in(console.c, email=ADMIN, password=ADMIN_PW)
    r = console.c.post(
        "/team/users",
        data={"email": DEV, "role": "developer", "display_name": "", "password": DEV_PW},
        follow_redirects=False,
    )
    assert "notice=created" in r.headers["location"]


# ------------------------------------------------------------ roles and state
def test_a_role_can_be_changed_from_the_console(console):
    created = team.create_user(console.db, email=DEV, role="developer", password=DEV_PW)
    sign_in(console.c, email=OWNER, password=OWNER_PW)
    r = console.c.post(
        f"/team/users/{created['id']}/role", data={"role": "readonly"},
        follow_redirects=False,
    )
    assert "notice=role+changed" in r.headers["location"]
    assert team.find_by_email(console.db, DEV)["role"] == "readonly"


def test_an_account_can_be_disabled_and_enabled(console):
    created = team.create_user(console.db, email=DEV, role="developer", password=DEV_PW)
    sign_in(console.c, email=OWNER, password=OWNER_PW)
    console.c.post(f"/team/users/{created['id']}/disable", follow_redirects=False)
    assert team.find_by_email(console.db, DEV)["status"] == "disabled"
    console.c.post(f"/team/users/{created['id']}/enable", follow_redirects=False)
    assert team.find_by_email(console.db, DEV)["status"] == "active"


def test_the_last_owner_cannot_be_disabled(console):
    """The console must not offer a button that locks everyone out."""
    owner = team.find_by_email(console.db, OWNER)
    sign_in(console.c, email=OWNER, password=OWNER_PW)
    r = console.c.post(f"/team/users/{owner['id']}/disable", follow_redirects=False)
    assert "error=" in r.headers["location"]
    assert team.find_by_email(console.db, OWNER)["status"] == "active"


# ---------------------------------------------------------------- signing out
def test_logging_out_revokes_the_session_server_side(console):
    """Deleting a cookie tells this browser to forget. Revoking the token is the
    difference between logging out and hiding."""
    sign_in(console.c, email=OWNER, password=OWNER_PW)
    token = console.c.cookies.get("pb_user")
    assert team.verify_session(console.db, token) is not None
    console.c.get("/logout", follow_redirects=False)
    assert team.verify_session(console.db, token) is None


def test_an_unsigned_visitor_is_sent_to_the_login_page(console):
    r = console.c.get("/team", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
