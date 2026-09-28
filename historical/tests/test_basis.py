"""Tests for the causal basis signals (``lab.basis_signals``) and the ``basis_eval`` scorer."""

from __future__ import annotations

import numpy as np
import pandas as pd

from historical.research_lab import basis_eval, lab


def _daily(n=80, seed=0):
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    basis = rng.normal(0, 1.0, n)
    basis[40] = 6.0  # extreme rich day: fades short
    basis[41] = -6.0  # extreme cheap day: fades long
    f_close = 1900.0 + np.cumsum(rng.normal(0, 1.0, n))
    return pd.DataFrame(
        {"d": dates, "basis_eod": basis, "basis_avg": basis, "f_open": f_close, "f_close": f_close}
    )


# --- signals ---
def test_basis_signals_causal_columns_and_fade_direction():
    sig = lab.basis_signals(_daily(), lookback=20)
    for c in ("basis_z", "thresh", "fire", "would_signal", "first_of_session"):
        assert c in sig.columns
    assert set(sig["would_signal"].unique()).issubset({"", "long", "short"})
    # rich extreme (z>0) fades to short; cheap extreme (z<0) fades to long
    rich = sig.iloc[40]
    cheap = sig.iloc[41]
    if rich["fire"]:
        assert rich["would_signal"] == "short"
    if cheap["fire"]:
        assert cheap["would_signal"] == "long"


def test_basis_signals_threshold_is_causal_shifted():
    # thresh is a trailing rolling quantile shifted by 1, so the first lookback rows are NaN
    sig = lab.basis_signals(_daily(), lookback=20)
    assert sig["thresh"].iloc[:20].isna().all()


def test_load_basis_daily_guard_survives_the_pandas_ns_us_trap(monkeypatch):
    """Dates inside the dev window pass the guard with pandas 3 microsecond datetimes.

    ``astype("int64")`` on datetimes depends on the resolution (ns or us), so the
    epoch-day computation must not assume nanoseconds. ClickHouse is mocked.
    """
    from historical.research_lab import basis_screen

    in_window = pd.date_range("2026-03-10", periods=5, freq="B")
    fake = pd.DataFrame(
        {
            "d": in_window.strftime("%Y-%m-%d"),
            "f_open": 1900.0,
            "f_close": 1900.0,
            "basis_eod": 0.0,
            "basis_avg": 0.0,
        }
    )
    monkeypatch.setattr(basis_screen, "load_daily_ch", lambda ch=None: fake)
    out = lab.load_basis_daily()  # every date is inside the dev window
    assert len(out) == 5


def test_load_basis_daily_guard_still_rejects_reserved_dates(monkeypatch):
    """A row in the reserved date range is still rejected by the epoch-day guard."""
    from historical.research_lab import basis_screen

    dates = ["2025-08-01"] + list(
        pd.date_range("2026-03-10", periods=4, freq="B").strftime("%Y-%m-%d")
    )
    fake = pd.DataFrame(
        {"d": dates, "f_open": 1900.0, "f_close": 1900.0, "basis_eod": 0.0, "basis_avg": 0.0}
    )
    monkeypatch.setattr(basis_screen, "load_daily_ch", lambda ch=None: fake)
    try:
        lab.load_basis_daily()
        assert False, "expected AssertionError for the reserved 2025-08-01 row"
    except AssertionError as e:
        assert "basis_m1" in str(e) and "OUTSIDE" in str(e)


# --- forward evaluation ---
def _ledger(n, gross, fee=2.0, net=None, start="2025-01-01"):
    net = (gross - fee) if net is None else net
    dates = pd.date_range(start, periods=n, freq="7D")
    return pd.DataFrame(
        {"date_D1": dates.astype(str), "gross_pts": gross, "fee_pts": fee, "net_pts": net}
    )


def _sessions(n_accrued, start="2026-08-17", missing=0):
    """Build a ``sessions.jsonl``-shaped list with one row per expected trading session.

    Duration counts accrued sessions: six months means at least 126 (21 a month).
    """
    dates = pd.date_range(start, periods=n_accrued + missing, freq="B").astype(str)
    rows = [{"date": d, "status": "accrued"} for d in dates[:n_accrued]]
    rows += [{"date": d, "status": "MISSING_DATA"} for d in dates[n_accrued:]]
    return rows


def test_basis_eval_accruing_below_threshold():
    res = basis_eval.evaluate(_ledger(10, gross=4.0))  # <60 signals
    assert res["verdict"] == "ACCRUING"
    # below the pre-registered minimum no performance statistic is reported
    for leaked in ("hit_rate", "net_per_trade_pts", "total_net_pts", "criteria"):
        assert leaked not in res, f"{leaked} leaked below the gate"


def test_basis_eval_empty_is_accruing():
    res = basis_eval.evaluate(pd.DataFrame())
    assert res["n_signals"] == 0 and res["verdict"] == "ACCRUING"


def test_basis_eval_confirmed_when_enough_and_clears_worse_side():
    # 60 signals and >= 126 accrued sessions; gross 4 points > worse-side cost 3 (2 + 1)
    res = basis_eval.evaluate(_ledger(60, gross=4.0, fee=2.0, net=1.0), sessions=_sessions(130))
    assert res["verdict"] == "FORWARD-CONFIRMED", res
    assert res["criteria"]["1_gross_per_trade>worse_side_cost"] is True


def test_basis_eval_kill_when_below_worse_side():
    # enough data, but gross 2.5 points < worse-side cost 3 points
    res = basis_eval.evaluate(_ledger(60, gross=2.5, fee=2.0, net=-0.1), sessions=_sessions(130))
    assert res["verdict"] == "KILL", res


def test_basis_eval_enough_signals_but_too_few_sessions_still_accrues():
    """60 signals over 40 sessions are not six months of evidence."""
    res = basis_eval.evaluate(_ledger(60, gross=4.0, fee=2.0, net=1.0), sessions=_sessions(40))
    assert res["verdict"] == "ACCRUING"


def test_basis_eval_blocks_when_missing_sessions_exceed_budget():
    """Missing sessions beyond the budget block the verdict."""
    res = basis_eval.evaluate(
        _ledger(60, gross=4.0, fee=2.0, net=1.0), sessions=_sessions(130, missing=40)
    )
    assert res["verdict"] == "BLOCKED_MISSING_DATA", res
    assert res["missing_sessions"] == 40
