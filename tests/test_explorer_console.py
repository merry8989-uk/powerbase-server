"""The log-explorer console page.

The console calls the same query layer the API does, so these tests are about
the wiring: does the page render, do the filters reach the query, does a refused
query say so instead of showing an empty table.
"""

import re

import pytest

from helpers import ADMIN_PASSWORD, APP_ID, log_event, log_events_payload

PAGE = "/explorer"
PARTIAL = "/partials/explorer"


@pytest.fixture()
def console(make_env):
    env = make_env(admin_password=ADMIN_PASSWORD)
    ingest = env.key(scope="ingest")
    for index in range(6):
        env.post(
            "/v1/ingest/log-events",
            ingest,
            log_events_payload(
                batch_id=f"con-{index}",
                events=[
                    log_event(
                        event_id=f"ev-{index}",
                        ts=f"2026-10-22T0{index}:00:00Z",
                        severity=["INFO", "WARN", "ERROR"][index % 3],
                        service_name=["checkout-api", "auth-api"][index % 2],
                        environment="production",
                        status_code=500 if index % 3 == 2 else 200,
                        trace_id="trace-1" if index < 3 else None,
                        error_fingerprint="fp-1" if index % 3 == 2 else None,
                    )
                ],
            ),
        )
    env.client.post(
        "/login", data={"password": ADMIN_PASSWORD, "next": "/"}, follow_redirects=False
    )
    return env


def test_the_page_renders_with_its_controls(console):
    page = console.client.get(PAGE)
    assert page.status_code == 200
    for expected in (
        'hx-get="/partials/explorer"',
        'name="severity"',
        'name="source"',
        'name="service_name"',
        'name="environment"',
        'name="route_template"',
        'name="trace_id"',
        'name="min_status"',
        'name="q"',
        'name="bucket"',
        'name="sort"',
        'name="direction"',
    ):
        assert expected in page.text, expected


def test_the_selectors_are_filled_from_real_data(console):
    page = console.client.get(PAGE)
    # Service and environment dropdowns come from what has actually been ingested.
    assert "checkout-api" in page.text
    assert "auth-api" in page.text
    assert "production" in page.text


def test_the_results_partial_lists_events_and_a_histogram(console):
    body = console.client.get(PARTIAL, params={"app_id": APP_ID})
    assert body.status_code == 200
    assert body.text.count('hx-get="/partials/log-event/') == 6
    assert "histogram-bar" in body.text


def test_a_filter_reaches_the_query(console):
    body = console.client.get(PARTIAL, params={"app_id": APP_ID, "severity": "ERROR"})
    assert body.text.count('hx-get="/partials/log-event/') == 2


def test_the_next_page_link_is_a_cursor_not_an_offset(console):
    body = console.client.get(PARTIAL, params={"app_id": APP_ID, "limit": "2"})
    match = re.search(r"cursor=([A-Za-z0-9_\-=]+)", body.text)
    assert match, "no cursor link rendered"
    assert "offset" not in body.text

    second = console.client.get(
        PARTIAL, params={"app_id": APP_ID, "limit": "2", "cursor": match.group(1)}
    )
    assert second.text.count('hx-get="/partials/log-event/') == 2


def test_a_refused_query_says_so_instead_of_looking_empty(console):
    body = console.client.get(PARTIAL, params={"app_id": APP_ID, "cursor": "!!!"})
    assert body.status_code == 200
    assert "refused" in body.text
    assert "No events match" not in body.text


def test_the_detail_drawer_shows_correlation(console):
    body = console.client.get("/partials/log-event/ev-0", params={"app_id": APP_ID})
    assert body.status_code == 200
    assert "Related events" in body.text
    assert "trace-1" in body.text
    # The survived attributes are shown...
    assert "ap-south-1" in body.text
    assert "Attributes" in body.text


def test_the_detail_drawer_explains_when_every_attribute_was_dropped(console):
    """An event whose attributes were all credential-shaped renders the note
    rather than an empty block, so the absence is explained."""
    ingest = console.key(scope="ingest")
    console.post(
        "/v1/ingest/log-events",
        ingest,
        log_events_payload(
            batch_id="creds",
            events=[log_event(event_id="ev-creds", attributes={"password": "x", "apiKey": "y"})],
        ),
    )
    body = console.client.get("/partials/log-event/ev-creds", params={"app_id": APP_ID})
    assert "credential" in body.text


def test_the_detail_drawer_handles_a_missing_event(console):
    body = console.client.get("/partials/log-event/nope", params={"app_id": APP_ID})
    assert body.status_code == 200
    assert "not found" in body.text


def test_another_projects_event_is_not_reachable_from_the_drawer(console):
    """The drawer is scoped by app_id, so the same event id is a miss elsewhere."""
    from powerbase.provision import provision_app

    provision_app(console.db, "com.other.app", "Other App")
    body = console.client.get("/partials/log-event/ev-0", params={"app_id": "com.other.app"})
    assert body.status_code == 200
    assert "not found" in body.text
    assert "trace-1" not in body.text


def test_saving_the_current_filters(console):
    response = console.client.post(
        "/explorer/save",
        data={"app_id": APP_ID, "name": "errors only", "severity": "ERROR", "days": "7"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash=" in response.headers["location"]

    page = console.client.get(PAGE)
    assert "errors only" in page.text


def test_a_save_with_an_invalid_name_reports_why(console):
    response = console.client.post(
        "/explorer/save",
        data={"app_id": APP_ID, "name": "", "severity": "ERROR"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Not+saved" in response.headers["location"] or "Not%20saved" in response.headers["location"]


def test_loading_a_saved_search_seeds_the_form(console):
    created = console.client.post(
        "/explorer/save",
        data={"app_id": APP_ID, "name": "errored", "severity": "ERROR", "days": "14"},
        follow_redirects=False,
    )
    location = created.headers["location"]

    import urllib.parse

    query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    assert query["severity"] == ["ERROR"]

    page = console.client.get(PAGE, params={"app_id": APP_ID, "saved": "nope"})
    assert page.status_code == 200  # an unknown id degrades rather than 500s


def test_a_saved_search_round_trips_into_the_results(console):
    from powerbase import saved_queries

    saved = saved_queries.create_saved_query(
        console.db,
        APP_ID,
        "errors",
        {"filters": [{"field": "severity", "operator": "eq", "value": "ERROR"}]},
    )
    page = console.client.get(PAGE, params={"app_id": APP_ID, "saved": saved["id"]})
    assert page.status_code == 200
    # The form is seeded, so severity is the selected option.
    assert re.search(r'value="ERROR"\s+selected', page.text)


def test_deleting_a_saved_search_from_the_console(console):
    from powerbase import saved_queries

    saved = saved_queries.create_saved_query(console.db, APP_ID, "temp", {})
    response = console.client.post(
        "/explorer/delete",
        data={"app_id": APP_ID, "query_id": saved["id"]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert saved_queries.get_saved_query(console.db, saved["id"]) is None


def test_deleting_another_projects_search_is_refused(console):
    from powerbase import saved_queries
    from powerbase.provision import provision_app

    provision_app(console.db, "com.other.app", "Other")
    theirs = saved_queries.create_saved_query(console.db, "com.other.app", "theirs", {})

    response = console.client.post(
        "/explorer/delete",
        data={"app_id": APP_ID, "query_id": theirs["id"]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    # Still there: the console scopes the delete to the current project.
    assert saved_queries.get_saved_query(console.db, theirs["id"]) is not None


def test_the_console_needs_a_login(make_env):
    env = make_env(admin_password=ADMIN_PASSWORD)
    assert env.client.get(PAGE, follow_redirects=False).status_code in (303, 307)
    assert env.client.get(PARTIAL, follow_redirects=False).status_code in (303, 307)
