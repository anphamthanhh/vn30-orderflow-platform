"""Tests for Combinatorial Purged Cross-Validation (CPCV)."""

from __future__ import annotations

from math import comb

import numpy as np
import pytest

from historical.optimization.cpcv import (
    CpcvInapplicable,
    cpcv_paths_sharpe,
    cpcv_pbo,
    cpcv_report,
    cpcv_splits,
)


def test_splits_count_and_disjoint_purged() -> None:
    n, g, k = 120, 6, 2
    splits = cpcv_splits(n, n_groups=g, k_test=k, embargo_frac=0.02)
    assert len(splits) == comb(g, k)  # C(6,2) = 15 paths
    for train_idx, test_idx in splits:
        # train and test never overlap; purge and embargo leave a gap, so union <= n
        assert set(train_idx).isdisjoint(set(test_idx))
        assert train_idx.size + test_idx.size <= n
        assert train_idx.size > 0 and test_idx.size > 0


def test_splits_reject_bad_k() -> None:
    try:
        cpcv_splits(100, n_groups=4, k_test=4)
    except ValueError:
        return
    raise AssertionError("expected ValueError for k_test >= n_groups")


def test_paths_sharpe_is_retired_and_refuses() -> None:
    """``cpcv_paths_sharpe`` raises: slices of one unfitted series are not out-of-sample paths."""
    rng = np.random.default_rng(7)
    rets = rng.normal(0.001, 0.01, size=200)
    with pytest.raises(CpcvInapplicable):
        cpcv_paths_sharpe(rets, n_groups=6, k_test=2)


def test_report_is_stamped_inapplicable_not_computed() -> None:
    """``cpcv_report`` returns an inapplicable stamp, never a pass, for any input.

    With at most 6 groups the 15 paths overlap heavily and add no independent evidence.
    """
    strong = np.full(200, 0.01) + np.random.default_rng(1).normal(0, 0.001, 200)
    rep = cpcv_report(strong)
    assert rep.applicable is False
    assert rep.passed is False, "an inapplicable test must never report a pass"
    assert rep.n_paths == 0
    assert "inapplicable" in rep.stamp.lower()

    # a clearly bad series gets the same stamp; it is not a verdict
    bad = np.random.default_rng(2).normal(-0.005, 0.01, 200)
    assert cpcv_report(bad).applicable is False


def test_cpcv_paths_never_enter_the_trial_count() -> None:
    """CPCV paths are not trials and do not add to the trial count."""
    rep = cpcv_report(np.random.default_rng(3).normal(0.001, 0.01, 200))
    assert rep.n_paths == 0


def test_report_no_paths_when_too_few_obs() -> None:
    r = cpcv_report([0.1, 0.2, 0.3])  # far fewer than n_groups
    assert r.n_paths == 0 and not r.passed


def test_cpcv_pbo_two_configs() -> None:
    rng = np.random.default_rng(3)
    good = rng.normal(0.002, 0.01, 180)
    noise = rng.normal(0.0, 0.01, 180)
    value, passed, thr = cpcv_pbo([good, noise], n_groups=6, k_test=2)
    assert 0.0 <= value <= 1.0 and thr == 0.05
    assert isinstance(passed, bool)
