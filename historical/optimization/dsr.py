"""Deflated Sharpe Ratio and Minimum Backtest Length.

The DSR is the Probabilistic Sharpe Ratio measured against ``SR0``, the expected maximum Sharpe
of ``N`` skill-less trials (Bailey & López de Prado, 2014). MinBTL is the track record needed
before a Sharpe can be told apart from that maximum (Bailey, Borwein, López de Prado & Zhu,
2014). Sharpe ratios are per observation (not annualised) unless a name says otherwise.
"""

from __future__ import annotations

import math
from typing import Sequence, Tuple

import numpy as np
from scipy import stats

EULER_MASCHERONI = 0.577_215_664_901_532_9


def probabilistic_sharpe_ratio(
    sr: float,
    n_obs: int,
    skew: float = 0.0,
    kurt: float = 3.0,
    sr_benchmark: float = 0.0,
) -> float:
    """Probability that the true per-observation Sharpe exceeds ``sr_benchmark`` (PSR).

    ``PSR = Φ((SR - SR*)·√(n-1) / √(1 - skew·SR + (kurt-1)/4·SR²))`` (Bailey & López de
    Prado, 2012), assuming i.i.d. returns; ``kurt`` is non-excess (normal = 3). Returns 0.0
    when ``n_obs < 2`` or the variance term is not positive.
    """
    if n_obs < 2:
        return 0.0
    denom = 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * (sr**2)
    if denom <= 0 or not np.isfinite(denom):
        return 0.0
    z = (sr - sr_benchmark) * math.sqrt(max(n_obs - 1, 1)) / math.sqrt(denom)
    return float(stats.norm.cdf(z))


def expected_max_sharpe(var_sr_across_trials: float, n_trials: int) -> float:
    """Expected maximum per-observation Sharpe of ``n_trials`` independent skill-less trials.

        SR0 = sqrt(Var[SR_trials]) * ((1-γ)·Φ⁻¹(1-1/N) + γ·Φ⁻¹(1-1/(N·e))),  γ = Euler–Mascheroni

    ``var_sr_across_trials`` is the variance of the trial Sharpes. Returns 0.0 (no deflation)
    when ``n_trials < 2`` or the variance is not positive.
    """
    if n_trials < 2 or var_sr_across_trials <= 0:
        return 0.0
    n = float(n_trials)
    g = EULER_MASCHERONI
    z = (1.0 - g) * stats.norm.ppf(1.0 - 1.0 / n) + g * stats.norm.ppf(1.0 - 1.0 / (n * math.e))
    return float(math.sqrt(var_sr_across_trials) * z)


def deflated_sharpe_ratio(
    sr: float,
    n_obs: int,
    var_sr_across_trials: float,
    n_trials: int,
    skew: float = 0.0,
    kurt: float = 3.0,
) -> float:
    """Deflated Sharpe Ratio: the PSR of ``sr`` against the trials benchmark ``SR0``, in [0, 1].

    A value near 1 means the observed Sharpe exceeds what best-of-N selection explains. With
    ``n_trials < 2`` the benchmark is 0 and the result is the PSR against zero.
    """
    sr0 = expected_max_sharpe(var_sr_across_trials, n_trials)
    return probabilistic_sharpe_ratio(sr, n_obs, skew, kurt, sr_benchmark=sr0)


def deflated_sharpe_from_trials(
    sr_trials: Sequence[float],
    n_obs: int,
    skew: float = 0.0,
    kurt: float = 3.0,
) -> Tuple[float, int, float]:
    """DSR of the best of ``sr_trials``, deflated by the raw trial count.

    Returns ``(dsr, n_trials, var_sr)``. Raises ``ValueError`` with fewer than two finite
    trial Sharpes, since there is then no dispersion to deflate against.
    """
    arr = np.asarray(list(sr_trials), dtype=float)
    arr = arr[np.isfinite(arr)]
    n_trials = int(arr.size)
    if n_trials < 2:
        raise ValueError(
            f"deflated_sharpe needs >=2 trial Sharpes to deflate (got {n_trials}); "
            "feed the full trial ledger, do not default to N=1"
        )
    var_sr = float(arr.var(ddof=1))
    best = float(arr.max())
    dsr = deflated_sharpe_ratio(best, n_obs, var_sr, n_trials, skew, kurt)
    return dsr, n_trials, var_sr


def effective_trials(aligned_returns: np.ndarray) -> float:
    """Effective number of independent trials for correlated trials, such as a parameter grid.

    Equicorrelation approximation ``N_eff = N / (1 + (N-1)·rho_bar)``, with ``rho_bar`` the
    mean pairwise Pearson correlation floored at 0, and ``N_eff`` clamped to ``[1, N]``.
    ``aligned_returns`` has shape ``(n_trials, n_obs)`` on a common index, 0 where a trial
    held no position.
    """
    a = np.asarray(aligned_returns, dtype=float)
    n = a.shape[0]
    if n < 2:
        return float(n)
    corr = np.corrcoef(a)
    iu = np.triu_indices(n, k=1)
    rho = corr[iu]
    rho = rho[np.isfinite(rho)]
    rho_bar = max(float(np.mean(rho)) if rho.size else 0.0, 0.0)
    n_eff = n / (1.0 + (n - 1) * rho_bar)
    return float(min(max(n_eff, 1.0), float(n)))


def deflated_sharpe_from_trials_eff(
    sr_trials: Sequence[float],
    aligned_returns: np.ndarray,
    n_obs: int,
    skew: float = 0.0,
    kurt: float = 3.0,
) -> Tuple[float, float, float]:
    """DSR of the best trial, deflated by the correlation-adjusted count :func:`effective_trials`.

    Returns ``(dsr, n_eff, var_sr)``. Suited to a correlated parameter grid, where the raw
    count over-deflates.
    """
    arr = np.asarray(list(sr_trials), dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size < 2:
        raise ValueError("deflated_sharpe needs >=2 trial Sharpes to deflate")
    n_eff = effective_trials(aligned_returns)
    var_sr = float(arr.var(ddof=1))
    best = float(arr.max())
    dsr = deflated_sharpe_ratio(best, n_obs, var_sr, n_eff, skew, kurt)
    return dsr, n_eff, var_sr


def min_backtest_length(n_trials: int, target_annual_sharpe: float = 1.0) -> float:
    """Minimum backtest length (MinBTL) in years for ``n_trials`` trials.

        MinBTL ≈ [ (1-γ)·Φ⁻¹(1-1/N) + γ·Φ⁻¹(1-1/(N·e)) ]² / SR_annual²

    The history needed for the expected best annual Sharpe of ``n_trials`` skill-less trials to
    fall below ``target_annual_sharpe`` (Bailey, Borwein, López de Prado & Zhu, 2014). Returns
    ``inf`` when ``n_trials < 2`` or the target is not positive.
    """
    if n_trials < 2 or target_annual_sharpe <= 0:
        return float("inf")
    n = float(n_trials)
    g = EULER_MASCHERONI
    z = (1.0 - g) * stats.norm.ppf(1.0 - 1.0 / n) + g * stats.norm.ppf(1.0 - 1.0 / (n * math.e))
    return float((z / target_annual_sharpe) ** 2)


# Trading sessions per year; one session per trading day (morning and afternoon together).
SESSIONS_PER_YEAR = 252.0


def sessions_to_years(n_sessions: int) -> float:
    """Convert a number of trading sessions to years."""
    return float(n_sessions) / SESSIONS_PER_YEAR
