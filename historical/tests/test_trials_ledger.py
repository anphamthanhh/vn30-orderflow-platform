"""Tests for the trials ledger, trials-deflated Sharpe, Stage 3 wiring and forward gap."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from historical.optimization import trials_ledger as tl
from historical.optimization.gate_report import _stage3_overfitting, forward_gap

# resolved via the module, independent of the working directory
LEDGER = tl.default_ledger_path()
DAY = 86_400
BASE = 1_700_000_000


def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


# --- shipped ledger ---


def test_shipped_ledger_parses_and_totals():
    recs = tl.load_trials(LEDGER)
    assert len(recs) >= 12
    sharpes = tl.trial_sharpes(recs)
    assert all(np.isfinite(s) for s in sharpes)
    # records flagged estimate=True carry no measured Sharpe and are excluded by default
    assert len(sharpes) < len(recs), "expected some records to be flagged as estimates"
    assert len(sharpes) >= 2, "need dispersion for any deflation to be possible"
    # estimates still count toward N, but not toward the measured dispersion
    with_estimates = tl.trial_sharpes(recs, include_estimates=True)
    assert len(with_estimates) > len(sharpes)


def test_shipped_ledger_satisfies_schema_v2_invariants():
    """Check invariants that hold at any ledger size rather than a fixed total."""
    recs = tl.load_trials(LEDGER)
    problems = tl.validate_ledger(recs)
    assert problems == [], problems
    # v2 forbids n_combos = 0, so a re-screen of the same data always adds to N
    assert all(r.n_combos >= 1 for r in recs)
    # unique uids keep "supersedes" pointers unambiguous
    uids = [r.uid for r in recs]
    assert len(uids) == len(set(uids)), "duplicate uid — supersede pointers are ambiguous"
    # every record counts at least once toward N
    assert tl.total_trials(recs) >= len(recs)


def test_missing_ledger_is_empty_not_error():
    assert tl.load_trials("does/not/exist.jsonl") == []


# --- trials-deflated Sharpe ---


FOOTING = tl.FOOTING_PER_DAILY


def test_deflate_refuses_when_ledger_too_thin():
    """A single record has no dispersion, so deflation is unavailable and says why."""
    one = [tl.TrialRecord("x", "f", 10, 0.1, "KILL", sharpe_footing=FOOTING)]
    r = np.random.default_rng(0).normal(0.05, 1.0, 40)
    res = tl.deflated_sharpe_for_returns(r, one, FOOTING)
    assert res.available is False
    assert "no dispersion" in res.reason or "<2" in res.reason


def test_deflate_refuses_on_footing_mismatch():
    """Per-trade and per-day Sharpe differ by about sqrt(trades per day), so mixing raises."""
    recs = [
        tl.TrialRecord(f"n{i}", "noise", 1, 0.1 * i, "KILL", sharpe_footing=tl.FOOTING_PER_DAILY)
        for i in range(5)
    ]
    r = np.random.default_rng(0).normal(0.05, 1.0, 40)
    with pytest.raises(tl.FootingMismatch):
        tl.deflated_sharpe_for_returns(r, recs, tl.FOOTING_PER_TRADE)


def test_best_of_noise_is_deflated():
    """The best of 200 pure-noise trials does not clear the 0.95 DSR gate."""
    rng = np.random.default_rng(7)
    n_obs = 30
    sr_trials = [
        float(x.mean() / x.std(ddof=1)) for x in (rng.normal(0.0, 1.0, n_obs) for _ in range(200))
    ]
    recs = [
        tl.TrialRecord(f"n{i}", "noise", 1, s, "KILL", sharpe_footing=FOOTING)
        for i, s in enumerate(sr_trials)
    ]
    winner = rng.normal(0.0, 1.0, n_obs)
    res = tl.deflated_sharpe_for_returns(winner, recs, FOOTING)
    assert res.available is True, res.reason
    assert res.n_trials >= 200 and res.var_sr > 0
    assert 0.0 <= res.dsr <= 1.0
    assert res.dsr < 0.95


# --- Stage 3 wiring ---


def _ledger(var_target=0.01, n=200):
    # two trial Sharpes at +/- sqrt(var_target); n_combos totals n
    lo, hi = -(var_target**0.5), (var_target**0.5)
    return [
        tl.TrialRecord("a", "f", n // 2, lo, "KILL", sharpe_footing=FOOTING),
        tl.TrialRecord("b", "f", n // 2, hi, "KILL", sharpe_footing=FOOTING),
    ]


def _deflation(returns):
    """Deflate on the daily footing; Stage 3's other checks use per-trade returns."""
    return tl.deflated_sharpe_for_returns(returns, _ledger(), FOOTING)


def test_stage3_adds_passing_trials_check_for_strong_candidate():
    strong = np.random.default_rng(1).normal(1.0, 1.0, 60)  # per-observation Sharpe about 1
    stage, _psr, _flags = _stage3_overfitting(strong, seed=11, deflation=_deflation(strong))
    check = next((c for c in stage.checks if c.name == "deflated_sharpe_trials"), None)
    assert check is not None
    assert check.passed is True


def test_stage3_trials_check_fails_weak_candidate():
    weak = np.random.default_rng(2).normal(0.03, 1.0, 21)  # tiny edge, short N
    stage, _psr, _flags = _stage3_overfitting(weak, seed=11, deflation=_deflation(weak))
    check = next((c for c in stage.checks if c.name == "deflated_sharpe_trials"), None)
    assert check is not None
    assert check.passed is False


def test_stage3_without_a_deflation_fails_loud_by_default():
    """Without a deflation N is unknown, so the check fails instead of being skipped."""
    r = np.random.default_rng(3).normal(1.0, 1.0, 60)
    stage, _psr, _flags = _stage3_overfitting(r, seed=11, deflation=None)
    check = next((c for c in stage.checks if c.name == "deflated_sharpe_trials"), None)
    assert check is not None, "missing ledger must surface as a FAILING check, not a silent skip"
    assert check.passed is False


def test_stage3_can_opt_out_of_trials_deflation_explicitly():
    """``require_trials_deflation=False`` is an explicit opt-out, never the default."""
    r = np.random.default_rng(3).normal(1.0, 1.0, 60)
    stage, _psr, _flags = _stage3_overfitting(
        r, seed=11, deflation=None, require_trials_deflation=False
    )
    check = next((c for c in stage.checks if c.name == "deflated_sharpe_trials"), None)
    assert check is None or check.passed is not False


# --- forward gap ---


def _shadow_rows(n, entry, exit_, side="long"):
    return [
        {
            "v": 1,
            "ts_signal": BASE + i * DAY,
            "strategy_id": "S7",
            "side": side,
            "qty": 1,
            "sim_entry": entry,
            "sim_exit": exit_,
        }
        for i in range(n)
    ]


def _backtest_rows(n, pnl_points):
    return [
        {"ts": BASE + i * DAY, "action": "exit", "qty": 1, "pnl_points": pnl_points}
        for i in range(n)
    ]


def test_forward_gap_ratios_and_tier_L(tmp_path):
    shadow = tmp_path / "shadow.jsonl"
    bt = tmp_path / "bt.jsonl"
    _write_jsonl(shadow, _shadow_rows(40, 1000.0, 1002.0))  # +2 pts/trade
    _write_jsonl(bt, _backtest_rows(40, 2.5))  # +2.5 pts/trade
    res = forward_gap(shadow, bt)
    assert res["expectancy_forward_pts"] == 2.0
    assert res["expectancy_backtest_pts"] == 2.5
    assert abs(res["expectancy_ratio"] - 0.8) < 1e-9
    assert res["count_forward"] == 40 and res["count_backtest"] == 40
    assert res["n_sessions_forward"] == 40
    assert res["tier"] == "TIER_L"
    assert res["sufficient"] is True


def test_forward_gap_short_side_and_vnd_backtest(tmp_path):
    shadow = tmp_path / "s.jsonl"
    bt = tmp_path / "b.jsonl"
    # short: entry 1002, exit 1000: +2 points
    _write_jsonl(shadow, _shadow_rows(5, 1002.0, 1000.0, side="short"))
    # backtest in VND: 200,000 VND / 100,000 VND per point = 2 points
    _write_jsonl(bt, [{"ts": BASE, "action": "exit", "qty": 1, "pnl_vnd": 200000.0}])
    res = forward_gap(shadow, bt, min_sessions=40)
    assert abs(res["expectancy_forward_pts"] - 2.0) < 1e-9
    assert abs(res["expectancy_backtest_pts"] - 2.0) < 1e-9
    assert res["tier"] == "PRELIMINARY"  # 5 sessions
    assert res["sufficient"] is False
    assert "insufficient sample: 5/40" in res["note"]
