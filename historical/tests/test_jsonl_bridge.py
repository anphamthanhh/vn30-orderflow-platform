"""Tests for the Rust-to-Python backtest JSONL bridge."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from historical.backtest_engine.jsonl_bridge import (
    compare_runs,
    evaluate_run,
    load_equity,
    load_trades,
    render_report,
)


def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _make_run(tmp_path, name, trades, equity):
    d = tmp_path / name
    d.mkdir()
    _write_jsonl(d / "trades.jsonl", trades)
    _write_jsonl(d / "equity.jsonl", equity)
    return d


# One winning round-trip: enter + exit, net +100,000 VND. Equity spans two ICT
# days so daily-return metrics are non-degenerate.
BASE = 10_800  # 10:00 ICT
DAY = 86_400
TRADES = [
    {"ts": BASE, "side": "long", "action": "enter", "qty": 1, "price": 1250.0, "fee_vnd": 66200.0, "reason": "e"},
    {"ts": BASE + 60, "side": "long", "action": "exit", "qty": 1, "price": 1252.0, "fee_vnd": 66300.0, "pnl_points": 2.0, "pnl_vnd": 100000.0, "reason": "x"},
]
EQUITY = [
    {"ts": BASE, "equity": 100_000_000.0, "position": 1, "unrealized": 0.0, "realized": -66200.0},
    {"ts": BASE + 60, "equity": 100_100_000.0, "position": 0, "unrealized": 0.0, "realized": 100000.0},
    {"ts": BASE + DAY, "equity": 100_100_000.0, "position": 0, "unrealized": 0.0, "realized": 100000.0},
]


def test_load_equity_is_datetime_indexed(tmp_path):
    d = _make_run(tmp_path, "s", TRADES, EQUITY)
    eq = load_equity(d / "equity.jsonl")
    assert isinstance(eq.index, pd.DatetimeIndex)
    assert len(eq) == 3
    assert eq.iloc[-1] == pytest.approx(100_100_000.0)


def test_load_trades_only_exits_and_no_fee_double_count(tmp_path):
    d = _make_run(tmp_path, "s", TRADES, EQUITY)
    tr = load_trades(d / "trades.jsonl")
    assert len(tr) == 1  # only the exit row carries realized P&L
    assert tr["PnL_VND"].iloc[0] == pytest.approx(100000.0)
    assert tr["Fee_VND"].iloc[0] == 0.0  # fees already in pnl_vnd


def test_evaluate_run_metrics(tmp_path):
    d = _make_run(tmp_path, "s", TRADES, EQUITY)
    m = evaluate_run(d)
    assert m["num_trades"] == 1
    assert m["win_rate_pct"] == pytest.approx(100.0)
    # net P&L is the Rust value, not re-charged with fees.
    assert m["net_pnl_vnd"] == pytest.approx(100000.0)
    assert "sharpe_ratio" in m and "max_drawdown_pct" in m


def test_compare_and_report(tmp_path):
    a = _make_run(tmp_path, "cvd", TRADES, EQUITY)
    b = _make_run(tmp_path, "supertrend", TRADES, EQUITY)
    cmp = compare_runs({"cvd": a, "supertrend": b})
    assert list(cmp.index) == ["cvd", "supertrend"]
    assert "sharpe_ratio" in cmp.columns
    report = render_report(cmp)
    assert "Backtest Comparison" in report
    assert "cvd" in report


def test_empty_run_is_safe(tmp_path):
    d = _make_run(tmp_path, "empty", [], [])
    m = evaluate_run(d)
    assert m["num_trades"] == 0
