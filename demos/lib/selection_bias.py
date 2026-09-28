"""Best-of-N selection bias, measured with the project's validation statistics.

The statistics come from ``historical/optimization/dsr.py`` (PSR, deflated Sharpe, minimum backtest
length) and ``historical/optimization/overfitting.py`` (probability of backtest overfitting); this
module generates skill-less (optionally one skilled) daily return series and arranges the calls.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import common  # noqa: F401  - puts the repo root on sys.path
from historical.optimization.dsr import (
    deflated_sharpe_from_trials,
    min_backtest_length,
    probabilistic_sharpe_ratio,
)
from historical.optimization.overfitting import pbo


@dataclass(frozen=True)
class Universe:
    returns: np.ndarray  # shape (n_strategies, n_sessions), per-session returns
    skilled_index: int | None


def make_universe(n_strategies: int, n_sessions: int, skilled_annual_sharpe: float | None,
                  seed: int) -> Universe:
    """``n_strategies`` zero-mean return streams; optionally one of them carries a real edge."""
    rng = np.random.default_rng(seed)
    r = rng.normal(0.0, 0.01, (n_strategies, n_sessions))
    skilled = None
    if skilled_annual_sharpe:
        skilled = 0
        r[skilled] += 0.01 * skilled_annual_sharpe / np.sqrt(252.0)
    return Universe(r, skilled)


def per_obs_sharpe(r: np.ndarray) -> np.ndarray:
    return r.mean(axis=-1) / r.std(axis=-1, ddof=1)


def best_of_n(u: Universe) -> dict:
    """PSR of the best strategy against zero vs its deflated Sharpe against the trial set."""
    sr = per_obs_sharpe(u.returns)
    best = int(np.argmax(sr))
    n_obs = u.returns.shape[1]
    dsr, n_trials, _ = deflated_sharpe_from_trials(sr, n_obs)
    return {"best_is_the_skilled_one": best == u.skilled_index, "best_annual_sharpe": sr[best] * np.sqrt(252),
            "psr_vs_zero": probabilistic_sharpe_ratio(sr[best], n_obs), "dsr_vs_trials": dsr,
            "n_trials": n_trials}


def cscv_pbo(u: Universe, n_blocks: int = 16, n_splits: int = 200, seed: int = 11) -> float:
    """Combinatorially symmetric cross-validation: half the time blocks in-sample, half out."""
    rng = np.random.default_rng(seed)
    blocks = np.array_split(np.arange(u.returns.shape[1]), n_blocks)
    is_rows, oos_rows = [], []
    for _ in range(n_splits):
        pick = rng.permutation(n_blocks)[: n_blocks // 2]
        is_idx = np.concatenate([blocks[i] for i in pick])
        oos_idx = np.concatenate([blocks[i] for i in range(n_blocks) if i not in set(pick)])
        is_rows.append(per_obs_sharpe(u.returns[:, is_idx]))
        oos_rows.append(per_obs_sharpe(u.returns[:, oos_idx]))
    value, _, _ = pbo(is_rows, oos_rows)
    return value


def required_sharpe_for_dsr(n_trials: int, n_obs: int, target: float = 0.95) -> float:
    """Smallest per-observation Sharpe whose DSR reaches ``target`` when the trials are skill-less.

    Under the null the trial Sharpes have variance ~1/(n_obs-1); DSR(sr) = target is solved by
    bisection on ``deflated_sharpe_ratio`` with that null variance and ``n_trials``.
    """
    from historical.optimization.dsr import deflated_sharpe_ratio

    var_sr = 1.0 / (n_obs - 1)
    lo, hi = 0.0, 2.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if deflated_sharpe_ratio(mid, n_obs, var_sr, n_trials) >= target:
            hi = mid
        else:
            lo = mid
    return hi


__all__ = ["make_universe", "best_of_n", "cscv_pbo", "required_sharpe_for_dsr", "min_backtest_length"]
