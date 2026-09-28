"""Tests for the M1 OHLCV CSV validator (``data_provider/etl``) that gates ClickHouse loads."""

from __future__ import annotations

import pandas as pd

from data_provider.etl.validate_vn30f1m_csv import (
    check_duplicates,
    check_jumps,
    check_ohlc,
    check_ordering,
    check_sessions,
    prepare,
)


def _frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df["ticker"] = "VN30F1M"
    return prepare(df)


def _row(ts: str, o=100.0, h=101.0, low=99.0, c=100.5, v=10):
    return {"timestamp": ts, "open": o, "high": h, "low": low, "close": c, "volume": v}


def test_ohlc_violations_detected() -> None:
    df = _frame(
        [
            _row("2026-01-05 09:00:00"),  # clean
            _row("2026-01-05 09:01:00", h=99.5),  # high below body
            _row("2026-01-05 09:02:00", low=100.6),  # low above body
        ]
    )
    r = check_ohlc(df)
    assert r["status"] == "FAIL"
    assert r["high_below_body"] == 1 and r["low_above_body"] == 1


def test_conflicting_duplicates_fail_exact_duplicates_pass() -> None:
    clean = _frame([_row("2026-01-05 09:00:00"), _row("2026-01-05 09:00:00")])
    assert check_duplicates(clean)["status"] == "PASS"  # exact dup = removable
    conflict = _frame([_row("2026-01-05 09:00:00"), _row("2026-01-05 09:00:00", c=200.0)])
    r = check_duplicates(conflict)
    assert r["status"] == "FAIL" and r["conflicting_duplicates"] == 1


def test_ordering_detects_regression() -> None:
    df = _frame([_row("2026-01-05 09:01:00"), _row("2026-01-05 09:00:00")])
    assert check_ordering(df)["status"] == "FAIL"


def test_sessions_flags_out_of_window_and_accepts_atc() -> None:
    df = _frame(
        [
            _row("2026-01-05 09:00:00"),
            _row("2026-01-05 14:45:00"),  # ATC bar — valid
            _row("2026-01-05 12:15:00"),  # lunch — outside
            _row("2026-01-10 10:00:00"),  # Saturday — weekend
        ]
    )
    r = check_sessions(df)
    assert r["status"] == "WARN"
    assert r["rows_outside_session"] == 1 and r["weekend_rows"] == 1 and r["atc_bars"] == 1


def test_jumps_ignore_lunch_and_overnight_boundaries() -> None:
    df = _frame(
        [
            _row("2026-01-05 11:29:00", c=100.0),
            _row("2026-01-05 13:00:00", c=110.0),  # +10% across lunch — exempt
            _row("2026-01-05 13:01:00", c=110.1),
            _row("2026-01-05 13:02:00", c=120.0),  # +9% intraday — flagged
            _row("2026-01-06 09:00:00", c=100.0),  # overnight gap — exempt
        ]
    )
    r = check_jumps(df, max_jump_pct=3.0)
    assert r["intraday_jumps"] == 1
    assert r["overnight_gaps_gt_threshold"] == 1
    assert r["status"] == "WARN"
