"""Combinatorial purged cross-validation (López de Prado, 2018), reported as inapplicable here.

With fewer than :data:`CPCV_MIN_GROUPS` independent groups the combinatorial test paths
overlap too heavily to be evidence, so :func:`cpcv_report` always returns the inapplicability
stamp and :func:`cpcv_paths_sharpe` raises. The purged, embargoed split generator
(:func:`cpcv_splits`) and a PBO over its splits (:func:`cpcv_pbo`) remain for callers that fit
on the training indices. CPCV path counts never enter the DSR trial count.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Dict, List, Sequence, Tuple

import numpy as np

from historical.optimization.overfitting import pbo as _pbo

#: Text emitted on every verdict that requests CPCV.
CPCV_INAPPLICABLE_STAMP = (
    "CPCV inapplicable, N<12 groups; held-out afternoon + block bootstrap used instead."
)

#: Minimum number of independent groups for combinatorial paths to carry information.
CPCV_MIN_GROUPS = 12

# Unused; kept for import compatibility.
DEFAULT_PROB_OOS_LE0_MAX = 0.50


class CpcvInapplicable(RuntimeError):
    """Raised for CPCV path statistics; the message begins with the inapplicability stamp."""

    def __init__(self, msg: str = CPCV_INAPPLICABLE_STAMP) -> None:
        super().__init__(msg)


def _group_bounds(n: int, n_groups: int) -> List[Tuple[int, int]]:
    """Contiguous [start,end) bounds for ``n_groups`` near-equal groups."""
    edges = np.linspace(0, n, n_groups + 1).astype(int)
    return [(int(edges[i]), int(edges[i + 1])) for i in range(n_groups) if edges[i + 1] > edges[i]]


def cpcv_splits(
    n: int,
    n_groups: int = 6,
    k_test: int = 2,
    embargo_frac: float = 0.02,
) -> List[Tuple[np.ndarray, np.ndarray]]:
    """Purged and embargoed combinatorial train/test splits (López de Prado, 2018, ch. 12).

    Returns one ``(train_idx, test_idx)`` pair per ``C(n_groups, k_test)`` choice of contiguous
    test groups. Purging removes the observation on each side of every test block from
    training; the embargo also removes ``ceil(embargo_frac * n)`` observations after each test
    block. Returns ``[]`` when ``n`` is too small to form the groups.
    """
    if n <= 0:
        return []
    if not (1 <= k_test < n_groups):
        raise ValueError("require 1 <= k_test < n_groups")
    bounds = _group_bounds(n, n_groups)
    if len(bounds) < n_groups:
        # Too few observations to form the requested groups.
        return []
    embargo = int(np.ceil(embargo_frac * n)) if embargo_frac > 0 else 0

    splits: List[Tuple[np.ndarray, np.ndarray]] = []
    for combo in combinations(range(n_groups), k_test):
        test_mask = np.zeros(n, dtype=bool)
        purge_mask = np.zeros(n, dtype=bool)
        for g in combo:
            s, e = bounds[g]
            test_mask[s:e] = True
            # Purge one obs on each side of the test block.
            if s - 1 >= 0:
                purge_mask[s - 1] = True
            if e < n:
                purge_mask[e] = True
            # Embargo a forward window after the test block.
            if embargo > 0:
                purge_mask[e : min(e + embargo, n)] = True
        train_mask = ~test_mask & ~purge_mask
        test_idx = np.nonzero(test_mask)[0]
        train_idx = np.nonzero(train_mask)[0]
        if test_idx.size and train_idx.size:
            splits.append((train_idx, test_idx))
    return splits


def _sharpe(returns: np.ndarray) -> float:
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if r.size < 2:
        return 0.0
    sd = r.std(ddof=1)
    if sd == 0.0 or not np.isfinite(sd):
        return 0.0
    return float(r.mean() / sd)


def cpcv_paths_sharpe(*_args, **_kwargs) -> np.ndarray:
    """Not supported; always raises :class:`CpcvInapplicable`.

    Test blocks of one return series, with nothing fitted on the training indices, are not
    independent out-of-sample paths.
    """
    raise CpcvInapplicable(
        CPCV_INAPPLICABLE_STAMP
        + " (cpcv_paths_sharpe was a no-train slice of one series and is deleted;"
        " use overfitting.stationary_bootstrap + a held-out afternoon instead)"
    )


@dataclass(frozen=True)
class CpcvReport:
    """CPCV summary; :func:`cpcv_report` always returns the inapplicable form."""

    n_paths: int
    mean_oos_sharpe: float
    std_oos_sharpe: float
    prob_oos_le0: float
    passed: bool
    threshold: float
    applicable: bool = False
    stamp: str = CPCV_INAPPLICABLE_STAMP

    def as_dict(self) -> Dict[str, object]:
        return {
            "n_paths": int(self.n_paths),
            "mean_oos_sharpe": float(self.mean_oos_sharpe),
            "std_oos_sharpe": float(self.std_oos_sharpe),
            "prob_oos_le0": float(self.prob_oos_le0),
            "passed": bool(self.passed),
            "threshold": float(self.threshold),
            "applicable": bool(self.applicable),
            "stamp": self.stamp,
        }


def cpcv_report(*_args, **_kwargs) -> CpcvReport:
    """Return the inapplicability stamp: ``applicable=False``, ``passed=False``, ``n_paths=0``.

    The gate treats it as a flag, never as a check that can pass; ``n_paths=0`` keeps CPCV out
    of every trial count.
    """
    return CpcvReport(
        n_paths=0,
        mean_oos_sharpe=float("nan"),
        std_oos_sharpe=float("nan"),
        prob_oos_le0=float("nan"),
        passed=False,
        threshold=float("nan"),
        applicable=False,
        stamp=CPCV_INAPPLICABLE_STAMP,
    )


def cpcv_pbo(
    config_returns: Sequence[Sequence[float]],
    n_groups: int = 6,
    k_test: int = 2,
    embargo_frac: float = 0.02,
    threshold: float = 0.05,
) -> Tuple[float, bool, float]:
    """PBO across CPCV splits for a ``(n_configs, n_obs)`` matrix of per-observation returns.

    Builds per-split in-sample (training indices) and out-of-sample Sharpe matrices and
    delegates to :func:`historical.optimization.overfitting.pbo`; returns
    ``(1.0, False, threshold)`` when no split can be formed. Not used by the gate: below
    :data:`CPCV_MIN_GROUPS` groups the paths overlap heavily, so the value is descriptive.
    """
    m = np.asarray(config_returns, dtype=float)
    if m.ndim != 2 or m.shape[0] < 2:
        raise ValueError("config_returns must be (C>=2 configs x T observations)")
    n_cfg, t = m.shape
    splits = cpcv_splits(t, n_groups=n_groups, k_test=k_test, embargo_frac=embargo_frac)
    if not splits:
        return 1.0, False, threshold
    is_rows: List[List[float]] = []
    oos_rows: List[List[float]] = []
    for train_idx, test_idx in splits:
        is_rows.append([_sharpe(m[c, train_idx]) for c in range(n_cfg)])
        oos_rows.append([_sharpe(m[c, test_idx]) for c in range(n_cfg)])
    return _pbo(is_rows, oos_rows, threshold=threshold)
