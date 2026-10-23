from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from powerbase.api import create_app
from powerbase.config import Settings
from powerbase.db import Database
from powerbase.provision import issue_key, provision_app

from helpers import ADMIN_PASSWORD, APP_ID, OTHER_APP_ID, seed_dated, seed_dataset


class Env:
    """A fully wired instance: FastAPI app, database and key helpers."""

    def __init__(self, client: TestClient, db: Database, settings: Settings, app=None):
        self.client = client
        self.db = db
        self.settings = settings
        self.app = app

    def key(self, scope: str = "ingest", app_id: str = APP_ID) -> str:
        return issue_key(self.db, app_id, scope)

    @staticmethod
    def headers(token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def post(self, path: str, token: str, payload: dict):
        return self.client.post(path, json=payload, headers=self.headers(token))


@pytest.fixture()
def make_env(tmp_path):
    """Factory so individual tests can override settings (e.g. rate limits)."""
    counter = {"n": 0}

    def _make(**overrides) -> Env:
        counter["n"] += 1
        params = dict(
            db_path=str(tmp_path / f"test-{counter['n']}.db"),
            rate_burst=100_000,
            rate_per_second=100_000.0,
            max_batch_size=1000,
        )
        params.update(overrides)
        settings = Settings(**params)
        db = Database(settings.db_path)
        app = create_app(settings, db)  # runs migrations
        provision_app(db, APP_ID, "Example Shop", "android")
        return Env(TestClient(app), db, settings, app)

    return _make


@pytest.fixture()
def env(make_env) -> Env:
    return make_env()


@pytest.fixture()
def seeded_env(make_env) -> Env:
    """An instance with a dashboard password and a deterministic dataset."""
    env = make_env(admin_password=ADMIN_PASSWORD)
    seed_dataset(env.db)
    return env


@pytest.fixture()
def dated_env(make_env) -> Env:
    """An instance with events on fixed dates, for rollup and retention tests."""
    env = make_env(admin_password=ADMIN_PASSWORD)
    seed_dated(env.db)
    return env


@pytest.fixture()
def alert_env(make_env) -> Env:
    """Seeded data seen through a window wide enough to include it.

    The seeded records are 2-5 days old while the alert window defaults to 24h,
    so the rule tests need a wider window or they would all be testing the
    "nothing in range" path.
    """
    env = make_env(
        admin_password=ADMIN_PASSWORD,
        alert_window_hours=24 * 7,
        min_samples_for_rates=1,
    )
    seed_dataset(env.db)
    return env


@pytest.fixture()
def other_app(seeded_env) -> Env:
    """A second app in the same database, to prove tenant isolation."""
    provision_app(seeded_env.db, OTHER_APP_ID, "Other App")
    return seeded_env


@pytest.fixture()
def logged_in(seeded_env) -> TestClient:
    client = seeded_env.client
    response = client.post(
        "/login",
        data={"password": ADMIN_PASSWORD, "next": "/"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return client
