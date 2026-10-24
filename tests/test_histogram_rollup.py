"""The explorer histogram reads from the rollup when the rollup can answer it.

Grouping `log_events` on every request is a scan of the whole filtered range —
the most expensive thing the console does, and it gets worse with every row
ingested. `metrics_daily` already holds the answer per day.

Two things have to be true for this to be worth having, and they are what these
tests are about:

1. **It must agree with the raw answer.** A faster wrong number is worse than a
   slow right one.
2. **It must not answer a question it cannot answer.** `metrics_daily` stores
   totals, not a breakdown, so a filtered histogram cannot come from it. Getting
   that wrong returns unfiltered numbers under a filter — which is exactly the
   bug the first version of this had.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from helpers import APP_ID
from powerbase import logquery, queries, rollups

NOW = datetime.now(timezone.utc)


@pytest.fixture()
def rolled(make_env):
    """Ten days of events, with every whole day but today folded into the rollup."""
    env = make_env()
    rng = random.Random(11)
    rows = []
    for i in range(3000):
        when = NOW - timedelta(hours=rng.randrange(24 * 10))
        severity = rng.choice(["INFO"] * 8 + ["WARN", "ERROR", "FATAL"])
        rows.append(
            (f"e{i}", APP_ID, when.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
             NOW.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "server_sdk", severity,
             "http.request", "{}", 1)
        )
    with env.db.write() as conn:
        conn.executemany(
            "INSERT INTO log_events (event_id, app_id, event_time, received_at, source, "
            "severity, event_type, attributes, redaction_version) VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
    for back in range(1, 10):
        rollups.rollup_day(env.db, (NOW - timedelta(days=back)).strftime("%Y-%m-%d"))
    env.since = (NOW - timedelta(days=9)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    env.until = NOW.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return env


def stats(env, **kw):
    return logquery.log_event_stats(
        env.db, app_id=APP_ID, since=env.since, until=env.until, **kw
    )


def error_filter():
    return logquery.parse_filters(
        [{"field": "severity", "operator": "eq", "value": "ERROR"}]
    )


# ------------------------------------------------------------- which storage
def test_a_day_histogram_comes_from_the_rollup(rolled):
    assert stats(rolled, bucket="day")["source"] == "rollup"


def test_an_hour_histogram_comes_from_raw_rows(rolled):
    """`metrics_daily` is daily by design; there is no hourly rollup to read."""
    assert stats(rolled, bucket="hour")["source"] == "raw"


def test_a_filtered_histogram_comes_from_raw_rows(rolled):
    """The rollup stores totals, not a breakdown, so it cannot answer this."""
    assert stats(rolled, bucket="day", filters=error_filter())["source"] == "raw"


def test_a_searched_histogram_comes_from_raw_rows(rolled):
    assert stats(rolled, bucket="day", q="timeout")["source"] == "raw"


def test_a_blank_search_still_uses_the_rollup(rolled):
    """An empty search box is not a search."""
    assert stats(rolled, bucket="day", q="   ")["source"] == "rollup"


# --------------------------------------------------------------- agreement
def test_the_rollup_total_matches_the_raw_total(rolled):
    """The number a user sees must not change because the storage did."""
    day = stats(rolled, bucket="day")
    hour = stats(rolled, bucket="hour")
    assert day["source"] == "rollup" and hour["source"] == "raw"
    assert day["total"] == hour["total"]


def test_the_rollup_severity_breakdown_matches_raw(rolled):
    day = stats(rolled, bucket="day")
    hour = stats(rolled, bucket="hour")
    assert day["by_severity"] == hour["by_severity"]


def test_a_filtered_total_is_a_subset_of_the_unfiltered_one(rolled):
    """If a filter were ignored the totals would be equal, which is the bug."""
    everything = stats(rolled, bucket="day")["total"]
    errors_only = stats(rolled, bucket="day", filters=error_filter())["total"]
    assert errors_only < everything
    assert errors_only == stats(rolled, bucket="day")["by_severity"]["ERROR"]


def test_every_bucket_count_matches(rolled):
    day = {b["bucket"]: (b["count"], b["errors"]) for b in stats(rolled, bucket="day")["buckets"]}
    hour = stats(rolled, bucket="hour")["buckets"]
    # Fold the hourly buckets down to days and compare.
    folded: dict[str, list[int]] = {}
    for item in hour:
        key = item["bucket"][:10]
        slot = folded.setdefault(key, [0, 0])
        slot[0] += item["count"]
        slot[1] += item["errors"]
    for key, value in folded.items():
        assert day.get(key) == tuple(value), f"{key}: rollup {day.get(key)} vs raw {tuple(value)}"


# --------------------------------------------------------------- the merge
def test_today_comes_from_raw_rows_and_earlier_days_from_the_rollup(rolled):
    """Today has not been rolled up yet, so the range spans both. Both are read,
    and nothing is counted twice — the buckets meet on a day boundary."""
    merged = stats(rolled, bucket="day")
    today = NOW.strftime("%Y-%m-%d")
    assert merged["source"] == "rollup", "the covered middle still came from the rollup"
    assert any(b["bucket"] == today for b in merged["buckets"]), "and today is present"
    # Today's rows exist only in `log_events`, so they can only have come raw.
    # Bounded above by `until`, because the tail stretch is half-open: rows at
    # exactly `until` belong to no bucket. The seed puts a dozen rows there by
    # coincidence (240 hourly offsets, so `randrange` returns 0 about 1 time in
    # 240), which is what made an unbounded count disagree with the histogram.
    raw_today = rolled.db.scalar(
        "SELECT COUNT(*) FROM log_events WHERE app_id = ? AND event_time >= ? AND event_time < ?",
        (APP_ID, NOW.strftime("%Y-%m-%dT00:00:00.000000Z"), rolled.until),
    )
    assert raw_today > 0
    assert next(b for b in merged["buckets"] if b["bucket"] == today)["count"] == raw_today


def test_no_double_counting_across_the_boundary(rolled):
    merged = stats(rolled, bucket="day")["total"]
    raw = stats(rolled, bucket="hour")["total"]
    assert merged == raw, "a row counted in both stretches would inflate this"


# ------------------------------------------------------------- without rollups
def test_without_any_rollup_it_falls_back(env):
    """A fresh install has no rollups at all, and must still work."""
    assert rollups.coverage(env.db, APP_ID) == (None, None)
    body = logquery.log_event_stats(
        env.db, app_id=APP_ID, since=env.since if hasattr(env, "since") else None,
        until=None, bucket="day",
    )
    assert body["source"] == "raw"


def test_an_open_ended_range_uses_raw_rows(rolled):
    """The merge needs both bounds to find the whole days in the middle."""
    body = logquery.log_event_stats(rolled.db, app_id=APP_ID, since=None, bucket="day")
    assert body["source"] == "raw"


def test_the_usability_check_is_honest_about_filters(rolled):
    """The first version documented the filter rule and had no parameter for it,
    so a filtered histogram came back from the rollup with unfiltered numbers."""
    since = datetime.fromisoformat(rolled.since.replace("Z", "+00:00"))
    until = datetime.fromisoformat(rolled.until.replace("Z", "+00:00"))
    assert queries.histogram_is_rollup_usable(
        rolled.db, APP_ID, bucket="day", since=since, until=until
    )
    assert not queries.histogram_is_rollup_usable(
        rolled.db, APP_ID, bucket="day", since=since, until=until, filters=error_filter()
    )
    assert not queries.histogram_is_rollup_usable(
        rolled.db, APP_ID, bucket="day", since=since, until=until, q="timeout"
    )
