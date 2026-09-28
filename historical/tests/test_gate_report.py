"""Tests for the gate battery: an edge passes, noise fails, and DSR/PBO golden values."""

from __future__ import annotations

import json

import numpy as np

from historical.optimization.gate_report import gate_report
from historical.optimization.overfitting import deflated_sharpe, pbo

BASE = 10_800
DAY = 86_400


def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _make_run(tmp_path, name, pnls):
    """Build a run directory with one trade per ICT day, each exit carrying its ``pnl_vnd``.

    One trade per day gives one daily return per trade, so the daily Sharpe tracks the edge.
    """
    d = tmp_path / name
    d.mkdir()
    trades, equity = [], []
    eq = 100_000_000.0
    # day-0 equity point, so the first trade produces a daily return
    equity.append({"ts": BASE, "equity": eq, "position": 0, "unrealized": 0.0, "realized": 0.0})
    for i, pnl in enumerate(pnls):
        ts = BASE + (i + 1) * DAY  # one trade per ICT day
        eq += float(pnl)
        trades.append(
            {
                "ts": ts,
                "side": "long",
                "action": "enter",
                "qty": 1,
                "price": 1250.0,
                "fee_vnd": 0.0,
                "reason": "e",
            }
        )
        trades.append(
            {
                "ts": ts + 60,
                "side": "long",
                "action": "exit",
                "qty": 1,
                "price": 1252.0,
                "fee_vnd": 0.0,
                "pnl_points": float(pnl) / 100000.0,
                "pnl_vnd": float(pnl),
                "reason": "x",
            }
        )
        equity.append(
            {
                "ts": ts + 60,
                "equity": eq,
                "position": 0,
                "unrealized": 0.0,
                "realized": eq - 100_000_000.0,
            }
        )
    _write_jsonl(d / "trades.jsonl", trades)
    _write_jsonl(d / "equity.jsonl", equity)
    return d


# --- a positive edge passes ---


def test_positive_edge_run_passes(tmp_path):
    # strong, consistent per-trade wins
    rng = np.random.default_rng(3)
    pnls = rng.normal(120_000, 25_000, 40)  # mean >> std -> high Sharpe
    run = _make_run(tmp_path, "edge", pnls)
    # allow_skip=True is required to evaluate stages 1, 3 and 4 without a walk-forward result
    v = gate_report(run, wfa_result=None, seed=11, allow_skip=True)
    assert v.overall in ("PASS", "MARGINAL"), v.to_dict()
    stage_names = [s.stage for s in v.stages]
    assert "Stage1_Screening" in stage_names
    assert "Stage3_Overfitting" in stage_names
    assert "Stage4_CostAware" in stage_names
    assert v.dsr > 0.0
    assert "SINGLE-REGIME" in v.caveat


def test_missing_wfa_fails_by_default(tmp_path):
    """The same strong edge fails when Stage 2 is skipped without an explicit opt-out."""
    rng = np.random.default_rng(3)
    pnls = rng.normal(120_000, 25_000, 40)
    run = _make_run(tmp_path, "edge_noskip", pnls)
    v = gate_report(run, wfa_result=None, seed=11)
    assert v.overall == "FAIL", v.to_dict()
    s2 = next((s for s in v.stages if s.stage == "Stage2_Robustness"), None)
    assert s2 is not None and s2.skipped is True


# --- noise fails ---


def test_pure_noise_run_fails(tmp_path):
    # zero-mean noise: no edge
    rng = np.random.default_rng(123)
    pnls = rng.normal(0.0, 100_000, 40)
    run = _make_run(tmp_path, "noise", pnls)
    v = gate_report(run, wfa_result=None, seed=11)
    assert v.overall == "FAIL", v.to_dict()


def test_noisy_edge_rejected_by_overfitting_battery(tmp_path):
    # weak drift, large variance: passes the Stage 1 screen but fails the Stage 3 overfitting checks
    rng = np.random.default_rng(6)
    pnls = rng.normal(40_000, 800_000, 40)
    run = _make_run(tmp_path, "noisy_edge", pnls)
    # allow_skip lets the run reach Stage 3, so the rejection comes from that stage
    v = gate_report(run, wfa_result=None, seed=11, allow_skip=True)
    assert v.overall == "FAIL", v.to_dict()
    stage1 = next(s for s in v.stages if s.stage == "Stage1_Screening")
    stage3 = next((s for s in v.stages if s.stage == "Stage3_Overfitting"), None)
    assert stage1.passed is True  # passed the cheap screen
    assert stage3 is not None and stage3.passed is False  # rejected by overfitting tests


# --- golden values ---


def test_deflated_sharpe_golden():
    # seeded returns with a clear positive edge, N = 60
    rng = np.random.default_rng(42)
    r = rng.normal(1.0, 1.0, 60)
    dsr, passed, thr = deflated_sharpe(r)
    # DSR near 1.0; the threshold is 0.40 for N > 50
    assert thr == 0.40
    assert dsr > 0.99
    assert passed is True

    # small N: the threshold is +inf, so it cannot pass
    small = rng.normal(1.0, 1.0, 8)
    dsr_s, passed_s, thr_s = deflated_sharpe(small)
    assert thr_s == float("inf")
    assert passed_s is False


def test_pbo_golden():
    # 4 folds x 3 configs; the IS-best config is also OOS-best in every fold: PBO = 0
    is_m = [
        [0.1, 0.5, 0.9],
        [0.2, 0.4, 0.8],
        [0.0, 0.3, 0.7],
        [0.1, 0.2, 0.6],
    ]
    oos_m = [
        [0.1, 0.5, 0.9],
        [0.2, 0.4, 0.8],
        [0.0, 0.3, 0.7],
        [0.1, 0.2, 0.6],
    ]
    val, passed, thr = pbo(is_m, oos_m)
    assert val == 0.0
    assert passed is True
    assert thr == 0.05

    # the IS-best config is OOS-worst in every fold: PBO = 1
    oos_flip = [row[::-1] for row in is_m]
    val2, passed2, _ = pbo(is_m, oos_flip)
    assert val2 == 1.0
    assert passed2 is False
