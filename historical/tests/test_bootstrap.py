"""Tests for the session-aligned stationary bootstrap: block lengths and session boundaries."""

from __future__ import annotations

import numpy as np
import pytest

from historical.optimization.overfitting import (
    BOOTSTRAP_CAVEAT,
    politis_white_block_length,
    sharpe_bootstrap_ci,
    stationary_bootstrap,
    stationary_bootstrap_indices,
    stationary_bootstrap_indices_with_starts,
    whites_reality_check,
)


def _ar1(n, phi, seed, scale=1.0):
    rng = np.random.default_rng(seed)
    e = rng.normal(0.0, scale, n)
    x = np.empty(n, dtype=float)
    x[0] = e[0]
    for i in range(1, n):
        x[i] = phi * x[i - 1] + e[i]
    return x


def _runs(idx_row):
    """Return the lengths of maximal runs of consecutive indices (the sampled blocks)."""
    lengths = []
    cur = 1
    for a, b in zip(idx_row[:-1], idx_row[1:]):
        if b == a + 1:
            cur += 1
        else:
            lengths.append(cur)
            cur = 1
    lengths.append(cur)
    return np.asarray(lengths, dtype=float)


# --- block behaviour ---


def test_mean_block_length_matches_requested():
    # one long session, so nothing truncates the geometric block lengths
    n = 4000
    idx = stationary_bootstrap_indices(n, avg_block_len=10.0, n_boot=20, seed=1)
    mean_run = float(np.mean(np.concatenate([_runs(row) for row in idx])))
    # Geometric(1/10) has mean 10; the band allows for Monte Carlo noise
    assert 8.0 < mean_run < 12.5, mean_run


def test_block_len_one_is_the_old_iid_bootstrap():
    n = 2000
    idx = stationary_bootstrap_indices(n, avg_block_len=1.0, n_boot=20, seed=2)
    mean_run = float(np.mean(np.concatenate([_runs(row) for row in idx])))
    assert mean_run < 1.05, mean_run  # every draw restarts -> iid


def test_longer_blocks_preserve_autocorrelation_iid_destroys_it():
    x = _ar1(3000, phi=0.7, seed=3)

    def lag1(a):
        a = a - a.mean()
        return float(np.dot(a[:-1], a[1:]) / np.dot(a, a))

    blocked = stationary_bootstrap(x, avg_block_len=25.0, n_boot=40, seed=4)
    iid = stationary_bootstrap(x, avg_block_len=1.0, n_boot=40, seed=4)
    rho_blocked = float(np.mean([lag1(r) for r in blocked]))
    rho_iid = float(np.mean([lag1(r) for r in iid]))
    assert rho_blocked > 0.4, rho_blocked  # dependence survives the resample
    assert abs(rho_iid) < 0.1, rho_iid  # iid resampling removes it


def test_politis_white_picks_longer_blocks_for_autocorrelated_series():
    b_iid = politis_white_block_length(np.random.default_rng(5).normal(size=1500))
    b_ar = politis_white_block_length(_ar1(1500, phi=0.8, seed=6))
    assert b_ar > b_iid, (b_ar, b_iid)
    assert b_ar >= 2.0


def test_politis_white_degenerate_inputs_are_iid_not_crash():
    assert politis_white_block_length([]) == 1.0
    assert politis_white_block_length([0.1, 0.2, 0.3]) == 1.0
    assert politis_white_block_length(np.zeros(50)) == 1.0


# --- session alignment ---


def _session_ids(n_sessions, per_session):
    return np.repeat(np.arange(n_sessions), per_session)


def test_blocks_never_straddle_a_session_boundary():
    n_sessions, per_session = 39, 30  # 39 sessions of 30 intraday observations
    sess = _session_ids(n_sessions, per_session)
    n = sess.size
    idx, fresh = stationary_bootstrap_indices_with_starts(
        n, avg_block_len=40.0, n_boot=50, seed=7, session_ids=sess
    )
    # use the explicit new-block mask: a fresh start can land on idx[t-1] + 1 by chance,
    # so contiguity alone cannot tell a continuation from a restart
    straddles = 0
    for row, fr in zip(idx, fresh):
        cont = ~fr[1:]  # positions that continue the previous block
        straddles += int(np.sum(cont & (sess[row[1:]] != sess[row[:-1]])))
    assert straddles == 0

    # continuations are strictly +1: a block is a contiguous run
    for row, fr in zip(idx, fresh):
        cont = ~fr[1:]
        assert np.all(row[1:][cont] == row[:-1][cont] + 1)

    # with avg_block_len 40 > session length 30, blocks are truncated at the session edge
    mean_run = float(np.mean(np.concatenate([_runs(row) for row in idx])))
    assert mean_run <= per_session, mean_run


def test_no_circular_wraparound_onto_the_first_session():
    # the textbook stationary bootstrap wraps the end of the series onto its start,
    # which would splice the last session onto the first
    sess = _session_ids(5, 20)
    n = sess.size
    idx = stationary_bootstrap_indices(
        n, avg_block_len=1000.0, n_boot=30, seed=8, session_ids=sess
    )
    wrapped = 0
    for row in idx:
        wrapped += int(np.sum((row[:-1] == n - 1) & (row[1:] == 0)))
    assert wrapped == 0


def test_session_ids_length_mismatch_is_refused():
    with pytest.raises(ValueError):
        stationary_bootstrap(np.zeros(10), session_ids=np.zeros(9), n_boot=2)


def test_resamples_are_drawn_from_the_original_values_and_seeded():
    r = np.arange(50, dtype=float)
    a = stationary_bootstrap(r, avg_block_len=5.0, n_boot=10, seed=11)
    b = stationary_bootstrap(r, avg_block_len=5.0, n_boot=10, seed=11)
    c = stationary_bootstrap(r, avg_block_len=5.0, n_boot=10, seed=12)
    assert a.shape == (10, 50)
    assert np.array_equal(a, b)  # seeded -> reproducible
    assert not np.array_equal(a, c)
    assert set(np.unique(a)).issubset(set(r.tolist()))


# --- the caveat travels with the interval ---


def test_interval_carries_the_bias_not_variance_caveat():
    r = np.random.default_rng(13).normal(0.05, 1.0, 39)
    ci = sharpe_bootstrap_ci(r, n_boot=300, seed=13)
    assert ci.caveat == BOOTSTRAP_CAVEAT
    assert "does NOT fix VARIANCE" in ci.caveat
    assert "REGIME_UNTESTED" in ci.caveat
    assert ci.lo < ci.point < ci.hi
    # with 39 sessions the interval is wide; the width is the result, not a defect
    assert (ci.hi - ci.lo) > 0.4
    assert "caveat" in ci.as_dict()


def test_thin_sample_interval_spans_zero_for_a_true_null():
    """A series with no edge does not produce a confidently positive interval."""
    r = np.random.default_rng(13).normal(0.0, 1.0, 39)
    ci = sharpe_bootstrap_ci(r, n_boot=300, seed=13)
    assert ci.lo < 0.0 < ci.hi, (ci.lo, ci.point, ci.hi)


def test_block_bootstrap_null_is_wider_than_the_old_iid_null():
    # on an autocorrelated series an iid null is too tight and overstates the WRC delta
    x = _ar1(600, phi=0.85, seed=14) + 0.05
    delta_iid, _, _ = whites_reality_check(x, n_boot=400, seed=15, avg_block_len=1.0)
    delta_block, _, _ = whites_reality_check(x, n_boot=400, seed=15)
    assert delta_block < delta_iid, (delta_block, delta_iid)


def test_wrc_detail_reports_block_diagnostics():
    from historical.optimization.overfitting import whites_reality_check_detail

    x = _ar1(400, phi=0.6, seed=16)
    (_d, _p, _t), detail = whites_reality_check_detail(x, n_boot=200, seed=16)
    assert detail["avg_block_len"] >= 1.0
    assert detail["n_blocks_effective"] > 0
    assert detail["caveat"] == BOOTSTRAP_CAVEAT
