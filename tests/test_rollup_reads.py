"""The read path: rollups for whole days, raw for the partial edges.

The important property is that turning rollups on changes *where* a number comes
from, never *what* the number is.
"""

from datetime import datetime, timezone

import pytest

from powerbase import queries, rollups
from helpers import APP_ID, dated_range

SINCE = datetime(2026, 1, 5, 6, tzinfo=timezone.utc)
UNTIL = datetime(2026, 1, 10, 18, tzinfo=timezone.utc)
EXPECTED_TOTAL = 17  # 01-05..01-09 full days (3 each) + a partial 01-10 (2)


def rollup_first_six(env):
    for day in dated_range()[:6]:
        rollups.rollup_day(env.db, day)


def series(env, **kwargs):
    return queries.timeseries(
        env.db, APP_ID, "events", bucket="day", since=SINCE, until=UNTIL, **kwargs
    )


def test_source_is_raw_without_rollups(dated_env):
    assert queries.series_source(dated_env.db, APP_ID, "day", SINCE, UNTIL) == "raw"
    assert sum(p["value"] for p in series(dated_env)) == EXPECTED_TOTAL


def test_source_becomes_rollup_once_the_range_is_covered(dated_env):
    rollup_first_six(dated_env)
    assert queries.series_source(dated_env.db, APP_ID, "day", SINCE, UNTIL) == "rollup"


def test_source_stays_raw_when_rollups_do_not_cover_the_start(dated_env):
    # Only the last few days are rolled up, so the middle stretch is not covered.
    for day in dated_range()[4:]:
        rollups.rollup_day(dated_env.db, day)
    assert queries.series_source(dated_env.db, APP_ID, "day", SINCE, UNTIL) == "raw"


def test_merged_series_is_identical_to_raw(dated_env):
    raw = series(dated_env, source="raw")

    rollup_first_six(dated_env)
    merged = series(dated_env)

    assert merged == raw, "switching to rollups must not change a single point"
    assert sum(p["value"] for p in merged) == EXPECTED_TOTAL


def test_merged_series_keeps_the_partial_edges_from_raw(dated_env):
    rollup_first_six(dated_env)
    points = {p["bucket"]: p["value"] for p in series(dated_env)}

    # The head day is partial (starts at 06:00) and comes from raw.
    assert points["2026-01-05"] == 3
    # The tail day is partial (ends before 18:00) and comes from raw.
    assert points["2026-01-10"] == 2
    # The whole days in between come from rollups.
    assert points["2026-01-07"] == 3


def test_hourly_never_uses_rollups(dated_env):
    rollup_first_six(dated_env)
    assert queries.series_source(dated_env.db, APP_ID, "hour", SINCE, UNTIL) == "raw"
    hourly = queries.timeseries(
        dated_env.db, APP_ID, "events", bucket="hour", since=SINCE, until=UNTIL
    )
    assert len(hourly) > 5  # more buckets than there are days


def test_forcing_the_rollup_source_without_bounds_is_an_error(dated_env):
    with pytest.raises(ValueError):
        queries.timeseries(dated_env.db, APP_ID, "events", bucket="day", source="rollup")


def test_unknown_metric_still_raises(dated_env):
    with pytest.raises(ValueError):
        queries.timeseries(dated_env.db, APP_ID, "nonsense", bucket="day", since=SINCE, until=UNTIL)


def test_unknown_bucket_still_raises(dated_env):
    with pytest.raises(ValueError):
        queries.timeseries(dated_env.db, APP_ID, "events", bucket="minute", since=SINCE, until=UNTIL)


# ------------------------------------------------------------------- the API

def api_series(env, **params):
    headers = env.headers(env.key("read"))
    query = {
        "metric": "events",
        "bucket": "day",
        "since": "2026-01-05T06:00:00Z",
        "until": "2026-01-10T18:00:00Z",
    }
    query.update(params)
    return env.client.get("/v1/metrics/timeseries", params=query, headers=headers).json()


def test_api_reports_the_source(dated_env):
    assert api_series(dated_env)["source"] == "raw"
    assert sum(p["value"] for p in api_series(dated_env)["points"]) == EXPECTED_TOTAL

    rollup_first_six(dated_env)
    body = api_series(dated_env)
    assert body["source"] == "rollup"
    assert sum(p["value"] for p in body["points"]) == EXPECTED_TOTAL


def test_api_points_are_unchanged_by_the_switch(dated_env):
    before = api_series(dated_env)["points"]
    rollup_first_six(dated_env)
    after = api_series(dated_env)["points"]
    assert before == after
