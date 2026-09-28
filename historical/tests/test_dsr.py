"""Tests for the trials-deflated Sharpe ratio (``historical/optimization/dsr.py``)."""

from __future__ import annotations

import numpy as np
import pytest
from scipy import stats

from historical.optimization import dsr
from historical.optimization.overfitting import deflated_sharpe as old_deflated_sharpe


def _per_obs_sharpe(r):
    r = np.asarray(r, float)
    sd = r.std(ddof=1)
    return float(r.mean() / sd) if sd > 0 else 0.0


def test_psr_reproduces_old_deflated_sharpe_as_special_case():
    # with sr_benchmark=0, PSR equals overfitting.deflated_sharpe (a PSR against zero)
    rng = np.random.default_rng(1)
    r = rng.normal(0.05, 1.0, size=40)
    sr = _per_obs_sharpe(r)
    skew = float(stats.skew(r, bias=False))
    kurt = float(stats.kurtosis(r, fisher=False, bias=False))
    old_val = old_deflated_sharpe(r)[0]
    new_val = dsr.probabilistic_sharpe_ratio(sr, r.size, skew, kurt, sr_benchmark=0.0)
    assert abs(old_val - new_val) < 1e-9


def test_best_of_noise_false_pass_is_caught():
    # 200 pure-noise strategies of 21 observations each
    rng = np.random.default_rng(7)
    n_obs, n_trials = 21, 200
    sr_trials = [_per_obs_sharpe(rng.normal(0.0, 1.0, size=n_obs)) for _ in range(n_trials)]
    best = max(sr_trials)

    # PSR against zero rates the best noise strategy as confident
    psr_zero = dsr.probabilistic_sharpe_ratio(best, n_obs)
    assert psr_zero > 0.9

    # the trials-deflated DSR does not
    deflated, n, var_sr = dsr.deflated_sharpe_from_trials(sr_trials, n_obs)
    assert n == n_trials
    assert var_sr > 0
    assert deflated < 0.65, f"deflated DSR should not be confident on noise, got {deflated}"
    assert deflated < psr_zero  # deflation strictly lowers the score


def test_dsr_monotone_decreasing_in_trials():
    # fixed Sharpe and variance: more trials raise the expected maximum and lower the DSR
    vals = [
        dsr.deflated_sharpe_ratio(sr=0.6, n_obs=21, var_sr_across_trials=0.05, n_trials=n)
        for n in (2, 10, 50, 200, 1000)
    ]
    assert all(a >= b - 1e-12 for a, b in zip(vals, vals[1:])), vals


def test_min_backtest_length_exceeds_39_sessions():
    # 100 configs at a target annual Sharpe of 1.0 need years; 39 sessions are 0.155 years
    need_years = dsr.min_backtest_length(n_trials=100, target_annual_sharpe=1.0)
    have_years = dsr.sessions_to_years(39)
    assert need_years > have_years
    assert need_years > 1.0


def test_fail_loud_on_single_trial():
    with pytest.raises(ValueError):
        dsr.deflated_sharpe_from_trials([0.9], n_obs=21)


def test_effective_trials_bounds():
    rng = np.random.default_rng(3)
    n, obs = 10, 500
    # independent trials: N_eff close to N
    indep = rng.normal(size=(n, obs))
    assert dsr.effective_trials(indep) > 0.7 * n
    # identical trials: N_eff = 1
    ident = np.tile(rng.normal(size=obs), (n, 1))
    assert abs(dsr.effective_trials(ident) - 1.0) < 1e-6
    # correlated family (common factor + noise): 1 < N_eff < N
    factor = rng.normal(size=obs)
    corr = np.vstack([0.8 * factor + 0.2 * rng.normal(size=obs) for _ in range(n)])
    ne = dsr.effective_trials(corr)
    assert 1.0 < ne < n


def test_eff_dsr_relaxes_over_deflation_of_correlated_grid():
    # on a correlated grid, raw N deflates harder than effective N
    rng = np.random.default_rng(5)
    obs = 800
    factor = rng.normal(0.03, 1.0, size=obs)  # mild positive edge in the family
    mat = np.vstack([0.85 * factor + 0.15 * rng.normal(size=obs) for _ in range(12)])
    srs = [float(r.mean() / r.std(ddof=1)) for r in mat]
    d_raw, n_raw, _ = dsr.deflated_sharpe_from_trials(srs, n_obs=obs)
    d_eff, n_eff, _ = dsr.deflated_sharpe_from_trials_eff(srs, mat, n_obs=obs)
    assert n_eff < n_raw
    assert d_eff >= d_raw  # the correlation adjustment never lowers the DSR
