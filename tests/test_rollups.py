"""Daily rollups: folding raw rows into metrics_daily."""

from datetime import datetime, timezone

from powerbase import rollups
from helpers import APP_ID, dated_day_str, dated_range


def metric(env, day, name, dimension=""):
    return env.db.scalar(
        "SELECT value FROM metrics_daily WHERE day = ? AND app_id = ? AND metric = ? AND dimension = ?",
        (day, APP_ID, name, dimension),
    )


def test_rollup_folds_the_day(dated_env):
    day = dated_day_str(2)
    result = rollups.rollup_day(dated_env.db, day)
    assert result["day"] == day
    assert result["rows"] > 0
    assert metric(dated_env, day, "events") == 3
    assert metric(dated_env, day, "logs") == 0
    # The dated fixture writes events without sessions, and `active_devices`
    # counts distinct devices per session — the same definition `summary` uses.
    assert metric(dated_env, day, "sessions") == 0
    assert metric(dated_env, day, "active_devices") == 0


def test_rollup_is_idempotent(dated_env):
    day = dated_day_str(2)
    rollups.rollup_day(dated_env.db, day)
    rows_after_first = dated_env.db.scalar("SELECT COUNT(*) FROM metrics_daily")
    rollups.rollup_day(dated_env.db, day)
    rollups.rollup_day(dated_env.db, day)

    assert dated_env.db.scalar("SELECT COUNT(*) FROM metrics_daily") == rows_after_first
    assert metric(dated_env, day, "events") == 3  # not 9


def test_rollup_writes_zeros_for_a_quiet_day(dated_env):
    # A day with no activity still gets its base metrics, so the read path can
    # tell "nothing happened" apart from "never rolled up".
    quiet = "2026-02-01"
    rollups.rollup_day(dated_env.db, quiet)
    assert metric(dated_env, quiet, "events") == 0
    assert metric(dated_env, quiet, "sessions") == 0
    assert metric(dated_env, quiet, "session_duration_total_ms") == 0


def test_rollup_writes_dimensions(dated_env):
    day = dated_day_str(2)
    rollups.rollup_day(dated_env.db, day)
    assert metric(dated_env, day, "events_by_screen", "HomeActivity") == 3
    assert metric(dated_env, day, "events_by_name", "screen_view") == 3


def test_rollup_records_the_injected_clock(dated_env):
    # The ledger must be written with the job's own `now`, not the wall clock,
    # or the scheduler's "already ran today?" guard compares two different clocks.
    moment = datetime(2026, 1, 11, 3, tzinfo=timezone.utc)
    rollups.rollup_day(dated_env.db, dated_day_str(2), now=moment)
    assert rollups.recent_runs(dated_env.db)[0]["ran_at"] == "2026-01-11T03:00:00.000000Z"
    assert rollups.ran_on(dated_env.db, "rollup", "2026-01-11") is True


def test_rollup_reproduces_the_session_dataset(seeded_env):
    from datetime import timedelta

    from helpers import EXPECTED

    now = datetime.now(timezone.utc)
    for offset in range(1, 7):
        rollups.rollup_day(
            seeded_env.db,
            (now - timedelta(days=offset)).strftime("%Y-%m-%d"),
            now=now,
        )

    totals = rollups.totals(
        seeded_env.db, APP_ID,
        since=now - timedelta(days=7),
        until=now,
    )
    assert totals["sessions"] == EXPECTED["sessions"]
    assert totals["events"] == EXPECTED["events"]
    assert totals["crashes"] == EXPECTED["crashes"]
    assert totals["error_logs"] == EXPECTED["error_logs"]
    assert totals["active_devices"] == EXPECTED["devices"]
    assert totals["session_duration_total_ms"] == 4 * 120_000


def test_rollup_covers_every_app(dated_env):
    day = dated_day_str(2)
    result = rollups.rollup_day(dated_env.db, day)
    assert result["apps"] >= 1
    assert metric(dated_env, day, "events") is not None


def test_backfill_folds_a_range(dated_env):
    result = rollups.backfill(dated_env.db, days=3, now=datetime(2026, 1, 11, tzinfo=timezone.utc))
    assert result["days"] == 3
    assert [entry["day"] for entry in result["folded"]] == ["2026-01-08", "2026-01-09", "2026-01-10"]


def test_previous_day_is_the_last_complete_day():
    assert rollups.previous_day(datetime(2026, 1, 11, 0, 30, tzinfo=timezone.utc)) == "2026-01-10"


def test_day_bounds_are_half_open():
    start, end = rollups.day_bounds("2026-01-04")
    assert start == "2026-01-04T00:00:00.000000Z"
    assert end == "2026-01-05T00:00:00.000000Z"


def test_coverage_reflects_successful_runs(dated_env):
    assert rollups.coverage(dated_env.db, APP_ID) == (None, None)

    for day in dated_range()[:3]:
        rollups.rollup_day(dated_env.db, day)

    first, last = rollups.coverage(dated_env.db, APP_ID)
    assert first == dated_day_str(0)
    assert last == dated_day_str(2)


def test_job_ledger_records_the_run(dated_env):
    day = dated_day_str(1)
    rollups.rollup_day(dated_env.db, day)

    runs = rollups.recent_runs(dated_env.db)
    assert runs[0]["job"] == "rollup"
    assert runs[0]["day"] == day
    assert runs[0]["ok"] == 1

    assert rollups.last_run(dated_env.db, "rollup")["day"] == day
    # `rolled_up` asks about the data day; `ran_on` asks about the wall clock.
    assert rollups.rolled_up(dated_env.db, day) is True
    assert rollups.rolled_up(dated_env.db, "2026-01-09") is False
    today = rollups.recent_runs(dated_env.db)[0]["ran_at"][:10]
    assert rollups.ran_on(dated_env.db, "rollup", today) is True
    assert rollups.ran_on(dated_env.db, "backup", today) is False


def test_series_reader_returns_daily_points(dated_env):
    for day in dated_range():
        rollups.rollup_day(dated_env.db, day)

    points = rollups.series(
        dated_env.db,
        APP_ID,
        "events",
        since=datetime(2026, 1, 5, tzinfo=timezone.utc),
        until=datetime(2026, 1, 8, tzinfo=timezone.utc),
    )
    assert [p["bucket"] for p in points] == ["2026-01-05", "2026-01-06", "2026-01-07"]
    assert [p["value"] for p in points] == [3, 3, 3]


def test_totals_reader(dated_env):
    for day in dated_range():
        rollups.rollup_day(dated_env.db, day)

    totals = rollups.totals(
        dated_env.db, APP_ID,
        since=datetime(2026, 1, 4, tzinfo=timezone.utc),
        until=datetime(2026, 1, 11, tzinfo=timezone.utc),
    )
    assert totals["events"] == 21
    assert totals["sessions"] == 0
