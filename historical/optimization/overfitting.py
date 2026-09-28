"""Overfitting tests on a strategy's returns; each test returns ``(value, passed, threshold)``.

Tests: Probabilistic Sharpe vs zero (no trial deflation; see ``dsr`` for that), PBO,
White's Reality Check and an Aronson-style robustness suite. Resampling uses a session-aligned
stationary bootstrap (Politis & Romano, 1994) with the Politis & White (2004) block length, so
no block crosses a session boundary. The block bootstrap corrects the bias of an i.i.d.
resample on autocorrelated returns but not small-sample variance, so every interval carries
:data:`BOOTSTRAP_CAVEAT`. All randomness is seeded by the caller.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

import numpy as np
from scipy import stats

# --- PSR-vs-zero pass threshold, scaled by trade count ----------------------
DSR_MIN_TRADES = 10

#: Caveat attached to every bootstrap interval and diagnostics dict from this module.
BOOTSTRAP_CAVEAT = (
    "BLOCK-BOOTSTRAP CAVEAT: the stationary bootstrap removes the iid BIAS "
    "(falsely-tight nulls on autocorrelated returns); it does NOT fix VARIANCE. "
    "With only a few dozen session blocks the interval is wide and unstable and "
    "the Politis-White block length is itself noisy at this N. That width is the "
    "correct signal, not a defect. It measures resampling error WITHIN one "
    "regime -> REGIME_UNTESTED; it says nothing about a new regime."
)


def _dsr_threshold(n_trades: int) -> float:
    """PSR-vs-zero pass threshold for ``n_trades``; ``inf`` (unreachable) below 10 trades."""
    if n_trades < DSR_MIN_TRADES:
        return float("inf")
    if n_trades <= 20:
        return 0.50
    if n_trades <= 50:
        return 0.45
    return 0.40


def _sharpe(returns: np.ndarray) -> float:
    """Per-observation Sharpe (mean / sample std) of the finite values; 0.0 when undefined."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if r.size < 2:
        return 0.0
    sd = r.std(ddof=1)
    if sd == 0.0 or not np.isfinite(sd):
        return 0.0
    return float(r.mean() / sd)


# --- Stationary block bootstrap (Politis & Romano, 1994) --------------------


def politis_white_block_length(
    returns: Sequence[float],
    b_max: Optional[float] = None,
) -> float:
    """Mean block length for the stationary bootstrap (Politis & White, 2004).

    With the Patton, Politis & White (2009) correction::

        b_opt = ( 2 * Ghat^2 / D_SB )^(1/3) * n^(1/3)

    where ``Ghat = sum lam(|k|/M) |k| R(k)`` and ``D_SB = 2 * (sum lam(|k|/M) R(k))^2`` over
    ``|k| <= M``, ``R`` is the sample autocovariance, ``lam`` the flat-top lag window and
    ``M = 2*m``, with lags ``m+1 .. m+K_N`` the first run inside ``2*sqrt(log10(n)/n)``. The
    result is clipped to ``[1, b_max]`` (default ``ceil(min(3*sqrt(n), n/3))``); short or
    degenerate input returns 1.0, i.e. an i.i.d. bootstrap.
    """
    r = np.asarray(list(returns), dtype=float)
    r = r[np.isfinite(r)]
    n = int(r.size)
    if n < 8:
        return 1.0
    x = r - r.mean()
    acov0 = float(np.dot(x, x)) / n
    if acov0 <= 0 or not np.isfinite(acov0):
        return 1.0

    k_n = max(5, int(math.ceil(math.sqrt(math.log10(n)))))
    m_max = int(math.ceil(math.sqrt(n))) + k_n
    m_max = min(m_max, n - 1)
    if m_max < 1:
        return 1.0

    acov = np.empty(m_max + 1, dtype=float)
    for k in range(m_max + 1):
        acov[k] = float(np.dot(x[: n - k], x[k:])) / n
    rho = acov / acov[0]

    crit = 2.0 * math.sqrt(math.log10(n) / n)
    m_hat = m_max
    for m in range(1, m_max + 1):
        hi = min(m + k_n, m_max + 1)
        window = np.abs(rho[m:hi])
        if window.size and bool(np.all(window < crit)):
            m_hat = m - 1
            break
    m_hat = max(int(m_hat), 1)

    big_m = max(min(2 * m_hat, m_max), 1)
    ks = np.arange(-big_m, big_m + 1)
    t = np.abs(ks) / float(big_m)
    lam = np.where(t <= 0.5, 1.0, np.where(t <= 1.0, 2.0 * (1.0 - t), 0.0))
    rk = acov[np.abs(ks)]
    g_hat = float(np.sum(lam * np.abs(ks) * rk))
    g0 = float(np.sum(lam * rk))
    d_sb = 2.0 * g0 * g0
    if d_sb <= 0 or not np.isfinite(d_sb) or g_hat == 0.0:
        return 1.0

    b = ((2.0 * g_hat * g_hat) / d_sb) ** (1.0 / 3.0) * (n ** (1.0 / 3.0))
    cap = b_max if b_max is not None else math.ceil(min(3.0 * math.sqrt(n), n / 3.0))
    if not np.isfinite(b):
        return 1.0
    return float(min(max(b, 1.0), max(float(cap), 1.0)))


def _session_starts(session_ids: Optional[Sequence]) -> Optional[np.ndarray]:
    """Mask that is True where a new session starts; None when no session ids are given."""
    if session_ids is None:
        return None
    s = np.asarray(list(session_ids))
    if s.size == 0:
        return None
    starts = np.ones(s.size, dtype=bool)
    starts[1:] = s[1:] != s[:-1]
    return starts


def stationary_bootstrap_indices(
    n: int,
    avg_block_len: float,
    n_boot: int,
    seed: int = 12345,
    session_ids: Optional[Sequence] = None,
) -> np.ndarray:
    """Index matrix ``(n_boot, n)`` for the session-aligned stationary bootstrap.

    Block lengths are geometric with mean ``avg_block_len`` (Politis & Romano, 1994). A block
    walks forward from a uniform random start and restarts at a new random start when the next
    index would pass the series end or fall in another session (``session_ids`` group). There
    is no circular wrap-around, which would join the last session to the first.
    """
    idx, _ = stationary_bootstrap_indices_with_starts(
        n, avg_block_len, n_boot, seed=seed, session_ids=session_ids
    )
    return idx


def stationary_bootstrap_indices_with_starts(
    n: int,
    avg_block_len: float,
    n_boot: int,
    seed: int = 12345,
    session_ids: Optional[Sequence] = None,
) -> "tuple[np.ndarray, np.ndarray]":
    """As :func:`stationary_bootstrap_indices`, plus a mask that is True where a block starts.

    The mask makes the session-alignment invariant testable: a random restart can land on
    ``idx[t-1] + 1`` by chance, so block continuity cannot be inferred from the indices alone.
    """
    nb = max(int(n_boot), 0)
    if n <= 0:
        return np.zeros((nb, 0), dtype=np.int64), np.zeros((nb, 0), dtype=bool)
    starts = _session_starts(session_ids)
    if starts is None or starts.size != n:
        sess = np.zeros(n, dtype=np.int64)
    else:
        sess = np.cumsum(starts.astype(np.int64)) - 1
    p = 1.0 / max(float(avg_block_len), 1.0)
    rng = np.random.default_rng(int(seed))
    out = np.empty((nb, n), dtype=np.int64)
    fresh = np.zeros((nb, n), dtype=bool)
    for b in range(nb):
        idx = int(rng.integers(0, n))
        out[b, 0] = idx
        fresh[b, 0] = True
        for t in range(1, n):
            nxt = idx + 1
            if nxt >= n or sess[nxt] != sess[idx] or rng.random() < p:
                idx = int(rng.integers(0, n))
                fresh[b, t] = True
            else:
                idx = nxt
            out[b, t] = idx
    return out, fresh


def stationary_bootstrap(
    returns: Sequence[float],
    avg_block_len: Optional[float] = None,
    n_boot: int = 1000,
    seed: int = 12345,
    session_ids: Optional[Sequence] = None,
) -> np.ndarray:
    """Session-aligned stationary-bootstrap resamples of the finite returns, ``(n_boot, n)``.

    ``avg_block_len=None`` selects the length with :func:`politis_white_block_length`. Raises
    ``ValueError`` when ``session_ids`` and ``returns`` differ in length.
    """
    r = np.asarray(list(returns), dtype=float)
    finite = np.isfinite(r)
    sid: Optional[np.ndarray]
    if session_ids is not None:
        sid_arr = np.asarray(list(session_ids))
        if sid_arr.size != r.size:
            raise ValueError(
                f"session_ids length {sid_arr.size} != returns length {r.size}; "
                "blocks cannot be session-aligned against a mismatched index"
            )
        sid = sid_arr[finite]
    else:
        sid = None
    r = r[finite]
    if r.size == 0:
        return np.zeros((int(n_boot), 0), dtype=float)
    b = politis_white_block_length(r) if avg_block_len is None else float(avg_block_len)
    idx = stationary_bootstrap_indices(r.size, b, n_boot, seed=seed, session_ids=sid)
    return r[idx]


@dataclass(frozen=True)
class BootstrapInterval:
    """Bootstrap point estimate and percentile interval, with block diagnostics and caveat."""

    point: float
    lo: float
    hi: float
    n_boot: int
    avg_block_len: float
    n_blocks_effective: float
    caveat: str = BOOTSTRAP_CAVEAT

    def as_dict(self) -> Dict[str, object]:
        return {
            "point": float(self.point),
            "lo": float(self.lo),
            "hi": float(self.hi),
            "n_boot": int(self.n_boot),
            "avg_block_len": float(self.avg_block_len),
            "n_blocks_effective": float(self.n_blocks_effective),
            "caveat": self.caveat,
        }

    def __str__(self) -> str:  # pragma: no cover - formatting only
        return (
            f"{self.point:.4f} [{self.lo:.4f}, {self.hi:.4f}] "
            f"(block~{self.avg_block_len:.1f}, ~{self.n_blocks_effective:.0f} blocks)\n"
            f"{self.caveat}"
        )


def sharpe_bootstrap_ci(
    returns: Sequence[float],
    n_boot: int = 1000,
    seed: int = 12345,
    session_ids: Optional[Sequence] = None,
    avg_block_len: Optional[float] = None,
    alpha: float = 0.05,
) -> BootstrapInterval:
    """Per-observation Sharpe with a session-aligned block-bootstrap percentile interval.

    Returns a :class:`BootstrapInterval` at confidence ``1 - alpha``; the bounds are NaN with
    fewer than two finite returns.
    """
    r = np.asarray(list(returns), dtype=float)
    keep = np.isfinite(r)
    sid = None
    if session_ids is not None:
        sid_arr = np.asarray(list(session_ids))
        if sid_arr.size == r.size:
            sid = sid_arr[keep]
    r = r[keep]
    b = politis_white_block_length(r) if avg_block_len is None else float(avg_block_len)
    if r.size < 2:
        return BootstrapInterval(0.0, float("nan"), float("nan"), 0, b, 0.0)
    boots = stationary_bootstrap(
        r, avg_block_len=b, n_boot=n_boot, seed=seed, session_ids=sid
    )
    sr = np.array([_sharpe(row) for row in boots], dtype=float)
    lo = float(np.percentile(sr, 100.0 * alpha / 2.0))
    hi = float(np.percentile(sr, 100.0 * (1.0 - alpha / 2.0)))
    return BootstrapInterval(
        point=_sharpe(r),
        lo=lo,
        hi=hi,
        n_boot=int(sr.size),
        avg_block_len=float(b),
        n_blocks_effective=float(r.size) / max(float(b), 1.0),
    )


# --- Probabilistic Sharpe vs zero (no trial deflation) ----------------------


def probabilistic_sharpe_vs_zero(returns: Sequence[float]) -> Tuple[float, bool, float]:
    """P(true Sharpe > 0) with skew, kurtosis and sample-size correction; not a deflated Sharpe.

    The Probabilistic Sharpe Ratio against a zero benchmark (Bailey & López de Prado, 2012)::

        PSR = Phi( SR * sqrt(N-1) / sqrt(1 - skew*SR + (kurt-1)/4 * SR^2) ),  SR = mean/std

    With no trial-count deflation, a best-of-N winner on noise still scores near 1; the
    deflated figure comes from ``trials_ledger.deflated_sharpe_for_returns``. Passes when
    ``N >= 10`` and PSR exceeds the trade-count threshold.
    """
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    n = r.size
    thr = _dsr_threshold(n)
    if n < 2:
        return 0.0, False, thr

    sr = _sharpe(r)
    skew = float(stats.skew(r, bias=False)) if n > 2 else 0.0
    # non-excess kurtosis (normal = 3)
    kurt = float(stats.kurtosis(r, fisher=False, bias=False)) if n > 3 else 3.0

    denom = 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * (sr**2)
    if denom <= 0 or not np.isfinite(denom):
        return 0.0, False, thr

    z = sr * np.sqrt(max(n - 1, 1)) / np.sqrt(denom)
    psr = float(stats.norm.cdf(z))
    passed = bool(n >= DSR_MIN_TRADES and psr > thr)
    return psr, passed, thr


#: Deprecated alias of :func:`probabilistic_sharpe_vs_zero`; it is not a deflated Sharpe.
deflated_sharpe = probabilistic_sharpe_vs_zero


def whites_reality_check_detail(
    returns: Sequence[float],
    n_boot: int = 1000,
    seed: int = 12345,
    delta_threshold: float = 0.30,
    session_ids: Optional[Sequence] = None,
    avg_block_len: Optional[float] = None,
) -> Tuple[Tuple[float, bool, float], Dict[str, object]]:
    """As :func:`whites_reality_check`, plus diagnostics (block length, null p95, caveat)."""
    r = np.asarray(returns, dtype=float)
    mask = np.isfinite(r)
    sid = None
    if session_ids is not None:
        sid_arr = np.asarray(list(session_ids))
        if sid_arr.size == r.size:
            sid = sid_arr[mask]
    r = r[mask]
    if r.size < 2:
        return (0.0, False, delta_threshold), {
            "note": "fewer than 2 finite observations",
            "caveat": BOOTSTRAP_CAVEAT,
        }

    b = politis_white_block_length(r) if avg_block_len is None else float(avg_block_len)
    is_sharpe = _sharpe(r)
    null = r - r.mean()  # recenter to zero-edge null
    boots = stationary_bootstrap(
        null, avg_block_len=b, n_boot=n_boot, seed=seed, session_ids=sid
    )
    boot = np.array([_sharpe(row) for row in boots], dtype=float)
    p95 = float(np.percentile(boot, 95))
    delta = float(is_sharpe - p95)
    detail: Dict[str, object] = {
        "is_sharpe": float(is_sharpe),
        "null_p95": p95,
        "avg_block_len": float(b),
        "n_blocks_effective": float(r.size) / max(float(b), 1.0),
        "n_boot": int(boot.size),
        "caveat": BOOTSTRAP_CAVEAT,
    }
    return (delta, bool(delta >= delta_threshold), float(delta_threshold)), detail


def whites_reality_check(
    returns: Sequence[float],
    n_boot: int = 1000,
    seed: int = 12345,
    delta_threshold: float = 0.30,
    session_ids: Optional[Sequence] = None,
    avg_block_len: Optional[float] = None,
) -> Tuple[float, bool, float]:
    """Data-snooping check after White's Reality Check (White, 2000), on one return series.

    The null is the de-meaned series (zero edge), resampled ``n_boot`` times with the
    session-aligned stationary bootstrap. Returns ``(delta, passed, threshold)`` with
    ``delta = observed Sharpe - null 95th-percentile Sharpe``; passes when
    ``delta >= delta_threshold``. Unlike White's test it does not take the maximum over a
    universe of strategies.
    """
    return whites_reality_check_detail(
        returns,
        n_boot=n_boot,
        seed=seed,
        delta_threshold=delta_threshold,
        session_ids=session_ids,
        avg_block_len=avg_block_len,
    )[0]


def pbo(
    is_matrix: Sequence[Sequence[float]],
    oos_matrix: Sequence[Sequence[float]],
    threshold: float = 0.05,
) -> Tuple[float, bool, float]:
    """Probability of Backtest Overfitting (Bailey, Borwein, López de Prado & Zhu, 2017).

    Inputs are ``(n_folds, n_configs)`` performance matrices, one row per split. For each fold
    the in-sample best configuration's out-of-sample relative rank ``w`` gives the logit
    ``lambda = ln(w / (1 - w))``; PBO is the share of folds with ``lambda < 0``, i.e. below the
    out-of-sample median. Returns ``(pbo, pbo < threshold, threshold)``, or
    ``(1.0, False, threshold)`` with no folds or fewer than two configurations.
    """
    is_m = np.asarray(is_matrix, dtype=float)
    oos_m = np.asarray(oos_matrix, dtype=float)
    if is_m.ndim != 2 or is_m.shape != oos_m.shape:
        raise ValueError("is_matrix and oos_matrix must be equal-shaped 2D arrays")

    n_folds, n_cfg = is_m.shape
    if n_folds == 0 or n_cfg < 2:
        return 1.0, False, threshold

    below = 0
    for f in range(n_folds):
        best_cfg = int(np.argmax(is_m[f]))
        oos_row = oos_m[f]
        # OOS relative rank of the IS-best config, in [0, 1]
        rank = float((oos_row < oos_row[best_cfg]).sum()) / max(n_cfg - 1, 1)
        w = (rank * n_cfg + 0.5) / (n_cfg + 1)  # in (0,1), avoids 0/1 logit blowup
        lam = np.log(w / (1.0 - w))
        if lam < 0.0:
            below += 1

    pbo_value = float(below) / float(n_folds)
    return pbo_value, bool(pbo_value < threshold), float(threshold)


# --- Aronson-style robustness suite: four resampling tests, pass at 3 of 4 --

ARONSON_TEST_NAMES = (
    "block_resample_recovery",
    "block_bootstrap_sign",
    "session_sign_flip_null",
    "top_contributor_jackknife",
)
ARONSON_N_TESTS = len(ARONSON_TEST_NAMES)
ARONSON_PASS_FRACTION = 0.75  # 3 of 4

_BLOCK_SIGN_MIN = 0.95  # >=95% of block resamples must stay profitable
_RECOVERY_FRACTION = 0.60  # median block-resample Sharpe vs base
_JACKKNIFE_FRACTION = 0.30  # Sharpe retained after dropping top contributors


def _aronson_block_resample_recovery(boots: np.ndarray, base: float) -> bool:
    """Pass if the median block-resample Sharpe is at least 60 % of a positive base Sharpe."""
    if base <= 0 or boots.size == 0:
        return False
    recovered = float(np.median(np.array([_sharpe(row) for row in boots], dtype=float)))
    return bool(recovered >= _RECOVERY_FRACTION * base)


def _aronson_block_bootstrap_sign(boots: np.ndarray) -> bool:
    """Pass if at least 95 % of the block-bootstrap resamples have a positive Sharpe."""
    if boots.size == 0:
        return False
    sr = np.array([_sharpe(row) for row in boots], dtype=float)
    return bool(float(np.mean(sr > 0.0)) >= _BLOCK_SIGN_MIN)


def _aronson_session_sign_flip_null(
    r: np.ndarray,
    rng: np.random.Generator,
    base: float,
    session_ids: Optional[np.ndarray],
    n_perm: int = 500,
) -> bool:
    """Pass if the base Sharpe beats the 95th percentile of a session sign-flip null.

    Each session (each observation without session ids) is negated with probability 0.5,
    which removes directional edge but keeps magnitudes and within-session dependence.
    """
    if r.size < 3 or base <= 0:
        return False
    groups = np.arange(r.size) if session_ids is None else np.asarray(session_ids)
    _uniq, inv = np.unique(groups, return_inverse=True)
    n_groups = int(_uniq.size)
    null = np.empty(int(n_perm), dtype=float)
    for i in range(int(n_perm)):
        signs = rng.choice(np.array([-1.0, 1.0]), size=n_groups)
        null[i] = _sharpe(r * signs[inv])
    return bool(base > float(np.percentile(null, 95)))


def _aronson_top_contributor_jackknife(r: np.ndarray, base: float) -> bool:
    """Top-contributor jackknife: drop the largest 5 % of returns (at least one).

    Passes if the remaining Sharpe is positive and at least 30 % of the base Sharpe.
    """
    if r.size < 3 or base <= 0:
        return False
    k = max(1, int(math.ceil(0.05 * r.size)))
    order = np.argsort(r)
    jk = np.delete(r, order[-k:])
    if jk.size < 2:
        return False
    jk_sr = _sharpe(jk)
    return bool(jk_sr > 0.0 and jk_sr >= _JACKKNIFE_FRACTION * base)


def aronson_suite_detail(
    trade_returns: Sequence[float],
    seed: int = 12345,
    session_ids: Optional[Sequence] = None,
    n_boot: int = 500,
    avg_block_len: Optional[float] = None,
) -> Tuple[Tuple[float, bool, float], Dict[str, object]]:
    """As :func:`aronson_suite`, plus per-test results, block diagnostics and caveat."""
    r = np.asarray(trade_returns, dtype=float)
    mask = np.isfinite(r)
    sid: Optional[np.ndarray] = None
    if session_ids is not None:
        sid_arr = np.asarray(list(session_ids))
        if sid_arr.size == r.size:
            sid = sid_arr[mask]
    r = r[mask]
    threshold = ARONSON_PASS_FRACTION
    if r.size < 3:
        return (0.0, False, threshold), {
            "note": "fewer than 3 finite trades",
            "tests": {},
            "n_passed": 0,
            "n_tests": ARONSON_N_TESTS,
            "caveat": BOOTSTRAP_CAVEAT,
        }

    rng = np.random.default_rng(int(seed))
    base = _sharpe(r)
    b = politis_white_block_length(r) if avg_block_len is None else float(avg_block_len)
    boots = stationary_bootstrap(
        r, avg_block_len=b, n_boot=n_boot, seed=int(seed) + 1, session_ids=sid
    )

    results = {
        "block_resample_recovery": _aronson_block_resample_recovery(boots, base),
        "block_bootstrap_sign": _aronson_block_bootstrap_sign(boots),
        "session_sign_flip_null": _aronson_session_sign_flip_null(r, rng, base, sid),
        "top_contributor_jackknife": _aronson_top_contributor_jackknife(r, base),
    }
    n_pass = int(sum(bool(v) for v in results.values()))
    frac = float(n_pass) / float(ARONSON_N_TESTS)
    detail: Dict[str, object] = {
        "tests": {k: bool(v) for k, v in results.items()},
        "n_passed": n_pass,
        "n_tests": ARONSON_N_TESTS,
        "base_sharpe": float(base),
        "avg_block_len": float(b),
        "n_blocks_effective": float(r.size) / max(float(b), 1.0),
        "caveat": BOOTSTRAP_CAVEAT,
    }
    return (frac, bool(frac >= threshold), threshold), detail


def aronson_suite(
    trade_returns: Sequence[float],
    seed: int = 12345,
    session_ids: Optional[Sequence] = None,
) -> Tuple[float, bool, float]:
    """Aronson-style robustness suite on trade returns; passes when 3 of the 4 tests pass.

    Tests: block-resample recovery, block-bootstrap sign, session sign-flip null and
    top-contributor jackknife. Returns ``(fraction_passed, passed, 0.75)``.
    """
    return aronson_suite_detail(trade_returns, seed=seed, session_ids=session_ids)[0]


@dataclass(frozen=True)
class OverfittingResults:
    """Bundle of the four test outputs (value, passed, threshold each)."""

    dsr: Tuple[float, bool, float]
    wrc: Tuple[float, bool, float]
    pbo: Tuple[float, bool, float]
    aronson: Tuple[float, bool, float]

    def as_dict(self) -> Dict[str, Dict[str, float]]:
        def _fmt(t):
            return {"value": float(t[0]), "passed": bool(t[1]), "threshold": float(t[2])}

        return {
            "probabilistic_sharpe_vs_zero": _fmt(self.dsr),
            "whites_reality_check": _fmt(self.wrc),
            "pbo": _fmt(self.pbo),
            "aronson_suite": _fmt(self.aronson),
        }
