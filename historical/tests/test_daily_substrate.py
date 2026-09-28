"""Tests for the daily bar substrate, expiry calendar and screen statistics."""

from __future__ import annotations

import numpy as np
import pandas as pd

from historical.research_lab.daily import (
    daily_bars,
    expiry_days,
    tag_calendar,
    third_thursday,
    window_stat,
)
from historical.research_lab.run_screens import c3_stats, wilson_ci


def _m1(rows):
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["timestamp"])
    df["date"] = df["ts"].dt.date
    return df


def test_daily_bars_aggregate_ohlcv():
    m1 = _m1(
        [
            {
                "timestamp": "2023-01-05 09:00:00",
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100.5,
                "volume": 10,
            },
            {
                "timestamp": "2023-01-05 14:45:00",
                "open": 100.5,
                "high": 103,
                "low": 100,
                "close": 102,
                "volume": 20,
            },
            {
                "timestamp": "2023-01-06 09:00:00",
                "open": 102.5,
                "high": 104,
                "low": 102,
                "close": 103,
                "volume": 5,
            },
        ]
    )
    # dates lie in the dev window, so daily_bars passes its holdout guard without allow_holdout
    d = daily_bars(m1)
    assert len(d) == 2
    r = d.iloc[0]
    assert r["open"] == 100 and r["close"] == 102 and r["high"] == 103 and r["low"] == 99
    assert r["volume"] == 30 and r["n_bars"] == 2


def test_third_thursday_known_dates():
    assert third_thursday(2020, 5) == pd.Timestamp("2020-05-21")  # a known expiry date
    assert third_thursday(2026, 1) == pd.Timestamp("2026-01-15")


def test_expiry_shifts_back_when_thursday_missing():
    # the nominal third Thursday is not a trading day, so expiry moves back to the 14th
    days = pd.to_datetime(["2026-01-13", "2026-01-14", "2026-01-16", "2026-01-19"])
    daily = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}, index=days)
    flags = expiry_days(daily)
    assert bool(flags.loc[pd.Timestamp("2026-01-14")])
    assert flags.sum() == 1


def test_tag_calendar_overnight_and_roll_boundary():
    days = pd.bdate_range("2026-01-12", periods=6)  # Monday to Monday, incl. the third Thursday
    daily = pd.DataFrame(
        {
            "open": [100, 102, 101, 105, 103, 104],
            "high": [101, 103, 106, 106, 104, 105],
            "low": [99, 101, 100, 102, 102, 103],
            "close": [100.5, 101.5, 105, 103.5, 103.5, 104.5],
            "volume": 1,
            "n_bars": 1,
        },
        index=days,
    )
    df = tag_calendar(daily)
    # overnight for day 2 = open 102 - previous close 100.5
    assert abs(df["overnight"].iloc[1] - 1.5) < 1e-9
    assert abs(df["intraday"].iloc[0] - 0.5) < 1e-9
    # the third Thursday is expiry; the next day is the roll boundary
    assert bool(df.loc[pd.Timestamp("2026-01-15"), "is_expiry"])
    assert bool(df.loc[pd.Timestamp("2026-01-16"), "roll_boundary"])


def test_window_stat_and_wilson():
    s = window_stat(pd.Series([1.0, 1.0, 1.0, 1.0]))
    assert s["n"] == 4 and s["mean"] == 1.0  # zero variance: t-stat guarded
    lo, hi = wilson_ci(8, 10)
    assert 0.4 < lo < 0.8 < hi <= 1.0


def test_c3_persistent_series_shows_positive_delta():
    # a long run of range days (efficiency near 0), then trend days (near 1): persistent
    n = 200
    trend_block = {"open": 100.0, "high": 110.0, "low": 99.0, "close": 109.5}
    range_block = {"open": 100.0, "high": 105.0, "low": 95.0, "close": 100.2}
    rows = [range_block] * (n // 2) + [trend_block] * (n // 2)
    df = pd.DataFrame(rows, index=pd.bdate_range("2020-01-01", periods=n))
    st = c3_stats(df)
    assert st["delta"] > 0.3 and st["ci_excludes_uncond"]


def test_c3_alternating_series_shows_negative_delta():
    n = 200
    rows = []
    for i in range(n):
        if i % 2 == 0:
            rows.append({"open": 100.0, "high": 105.0, "low": 95.0, "close": 100.2})  # range
        else:
            rows.append({"open": 100.0, "high": 110.0, "low": 99.0, "close": 109.5})  # trend
    df = pd.DataFrame(rows, index=pd.bdate_range("2020-01-01", periods=n))
    st = c3_stats(df)
    assert st["delta"] < 0  # anti-persistent
