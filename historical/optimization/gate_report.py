"""Staged go/no-go gate for a backtest run directory, with JSON and text report writers.

Stages run cheapest first and stop at the first failure: 1 screening; 2 walk-forward
robustness; 2b net P&L across regimes; 3 overfitting (PSR vs zero, White's Reality Check,
Aronson suite, trials-deflated Sharpe, and PBO when config matrices are given); 4 net profit
factor and Sharpe after costs. Thresholds are the module constants. The verdict is PASS,
MARGINAL or FAIL; a skipped stage fails unless ``allow_skip=True``, and a MinBTL longer than
the run's history adds ``INSUFFICIENT_DATA``, which caps the verdict at MARGINAL.
:func:`forward_gap` compares forward (shadow) and backtest expectancy.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np

from historical.backtest_engine.jsonl_bridge import load_equity, load_trades
from historical.backtest_engine.jsonl_bridge import evaluate_run
from historical.optimization.cpcv import CPCV_INAPPLICABLE_STAMP, cpcv_report
from historical.optimization.dsr import min_backtest_length, sessions_to_years
from historical.optimization.overfitting import (
    BOOTSTRAP_CAVEAT,
    aronson_suite_detail,
    pbo,
    probabilistic_sharpe_vs_zero,
    whites_reality_check_detail,
)
from historical.optimization.regime_labels import regime_for_ts
from historical.optimization.trials_ledger import (
    FOOTING_PER_DAILY,
    Deflation,
    FootingMismatch,
    TrialRecord,
    deflated_sharpe_for_returns,
    default_ledger_path,
    load_trials,
    total_trials,
)

PathLike = Union[str, Path]

ICT_OFFSET_SECONDS = 7 * 3600

SINGLE_REGIME_CAVEAT = (
    "SINGLE-REGIME DATA: verdict computed on one market regime (~1mo tick / "
    "~8mo bar). Sharpe overstates 30-50%; expect live Sharpe = 40-60% of "
    "backtest. DSR value embedded below; treat with thin-data skepticism."
)

# Stage thresholds.
S1_OOS_SHARPE_MIN = 0.50
S1_MIN_TRADES = 10
S1_MAX_DD_PCT = 15.0
S1_RET_MIN_PCT = 3.0

WFA_MEAN_SHARPE_MIN = 0.40
WFA_MIN_FOLD_SHARPE = 0.20
WFA_HIGH_VOL_SHARPE_MIN = 0.15
WFA_PARAM_STABILITY_MIN = 0.70
WFA_FOLD_SHARPE_STD_MAX = 0.25

S4_NET_PF_MIN = 1.3
S4_NET_SHARPE_MIN = 0.40

PBO_MARGINAL_LOW = 0.05
PBO_MARGINAL_HIGH = 0.15

# Pass bar for the trials-deflated Sharpe (Bailey & López de Prado, 2014).
DSR_TRIALS_MIN = 0.95

# Annualised Sharpe that MinBTL is computed for.
MINBTL_TARGET_ANNUAL_SHARPE = 1.0

# Forward sessions for a paper-tier (P) and live-tier (L) read; fewer is preliminary.
FORWARD_TIER_P_SESSIONS = 20
FORWARD_TIER_L_SESSIONS = 40
# Contract multiplier: VND per index point per contract.
VN30F1M_POINT_VALUE_VND = 100_000.0

# Stage 2b: minimum number of regimes traded, and of regimes with positive net P&L.
REGIME_MIN_COVERED = 2
REGIME_MIN_POSITIVE = 2


@dataclass
class GateCheck:
    """One threshold check within a stage."""

    name: str
    value: float
    threshold: float
    passed: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "value": float(self.value),
            "threshold": float(self.threshold),
            "passed": bool(self.passed),
        }


@dataclass
class StageResult:
    """Result of one gate stage."""

    stage: str
    checks: List[GateCheck] = field(default_factory=list)
    passed: bool = True
    skipped: bool = False
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "passed": bool(self.passed),
            "skipped": bool(self.skipped),
            "note": self.note,
            "checks": [c.to_dict() for c in self.checks],
        }


@dataclass
class Verdict:
    """Overall go/no-go verdict.

    ``dsr`` is the trials-deflated Sharpe, NaN when unavailable (the reason is in
    ``dsr_detail``); ``psr_vs_zero`` is the undeflated P(SR > 0).
    """

    overall: str  # PASS | MARGINAL | FAIL
    stages: List[StageResult]
    dsr: float
    seed: int
    caveat: str = SINGLE_REGIME_CAVEAT
    flags: List[str] = field(default_factory=list)
    dsr_kind: str = "trials_deflated"
    dsr_detail: Dict[str, Any] = field(default_factory=dict)
    psr_vs_zero: float = float("nan")
    skipped_stages: List[str] = field(default_factory=list)
    min_btl_years: float = float("nan")
    history_years: float = float("nan")
    n_trials: int = 0
    bootstrap_caveat: str = BOOTSTRAP_CAVEAT

    def to_dict(self) -> Dict[str, Any]:
        return {
            "overall": self.overall,
            "dsr": float(self.dsr),
            "dsr_kind": self.dsr_kind,
            "dsr_detail": self.dsr_detail,
            "psr_vs_zero": float(self.psr_vs_zero),
            "seed": int(self.seed),
            "caveat": self.caveat,
            "bootstrap_caveat": self.bootstrap_caveat,
            "flags": list(self.flags),
            "skipped_stages": list(self.skipped_stages),
            "min_btl_years": float(self.min_btl_years),
            "history_years": float(self.history_years),
            "n_trials": int(self.n_trials),
            "stages": [s.to_dict() for s in self.stages],
        }


# --- run_dir readers --------------------------------------------------------


def _trade_returns(run_dir: PathLike, initial_cash: float) -> np.ndarray:
    """Per-trade net P&L as a fraction of ``initial_cash`` (the engine already deducts fees)."""
    tr = load_trades(Path(run_dir) / "trades.jsonl")
    if "pnl_vnd" in tr.columns:
        pnl = tr["pnl_vnd"].astype(float).to_numpy()
    elif "PnL_VND" in tr.columns:
        pnl = tr["PnL_VND"].astype(float).to_numpy()
    else:
        return np.asarray([], dtype=float)
    return pnl / float(initial_cash)


def _trade_sessions(run_dir: PathLike) -> Optional[np.ndarray]:
    """ICT day index of each closed trade, used as the block-bootstrap session groups.

    Returns None when the trade log has no usable timestamps.
    """
    tr = load_trades(Path(run_dir) / "trades.jsonl")
    if tr.empty or "ts" not in tr.columns:
        return None
    ts = tr["ts"].astype(float).to_numpy()
    if not np.isfinite(ts).all():
        return None
    return ((ts + ICT_OFFSET_SECONDS) // 86_400).astype(np.int64)


def _daily_returns(run_dir: PathLike) -> np.ndarray:
    """Daily returns from ``equity.jsonl`` by ICT date: the ledger's ``per_daily`` footing.

    Empty when the file is missing, which makes the trials deflation unavailable.
    """
    path = Path(run_dir) / "equity.jsonl"
    if not path.exists():
        return np.asarray([], dtype=float)
    equity = load_equity(path)
    if equity is None or len(equity) == 0:
        return np.asarray([], dtype=float)
    daily = equity.resample("D").last().dropna()
    return daily.pct_change().dropna().to_numpy(dtype=float)


def _history_sessions(run_dir: PathLike) -> int:
    """Distinct ICT trading days covered by the run (equity first, else trades)."""
    path = Path(run_dir) / "equity.jsonl"
    if path.exists():
        equity = load_equity(path)
        if equity is not None and len(equity):
            return int(len(np.unique(equity.index.normalize())))
    sess = _trade_sessions(run_dir)
    return int(np.unique(sess).size) if sess is not None and sess.size else 0


# --- Stage 2: robustness ----------------------------------------------------


def _fold_is_real_walk_forward(fold: Any) -> bool:
    """True when the fold carries ``train_metrics``, i.e. a training window was fitted."""
    train = getattr(fold, "train_metrics", None)
    return bool(train)


def wfa_gate(wfa_result: Sequence[Any]) -> StageResult:
    """Stage 2: robustness over walk-forward folds (objects with train and test metrics).

    A fold without ``train_metrics`` fails the stage. Scores the mean, minimum and spread of the
    test Sharpes; the high-vol Sharpe over folds tagged ``high_vol_fold``; and parameter
    stability only when folds differ in selected parameters or report ``n_candidates > 1``.
    No folds marks the stage skipped.
    """
    stage = StageResult(stage="Stage2_Robustness")
    folds = list(wfa_result) if wfa_result is not None else []
    if not folds:
        stage.skipped = True
        stage.note = "no WFA folds provided"
        return stage

    fake = [f for f in folds if not _fold_is_real_walk_forward(f)]
    if fake:
        stage.passed = False
        stage.note = (
            f"{len(fake)}/{len(folds)} fold(s) carry no train window — these are "
            "contiguous slices of a single full-tape run, not walk-forward folds. "
            "Produce folds with walk_forward.WalkForwardAnalysis.run."
        )
        return stage

    oos_sharpes = np.array(
        [float(f.test_metrics.get("sharpe_ratio", 0.0)) for f in folds], dtype=float
    )
    mean_sharpe = float(np.mean(oos_sharpes))
    min_sharpe = float(np.min(oos_sharpes))
    std_sharpe = float(np.std(oos_sharpes))

    # high-vol Sharpe floor, over tagged folds only
    hv = [
        float(f.test_metrics.get("sharpe_ratio", 0.0))
        for f in folds
        if getattr(f, "high_vol_fold", None)
    ]
    high_vol_sharpe = float(min(hv)) if hv else float("nan")

    stage.checks = [
        GateCheck(
            "wfa_mean_sharpe", mean_sharpe, WFA_MEAN_SHARPE_MIN, mean_sharpe > WFA_MEAN_SHARPE_MIN
        ),
        GateCheck(
            "wfa_min_fold_sharpe", min_sharpe, WFA_MIN_FOLD_SHARPE, min_sharpe > WFA_MIN_FOLD_SHARPE
        ),
        GateCheck(
            "wfa_fold_sharpe_std",
            std_sharpe,
            WFA_FOLD_SHARPE_STD_MAX,
            std_sharpe < WFA_FOLD_SHARPE_STD_MAX,
        ),
    ]

    # Parameter stability: share of folds using the modal parameter set. With one fixed set it
    # is 1.0 by construction, so it is scored only when a per-fold search is evident.
    param_keys = [
        json.dumps(getattr(f, "selected_params", getattr(f, "best_params", {})), sort_keys=True)
        for f in folds
    ]
    notes: List[str] = []
    if len(set(param_keys)) > 1 or any(
        int(getattr(f, "n_candidates", 0) or 0) > 1 for f in folds
    ):
        modal = max(set(param_keys), key=param_keys.count)
        stability = float(param_keys.count(modal)) / float(len(param_keys))
        stage.checks.append(
            GateCheck(
                "wfa_param_stability",
                stability,
                WFA_PARAM_STABILITY_MIN,
                stability > WFA_PARAM_STABILITY_MIN,
            )
        )
    else:
        notes.append(
            "param-stability NOT scored: every fold selected the same param set and no "
            "per-fold search was evidenced — stability would be 1.0 by construction"
        )

    if hv:
        stage.checks.append(
            GateCheck(
                "wfa_high_vol_sharpe",
                high_vol_sharpe,
                WFA_HIGH_VOL_SHARPE_MIN,
                high_vol_sharpe > WFA_HIGH_VOL_SHARPE_MIN,
            )
        )
    else:
        notes.append("no high_vol_fold tagged — high-vol Sharpe check skipped")

    stage.note = "; ".join(notes)
    stage.passed = all(c.passed for c in stage.checks)
    return stage


def regime_segmentation_gate(
    run_dir: PathLike,
    regime_labels: Dict[str, Dict[str, Any]],
    min_covered: int = REGIME_MIN_COVERED,
    min_positive: int = REGIME_MIN_POSITIVE,
) -> StageResult:
    """Stage 2b: net P&L of closed trades by session regime.

    Passes when at least ``min_covered`` regimes were traded and at least ``min_positive`` of
    them are net positive. Trades in unlabelled sessions are ignored; the stage is skipped
    without labels or timestamped trades.
    """
    stage = StageResult(stage="Stage2b_Regime")
    if not regime_labels:
        stage.skipped = True
        stage.note = "no regime_labels supplied — regime segmentation skipped"
        return stage

    trades = load_trades(Path(run_dir) / "trades.jsonl")
    if trades.empty or "ts" not in trades.columns:
        stage.skipped = True
        stage.note = "no timestamped trades — regime segmentation skipped"
        return stage

    net_by_regime: Dict[str, float] = {}
    unlabelled = 0
    for ts, pnl in zip(trades["ts"].to_numpy(), trades["PnL_VND"].astype(float).to_numpy()):
        reg = regime_for_ts(float(ts), regime_labels)
        if reg is None:
            unlabelled += 1
            continue
        net_by_regime[reg] = net_by_regime.get(reg, 0.0) + float(pnl)

    covered = len(net_by_regime)
    positive = sum(1 for v in net_by_regime.values() if v > 0.0)
    stage.checks = [
        GateCheck("regimes_covered", covered, min_covered, covered >= min_covered),
        GateCheck("regimes_net_positive", positive, min_positive, positive >= min_positive),
    ]
    if unlabelled:
        stage.note = f"{unlabelled} trade(s) fell in unlabelled sessions (ignored)"
    stage.passed = all(c.passed for c in stage.checks)
    return stage


def _stage1_screening(metrics: Dict[str, Any]) -> StageResult:
    """Stage 1: Sharpe, trade count, maximum drawdown and total return of the run."""
    stage = StageResult(stage="Stage1_Screening")
    sharpe = float(metrics.get("sharpe_ratio", 0.0))
    n = int(metrics.get("num_trades", 0))
    max_dd = abs(float(metrics.get("max_drawdown_pct", 0.0)))
    ret = float(metrics.get("total_return_pct", 0.0))
    stage.checks = [
        GateCheck("oos_sharpe", sharpe, S1_OOS_SHARPE_MIN, sharpe > S1_OOS_SHARPE_MIN),
        GateCheck("trade_count", n, S1_MIN_TRADES, n >= S1_MIN_TRADES),
        GateCheck("max_drawdown_pct", max_dd, S1_MAX_DD_PCT, max_dd < S1_MAX_DD_PCT),
        GateCheck("total_return_pct", ret, S1_RET_MIN_PCT, ret > S1_RET_MIN_PCT),
    ]
    stage.passed = all(c.passed for c in stage.checks)
    return stage


# --- Stage 3: overfitting ---------------------------------------------------


def compute_deflation(
    daily_returns: Sequence[float],
    trials_records: Optional[Sequence[TrialRecord]],
) -> Tuple[Optional[Deflation], str]:
    """Trials-deflated Sharpe of daily returns, as ``(Deflation, "")`` or ``(None, reason)``.

    ``daily_returns`` is on the ``per_daily`` footing; a footing mismatch with the ledger
    becomes a refusal reason, never a number.
    """
    if not trials_records:
        return None, "no trials ledger supplied — N is unknown, nothing to deflate against"
    try:
        d = deflated_sharpe_for_returns(daily_returns, trials_records, FOOTING_PER_DAILY)
    except FootingMismatch as exc:
        return None, f"FOOTING MISMATCH: {exc}"
    if not d.available:
        return None, d.reason
    return d, ""


def _stage3_overfitting(
    returns: np.ndarray,
    seed: int,
    cpcv: bool = False,
    session_ids: Optional[Sequence] = None,
    deflation: Optional[Deflation] = None,
    deflation_refusal: str = "",
    require_trials_deflation: bool = True,
) -> "tuple[StageResult, float, list[str]]":
    """Stage 3 overfitting checks; returns ``(stage, psr_vs_zero, flags)``.

    ``returns`` are per-trade returns for the PSR, White's Reality Check and Aronson suite.
    The deflated Sharpe is computed on daily returns by :func:`compute_deflation` and passed
    in as ``deflation``.
    """
    stage = StageResult(stage="Stage3_Overfitting")
    flags: List[str] = []
    notes: List[str] = []
    n = int(returns.size)

    psr_v, psr_pass, psr_thr = probabilistic_sharpe_vs_zero(returns)
    (wrc_v, wrc_pass, wrc_thr), wrc_detail = whites_reality_check_detail(
        returns, seed=seed, session_ids=session_ids
    )
    (aro_v, aro_pass, aro_thr), aro_detail = aronson_suite_detail(
        returns, seed=seed, session_ids=session_ids
    )

    # PBO needs config matrices; gate_report adds it when pbo_matrices is given.
    stage.checks = [
        # P(SR > 0), not deflated for the number of trials
        GateCheck("psr_vs_zero", psr_v, psr_thr, psr_pass),
        GateCheck("whites_reality_check", wrc_v, wrc_thr, wrc_pass),
        GateCheck("aronson_suite", aro_v, aro_thr, aro_pass),
    ]
    notes.append(
        "block-bootstrap avg_block_len="
        f"{wrc_detail.get('avg_block_len', float('nan')):.2f} "
        f"(~{wrc_detail.get('n_blocks_effective', float('nan')):.1f} blocks)"
    )
    sub = aro_detail.get("tests", {})
    if sub:
        notes.append(
            "aronson "
            f"{aro_detail.get('n_passed', 0)}/{aro_detail.get('n_tests', 0)}: "
            + ", ".join(f"{k}={'ok' if v else 'XX'}" for k, v in sub.items())
        )

    # CPCV is reported as inapplicable: a flag, never a check, and never part of the trial count.
    if cpcv:
        cr = cpcv_report()
        flags.append("CPCV_INAPPLICABLE")
        notes.append(CPCV_INAPPLICABLE_STAMP)
        assert cr.n_paths == 0 and not cr.applicable

    # trials-deflated Sharpe; when unavailable it fails unless require_trials_deflation=False
    if deflation is not None:
        stage.checks.append(
            GateCheck(
                "deflated_sharpe_trials",
                deflation.dsr,
                DSR_TRIALS_MIN,
                deflation.dsr > DSR_TRIALS_MIN,
            )
        )
        notes.append(
            f"trials-DSR over n_trials={deflation.n_trials} "
            f"(var_sr={deflation.var_sr:.6g}, SR0={deflation.sr0:.4f}, "
            f"footing={deflation.sharpe_footing})"
        )
    else:
        flags.append("TRIALS_DEFLATION_UNAVAILABLE")
        notes.append(f"NO DEFLATED SHARPE: {deflation_refusal}")
        if require_trials_deflation:
            stage.checks.append(
                GateCheck("deflated_sharpe_trials", float("nan"), DSR_TRIALS_MIN, False)
            )

    if n < 20:
        flags.append("LOW_N_DSR_DOMINANT")  # PBO and WRC are unreliable below 20 trades
    stage.note = "; ".join(notes)
    stage.passed = all(c.passed for c in stage.checks)
    return stage, psr_v, flags


def _stage3_pbo(stage: StageResult, pbo_matrices, flags: List[str]) -> None:
    """Add the PBO check to Stage 3 when config matrices are supplied."""
    is_m, oos_m = pbo_matrices
    pbo_v, pbo_pass, pbo_thr = pbo(is_m, oos_m)
    stage.checks.append(GateCheck("pbo", pbo_v, pbo_thr, pbo_pass))
    if PBO_MARGINAL_LOW <= pbo_v <= PBO_MARGINAL_HIGH:
        flags.append("PBO_SIZE_DOWN")  # PBO in the 5-15 % size-down band
    stage.passed = all(c.passed for c in stage.checks)


def _stage4_cost(metrics: Dict[str, Any]) -> StageResult:
    """Stage 4: net profit factor and net Sharpe."""
    stage = StageResult(stage="Stage4_CostAware")
    net_pf = float(metrics.get("net_profit_factor", 0.0))
    net_sharpe = float(metrics.get("sharpe_ratio", 0.0))  # net: the Rust trade log deducts fees
    stage.checks = [
        GateCheck("net_profit_factor", net_pf, S4_NET_PF_MIN, net_pf > S4_NET_PF_MIN),
        GateCheck("net_sharpe", net_sharpe, S4_NET_SHARPE_MIN, net_sharpe > S4_NET_SHARPE_MIN),
    ]
    stage.passed = all(c.passed for c in stage.checks)
    return stage


_LEDGER_DEFAULT = object()


def gate_report(
    run_dir: PathLike,
    wfa_result: Optional[Sequence[Any]] = None,
    baseline: Optional[Dict[str, Any]] = None,
    seed: int = 12345,
    initial_cash: float = 100_000_000.0,
    pbo_matrices: Optional[tuple] = None,
    regime_labels: Optional[Dict[str, Dict[str, Any]]] = None,
    cpcv: bool = False,
    trials_ledger: Any = _LEDGER_DEFAULT,
    allow_skip: bool = False,
    require_trials_deflation: bool = True,
) -> Verdict:
    """Run the staged gate on ``run_dir`` and return a :class:`Verdict`.

    Stages run cheapest first and stop at the first failure. A skipped stage fails unless
    ``allow_skip=True``, and a pass with skipped stages becomes MARGINAL. ``trials_ledger``
    defaults to the canonical ``trials.jsonl`` and also takes a path, records or ``None``;
    without a deflated Sharpe Stage 3 fails unless ``require_trials_deflation=False``.
    """
    metrics = evaluate_run(run_dir, initial_cash=initial_cash)
    returns = _trade_returns(run_dir, initial_cash)
    sessions = _trade_sessions(run_dir)
    if sessions is not None and sessions.size != returns.size:
        sessions = None

    # --- trials ledger ------------------------------------------------------
    if trials_ledger is _LEDGER_DEFAULT:
        trials_records: Optional[Sequence[TrialRecord]] = load_trials(default_ledger_path())
    elif trials_ledger is None:
        trials_records = None
    elif isinstance(trials_ledger, (str, Path)):
        trials_records = load_trials(trials_ledger)
    else:
        trials_records = list(trials_ledger)

    daily = _daily_returns(run_dir)
    deflation, refusal = compute_deflation(daily, trials_records)

    n_trials = int(total_trials(trials_records)) if trials_records else 0
    history_years = sessions_to_years(_history_sessions(run_dir))
    min_btl = min_backtest_length(n_trials, MINBTL_TARGET_ANNUAL_SHARPE) if n_trials >= 2 else float("nan")

    ctx: Dict[str, Any] = {
        "psr_vs_zero": float("nan"),
        "deflation": deflation,
        "refusal": refusal,
        "n_trials": n_trials,
        "history_years": history_years,
        "min_btl": min_btl,
    }

    stages: List[StageResult] = []
    flags: List[str] = []

    # history shorter than MinBTL caps the verdict at MARGINAL
    if np.isfinite(min_btl) and history_years < min_btl:
        flags.append("INSUFFICIENT_DATA")

    # Stage 1 — Screening.
    s1 = _stage1_screening(metrics)
    stages.append(s1)
    if not s1.passed:
        return _finalize(stages, seed, flags, "FAIL", ctx, allow_skip)

    # Stage 2 — Robustness. Skip == FAIL unless allow_skip.
    s2 = wfa_gate(wfa_result)
    stages.append(s2)
    if (s2.skipped and not allow_skip) or (not s2.skipped and not s2.passed):
        return _finalize(stages, seed, flags, "FAIL", ctx, allow_skip)

    # Stage 2b — Regime segmentation. Skip == FAIL unless allow_skip.
    s2b = regime_segmentation_gate(run_dir, regime_labels or {})
    stages.append(s2b)
    if (s2b.skipped and not allow_skip) or (not s2b.skipped and not s2b.passed):
        return _finalize(stages, seed, flags, "FAIL", ctx, allow_skip)

    # Stage 3 — Overfitting.
    s3, psr_value, s3_flags = _stage3_overfitting(
        returns,
        seed,
        cpcv=cpcv,
        session_ids=sessions,
        deflation=deflation,
        deflation_refusal=refusal,
        require_trials_deflation=require_trials_deflation,
    )
    ctx["psr_vs_zero"] = psr_value
    flags.extend(s3_flags)
    if pbo_matrices is not None:
        _stage3_pbo(s3, pbo_matrices, flags)
    stages.append(s3)
    if not s3.passed:
        return _finalize(stages, seed, flags, "FAIL", ctx, allow_skip)

    # Stage 4 — Cost-aware.
    s4 = _stage4_cost(metrics)
    stages.append(s4)
    if not s4.passed:
        return _finalize(stages, seed, flags, "FAIL", ctx, allow_skip)

    # All stages passed; MARGINAL if a size-down or thin-data flag is set.
    overall = "MARGINAL" if ("PBO_SIZE_DOWN" in flags or "INSUFFICIENT_DATA" in flags) else "PASS"
    return _finalize(stages, seed, flags, overall, ctx, allow_skip)


def _finalize(
    stages: List[StageResult],
    seed: int,
    flags: List[str],
    overall: str,
    ctx: Dict[str, Any],
    allow_skip: bool,
) -> Verdict:
    skipped = [s.stage for s in stages if s.skipped]
    if skipped:
        flags.append("STAGES_SKIPPED:" + ",".join(skipped))
        if allow_skip and overall == "PASS":
            # a pass with skipped stages is provisional
            overall = "MARGINAL"
    deflation: Optional[Deflation] = ctx.get("deflation")
    detail = deflation.as_dict() if deflation is not None else {
        "available": False,
        "reason": ctx.get("refusal", ""),
    }
    return Verdict(
        overall=overall,
        stages=stages,
        dsr=float(deflation.dsr) if deflation is not None else float("nan"),
        seed=int(seed),
        flags=flags,
        dsr_kind="trials_deflated" if deflation is not None else "unavailable",
        dsr_detail=detail,
        psr_vs_zero=float(ctx.get("psr_vs_zero", float("nan"))),
        skipped_stages=skipped,
        min_btl_years=float(ctx.get("min_btl", float("nan"))),
        history_years=float(ctx.get("history_years", float("nan"))),
        n_trials=int(ctx.get("n_trials", 0)),
    )


# --- Renderers --------------------------------------------------------------


def write_verdict_json(verdict: Verdict, out_path: PathLike) -> Path:
    """Write the verdict as compact JSON with sorted keys and return the path."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(verdict.to_dict(), fh, sort_keys=True, separators=(",", ":"))
    return out_path


def render_gate_report_txt(verdict: Verdict) -> str:
    """Render the verdict as a plain-text report: header, stages with their checks, caveats."""
    dsr_label = (
        f"DSR (trials-deflated, N={verdict.n_trials}): {verdict.dsr:.4f}"
        if verdict.dsr_kind == "trials_deflated"
        else f"DSR: UNAVAILABLE — {verdict.dsr_detail.get('reason', '')}"
    )
    lines = [
        "=" * 68,
        f"GATE REPORT — OVERALL: {verdict.overall}",
        "=" * 68,
        dsr_label,
        f"PSR vs zero (descriptive, NOT deflated): {verdict.psr_vs_zero:.4f}",
        f"seed: {verdict.seed}",
        f"MinBTL: {verdict.min_btl_years:.2f} yr required vs "
        f"{verdict.history_years:.2f} yr available",
    ]
    if verdict.skipped_stages:
        lines.append("SKIPPED STAGES (untested, not passed): " + ", ".join(verdict.skipped_stages))
    if verdict.flags:
        lines.append(f"flags: {', '.join(verdict.flags)}")
    lines.append("")
    for st in verdict.stages:
        status = "SKIP" if st.skipped else ("PASS" if st.passed else "FAIL")
        lines.append(f"[{status}] {st.stage}" + (f"  ({st.note})" if st.note else ""))
        for c in st.checks:
            mark = "OK " if c.passed else "XX "
            lines.append(f"    {mark} {c.name:<24} value={c.value:>10.4f}  thr={c.threshold:>8.4f}")
        lines.append("")
    lines.append("CAVEAT: " + verdict.caveat)
    lines.append("BOOTSTRAP: " + verdict.bootstrap_caveat)
    lines.append("=" * 68)
    return "\n".join(lines)


def write_gate_report_txt(verdict: Verdict, out_path: PathLike) -> Path:
    """Write :func:`render_gate_report_txt` output to ``out_path`` and return the path."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(render_gate_report_txt(verdict))
    return out_path


# --- Forward gap: forward shadow log vs backtest trade log, in index points --


def _pnl_pts_from_shadow(rec: Dict[str, Any]) -> Optional[float]:
    """P&L in points of a shadow row: ``(sim_exit - sim_entry) * side * qty``."""
    entry, exit_ = rec.get("sim_entry"), rec.get("sim_exit")
    if entry is None or exit_ is None:
        return None
    side = str(rec.get("side", "")).lower()
    sign = 1.0 if side in ("long", "buy", "b", "1") else -1.0
    qty = float(rec.get("qty", 1) or 1)
    return (float(exit_) - float(entry)) * sign * qty


def _pnl_pts_from_row(rec: Dict[str, Any], point_value_vnd: float) -> Optional[float]:
    """Per-trade P&L in points from a shadow or backtest JSONL row; None for other rows.

    Backtest rows use ``pnl_points``/``pnl_pts``, else an exit row's VND P&L divided by
    ``point_value_vnd * qty``.
    """
    if "sim_entry" in rec and "sim_exit" in rec:
        return _pnl_pts_from_shadow(rec)
    for key in ("pnl_points", "pnl_pts"):
        if rec.get(key) is not None:
            return float(rec[key])
    if rec.get("action") == "exit":
        vnd = rec.get("pnl_vnd", rec.get("PnL_VND"))
        if vnd is not None:
            qty = float(rec.get("qty", 1) or 1)
            return float(vnd) / (point_value_vnd * max(qty, 1.0))
    return None


def _load_pts_series(path: PathLike, point_value_vnd: float) -> Tuple[np.ndarray, np.ndarray]:
    """(pts, ts) arrays of per-trade points and their epoch-second timestamps."""
    pts: List[float] = []
    tss: List[float] = []
    p = Path(path)
    if not p.exists():
        return np.asarray(pts, dtype=float), np.asarray(tss, dtype=float)
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            rec = json.loads(line)
            val = _pnl_pts_from_row(rec, point_value_vnd)
            if val is None:
                continue
            pts.append(val)
            ts = rec.get("ts", rec.get("ts_signal"))
            tss.append(float(ts) if ts is not None else float("nan"))
    return np.asarray(pts, dtype=float), np.asarray(tss, dtype=float)


def _sessions(ts: np.ndarray) -> int:
    finite = ts[np.isfinite(ts)]
    return int(np.unique((finite // 86_400).astype(np.int64)).size) if finite.size else 0


def forward_gap(
    shadow_path: PathLike,
    backtest_path: PathLike,
    min_sessions: int = FORWARD_TIER_L_SESSIONS,
    point_value_vnd: float = VN30F1M_POINT_VALUE_VND,
    regime_labels: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Compare forward (shadow-log) and backtest per-trade expectancy, in index points.

    Returns a JSON-serialisable dict: both expectancies and their ratio (the capture), trade
    counts and their ratio, the forward session count and tier (``TIER_L``, ``TIER_P`` or
    ``PRELIMINARY``), whether ``min_sessions`` is met, and, with ``regime_labels``, the
    forward expectancy per regime.
    """
    fwd, fwd_ts = _load_pts_series(shadow_path, point_value_vnd)
    bt, _bt_ts = _load_pts_series(backtest_path, point_value_vnd)
    exp_f = float(fwd.mean()) if fwd.size else 0.0
    exp_b = float(bt.mean()) if bt.size else 0.0
    n_sess = _sessions(fwd_ts)
    tier = (
        "TIER_L"
        if n_sess >= FORWARD_TIER_L_SESSIONS
        else ("TIER_P" if n_sess >= FORWARD_TIER_P_SESSIONS else "PRELIMINARY")
    )
    out: Dict[str, Any] = {
        "expectancy_forward_pts": exp_f,
        "expectancy_backtest_pts": exp_b,
        "expectancy_ratio": (exp_f / exp_b) if exp_b != 0.0 else float("nan"),
        "count_forward": int(fwd.size),
        "count_backtest": int(bt.size),
        "count_ratio": (fwd.size / bt.size) if bt.size else float("nan"),
        "n_sessions_forward": n_sess,
        "tier": tier,
        "sufficient": n_sess >= min_sessions,
        "note": "" if n_sess >= min_sessions else f"insufficient sample: {n_sess}/{min_sessions}",
    }
    if regime_labels and fwd.size:
        per: Dict[str, Dict[str, float]] = {}
        for val, ts in zip(fwd.tolist(), fwd_ts.tolist()):
            if not np.isfinite(ts):
                continue
            reg = regime_for_ts(float(ts), regime_labels)
            if reg is None:
                continue
            bucket = per.setdefault(reg, {"sum_pts": 0.0, "n": 0.0})
            bucket["sum_pts"] += val
            bucket["n"] += 1.0
        out["per_regime"] = {
            reg: {"mean_pts": b["sum_pts"] / b["n"], "n": int(b["n"])}
            for reg, b in per.items()
            if b["n"] > 0
        }
    return out


def _main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Gate-battery utilities.")
    ap.add_argument(
        "--forward-gap",
        nargs=2,
        metavar=("SHADOW_JSONL", "BACKTEST_JSONL"),
        help="Forward shadow log vs backtest trade log -> expectancy-gap JSON.",
    )
    ap.add_argument("--min-sessions", type=int, default=FORWARD_TIER_L_SESSIONS)
    ap.add_argument("--point-value", type=float, default=VN30F1M_POINT_VALUE_VND)
    args = ap.parse_args(argv)
    if args.forward_gap:
        res = forward_gap(
            args.forward_gap[0],
            args.forward_gap[1],
            min_sessions=args.min_sessions,
            point_value_vnd=args.point_value,
        )
        print(json.dumps(res, indent=2, sort_keys=True))
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
