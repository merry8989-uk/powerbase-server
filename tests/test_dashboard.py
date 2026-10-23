"""Dashboard: authentication, page rendering, HTMX partials and the data browser."""

from helpers import ADMIN_PASSWORD, EXPECTED, events_payload


def login(client, password=ADMIN_PASSWORD):
    return client.post(
        "/login", data={"password": password, "next": "/"}, follow_redirects=False
    )


# ----------------------------------------------------------------------- auth

def test_anonymous_is_redirected(seeded_env):
    r = seeded_env.client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_htmx_requests_get_a_redirect_header(seeded_env):
    # Not a 3xx: the browser would follow it inside the XHR and swap the login
    # page into the fragment. htmx navigates on HX-Redirect instead.
    r = seeded_env.client.get(
        "/partials/logs", headers={"HX-Request": "true"}, follow_redirects=False
    )
    assert r.status_code == 200
    assert r.headers.get("HX-Redirect") == "/login"


def test_login_page_renders(seeded_env):
    r = seeded_env.client.get("/login")
    assert r.status_code == 200
    assert "Admin password" in r.text


def test_wrong_password_is_rejected(seeded_env):
    r = login(seeded_env.client, "nope")
    assert r.status_code == 401
    assert "pb_session" not in r.cookies


def test_correct_password_sets_a_hardened_cookie(seeded_env):
    r = login(seeded_env.client)
    assert r.status_code == 303
    cookie_header = r.headers["set-cookie"].lower()
    assert "httponly" in cookie_header
    assert "samesite=lax" in cookie_header
    assert "pb_session" in r.cookies


def test_forged_cookie_is_rejected(seeded_env):
    seeded_env.client.cookies.set("pb_session", "9999999999.deadbeef")
    r = seeded_env.client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_expired_cookie_is_rejected(seeded_env):
    from powerbase import dashboard_auth as auth

    secret = auth.derive_secret(ADMIN_PASSWORD)
    stale = auth.issue_cookie(secret, ttl_seconds=-10)
    seeded_env.client.cookies.set("pb_session", stale)
    assert seeded_env.client.get("/", follow_redirects=False).status_code == 303


def test_login_is_throttled(seeded_env):
    for _ in range(5):
        assert login(seeded_env.client, "wrong").status_code == 401
    blocked = login(seeded_env.client, "wrong")
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers


def test_throttle_does_not_block_the_right_password_forever(seeded_env):
    # A successful login clears the counter, so a user who mistypes twice is fine.
    login(seeded_env.client, "wrong")
    login(seeded_env.client, "wrong")
    assert login(seeded_env.client).status_code == 303


def test_logout_clears_the_cookie(logged_in):
    r = logged_in.get("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


# ---------------------------------------------------------------------- pages

def test_every_page_renders(logged_in):
    for path in ["/", "/logs", "/crashes", "/charts", "/devices", "/sessions", "/data"]:
        r = logged_in.get(path)
        assert r.status_code == 200, path
        assert "Powerbase" in r.text


def test_overview_shows_the_kpis(logged_in):
    r = logged_in.get("/?days=365")
    assert r.status_code == 200
    for label in ["Active devices", "Sessions", "Events", "Crash-free sessions", "Error logs"]:
        assert label in r.text
    assert str(EXPECTED["sessions"]) in r.text


def test_overview_links_to_crash_groups(logged_in):
    assert "/crashes/" in logged_in.get("/?days=365").text


def test_logs_page_has_the_filter_form(logged_in):
    r = logged_in.get("/logs")
    assert 'hx-get="/partials/logs"' in r.text
    assert "full-text" in r.text


def test_logs_partial_renders_rows(logged_in):
    r = logged_in.get("/partials/logs?days=365")
    assert r.status_code == 200
    assert "matching log line" in r.text
    assert "[REDACTED_EMAIL]" in r.text


def test_logs_partial_respects_the_level_filter(logged_in):
    r = logged_in.get("/partials/logs?level=ERROR&days=365")
    assert "level-error" in r.text.lower()
    assert r.text.count("level-error") >= EXPECTED["error_logs"]


def test_logs_partial_paginates(logged_in):
    r = logged_in.get("/partials/logs?days=365&limit=2&offset=0")
    assert "Next" in r.text


def test_log_detail_partial(logged_in, seeded_env):
    log_id = seeded_env.db.scalar("SELECT id FROM logs LIMIT 1")
    r = logged_in.get(f"/partials/log/{log_id}")
    assert r.status_code == 200
    assert "Stacktrace" in r.text or "Message" in r.text


def test_crashes_page_lists_groups(logged_in):
    r = logged_in.get("/crashes?days=365")
    assert r.status_code == 200
    assert "crash group" in r.text.lower()
    assert "NullPointerException" in r.text


def test_crash_detail_page(logged_in, seeded_env):
    fp = seeded_env.db.scalar("SELECT fingerprint FROM crashes LIMIT 1")
    r = logged_in.get(f"/crashes/{fp}?days=365")
    assert r.status_code == 200
    assert fp in r.text
    assert "Occurrences" in r.text


def test_charts_page_renders_a_series(logged_in):
    r = logged_in.get("/charts?metric=events&dimension=screen&days=365")
    assert r.status_code == 200
    assert "seriesChart" in r.text
    assert "Top screen" in r.text


def test_charts_page_rejects_unknown_metric_by_falling_back(logged_in):
    r = logged_in.get("/charts?metric=nonsense&dimension=nonsense")
    assert r.status_code == 200  # falls back to the defaults rather than erroring


def test_charts_script_avoids_the_window_top_collision(logged_in):
    # `const top` at script top level is a SyntaxError in a browser (window.top
    # already exists), and it silently blanked every chart on this page.
    html = logged_in.get("/charts?days=365").text
    assert "const top " not in html
    assert "topValues" in html


def test_range_selector_shows_the_active_value(logged_in):
    # A <select> whose options do not include the active value displays its
    # first option, so a 365-day view claimed to be showing "last 1d".
    for path, value in [("/?days=365", 365), ("/charts?days=365", 365), ("/logs?days=14", 14)]:
        html = logged_in.get(path).text
        assert f'value="{value}" selected' in html, path


def test_devices_page_and_detail(logged_in):
    assert "dev-0" in logged_in.get("/devices").text
    r = logged_in.get("/devices/dev-0")
    assert r.status_code == 200
    assert "Pixel 8" in r.text


def test_unknown_device_is_not_found(logged_in):
    assert logged_in.get("/devices/does-not-exist").status_code == 404


def test_sessions_page_and_detail(logged_in):
    assert "sess-0" in logged_in.get("/sessions?days=365").text
    r = logged_in.get("/sessions/sess-0")
    assert r.status_code == 200
    assert "Event timeline" in r.text


def test_unknown_session_is_not_found(logged_in):
    assert logged_in.get("/sessions/nope").status_code == 404


def test_device_search_filters(logged_in):
    assert "dev-0" in logged_in.get("/devices?search=dev-0").text
    assert "dev-1" not in logged_in.get("/devices?search=dev-0").text


# -------------------------------------------------------------- data browser

def test_data_browser_lists_tables(logged_in):
    r = logged_in.get("/data")
    assert r.status_code == 200
    for table in ["apps", "events", "logs", "crashes", "ingest_batches"]:
        assert table in r.text


def test_data_browser_shows_rows(logged_in):
    r = logged_in.get("/data/events?limit=5")
    assert r.status_code == 200
    assert "20 rows" in r.text or "row" in r.text
    assert "properties" in r.text


def test_data_browser_follows_a_foreign_key(logged_in):
    # A foreign-key cell links to the *referenced* table, filtered by that
    # table's own key column (events.device_id -> devices.id).
    r = logged_in.get("/data/events")
    assert "/data/devices?" in r.text
    assert "/data/sessions?" in r.text
    assert "filter_col=id" in r.text


def test_foreign_key_link_actually_lands_on_the_row(logged_in):
    r = logged_in.get("/data/devices?filter_col=id&filter_val=dev-0")
    assert r.status_code == 200
    assert "1 row" in r.text
    assert "dev-0" in r.text


def test_data_browser_applies_a_filter(logged_in):
    r = logged_in.get("/data/events?filter_col=session_id&filter_val=sess-0")
    assert r.status_code == 200
    assert "5 rows" in r.text


def test_data_browser_sorts(logged_in):
    r = logged_in.get("/data/events?order_by=ts&order=asc")
    assert r.status_code == 200


def test_data_browser_unknown_table(logged_in):
    assert logged_in.get("/data/nope").status_code == 404


# ------------------------------------------------------------------ assets


def test_static_assets_are_served(seeded_env):
    # Static files are not behind the auth dependency; they carry no data.
    css = seeded_env.client.get("/static/app.css")
    js = seeded_env.client.get("/static/app.js")
    assert css.status_code == 200 and "--accent" in css.text
    assert js.status_code == 200 and "function lineChart" in js.text


# ---------------------------------------------------- dashboard switched off

def test_dashboard_disabled_without_a_password(env):
    r = env.client.get("/login")
    assert r.status_code == 503
    assert "disabled" in r.text.lower()


def test_root_is_unavailable_when_disabled(env):
    assert env.client.get("/", follow_redirects=False).status_code == 503


def test_disabling_the_dashboard_does_not_break_ingestion(env):
    key = env.key("ingest")
    r = env.post("/v1/ingest/events", key, events_payload())
    assert r.status_code == 202
    assert env.db.scalar("SELECT COUNT(*) FROM events") == 1
