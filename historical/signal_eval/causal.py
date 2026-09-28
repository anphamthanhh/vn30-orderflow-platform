"""Lookahead detector: recompute a forecast on truncated prefixes and require agreement.

A causal ``forecast_fn`` returns, on ``bars.iloc[:k]``, the values that ``forecast_fn(bars)``
reports for the same timestamps. Whole-sample statistics (quantile, mean, max), ``shift(-1)``,
centred rolling windows and full-sample fits all break this. Not detected: a leak confined to
bars before the first probe (such as a whole-sample maximum reached in the first 10 % of the
sample) and lookahead already present in ``bars`` itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

import numpy as np
import pandas as pd

from shared.models.forecast import Forecast

__all__ = ["LookaheadError", "ProbeResult", "CausalReport", "check_causal", "assert_causal"]

ForecastFn = Callable[[pd.DataFrame], "Forecast | pd.Series"]


class LookaheadError(AssertionError):
    """Raised by :func:`assert_causal` when a prefix recomputation disagrees."""


@dataclass(frozen=True)
class ProbeResult:
    """One prefix recomputation."""

    k: int
    """Prefix length (bars fed to ``forecast_fn``)."""
    timestamp: pd.Timestamp | None
    """Earliest disagreeing timestamp, or the prefix's last timestamp when ``ok``."""
    prefix_value: float
    full_value: float
    delta: float
    """``|prefix - full|`` at the mismatch, or the largest difference over the prefix when ``ok``.

    ``inf`` when exactly one side is NaN.
    """
    ok: bool
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "k": self.k,
            "timestamp": (
                None if self.timestamp is None else pd.Timestamp(self.timestamp).isoformat()
            ),
            "prefix_value": (
                None if not np.isfinite(self.prefix_value) else float(self.prefix_value)
            ),
            "full_value": None if not np.isfinite(self.full_value) else float(self.full_value),
            "delta": None if not np.isfinite(self.delta) else float(self.delta),
            "ok": bool(self.ok),
            "note": self.note,
        }


@dataclass(frozen=True)
class CausalReport:
    """Outcome of :func:`check_causal`."""

    passed: bool
    n_probes: int
    n_compared: int
    tol: float
    probes: tuple[ProbeResult, ...] = field(default_factory=tuple)
    first_mismatch: ProbeResult | None = None

    def summary(self) -> str:
        if self.passed:
            return (
                f"CAUSAL OK: {self.n_compared}/{self.n_probes} probes recomputed on "
                f"truncated prefixes agreed with the full-sample run (tol={self.tol:g})."
            )
        m = self.first_mismatch
        assert m is not None
        return (
            "LOOKAHEAD DETECTED: recomputing the forecast on a truncated prefix does not "
            "reproduce the full-sample value.\n"
            f"  first disagreement at {pd.Timestamp(m.timestamp)} (prefix length k={m.k})\n"
            f"  value knowing only data up to that bar : {m.prefix_value!r}\n"
            f"  value the full-sample run reports there: {m.full_value!r}\n"
            f"  |difference| = {m.delta!r}  (tolerance {self.tol:g})\n"
            f"  {sum(1 for p in self.probes if not p.ok)}/{self.n_compared} probes disagree.\n"
            "  The forecast at that bar depends on data that did not exist yet. Usual "
            "causes: a whole-sample quantile/mean/std, shift(-1), a centred rolling "
            "window, or a parameter fitted on the full sample."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": bool(self.passed),
            "n_probes": self.n_probes,
            "n_compared": self.n_compared,
            "tol": self.tol,
            "first_mismatch": (
                None if self.first_mismatch is None else self.first_mismatch.to_dict()
            ),
            "probes": [p.to_dict() for p in self.probes],
        }


def _to_series(out: "Forecast | pd.Series") -> pd.Series:
    if isinstance(out, Forecast):
        return out.values
    if isinstance(out, pd.Series):
        return out.astype("float64")
    raise TypeError(f"forecast_fn must return a Forecast or a pd.Series, got {type(out)!r}.")


#: Early probe points as fractions of the sample length (see :func:`_probe_cuts`).
_EARLY_PROBE_FRACTIONS: tuple[float, ...] = (0.10, 0.20, 0.30)


def _probe_cuts(n: int, n_probes: int, min_prefix_frac: float) -> list[int]:
    """Prefix lengths to probe: evenly spaced from ``min_prefix_frac`` to the end, plus early cuts.

    Late probes compare warmed-up values. Early cuts catch leaks such as ``close / close.max()``
    that are visible only from a prefix ending before the global extremum; a prefix that is
    still all-NaN is recorded as skipped.
    """
    start = max(3, int(np.ceil(n * min_prefix_frac)))
    if start >= n:
        start = max(3, n - 1)
    if start >= n:
        return []
    back = np.linspace(start, n - 1, num=max(1, n_probes)).round().astype(int)
    early = np.array([int(np.ceil(n * f)) for f in _EARLY_PROBE_FRACTIONS], dtype=int)
    cuts = np.unique(np.concatenate([early, back]))
    return [int(k) for k in cuts if 3 <= k <= n - 1]


def check_causal(
    forecast_fn: ForecastFn,
    bars: pd.DataFrame,
    n_probes: int = 8,
    tol: float = 1e-9,
    *,
    min_prefix_frac: float = 0.5,
    cuts: Sequence[int] | None = None,
) -> CausalReport:
    """Check ``forecast_fn`` for lookahead; return a :class:`CausalReport` instead of raising.

    Each probe recomputes the forecast on ``bars.iloc[:k]`` and compares it with the full run
    over the whole prefix; ``passed`` is False if any value differs by more than ``tol``.
    Probes are ``n_probes`` cuts from ``min_prefix_frac`` to the end plus early cuts, unless
    ``cuts`` gives the prefix lengths.

    ``forecast_fn`` must be a pure function of its input frame, returning a ``Forecast`` or a
    ``pd.Series`` on the bar index; ``bars`` needs an ascending index and at least 6 rows.
    Raises ``ValueError`` if no probe could be compared (every prefix empty or all-NaN).
    """
    if not isinstance(bars, pd.DataFrame):
        raise TypeError(f"bars must be a DataFrame, got {type(bars)!r}.")
    n = len(bars)
    if n < 6:
        raise ValueError(f"need >= 6 bars to probe causality, got {n}.")
    if not bars.index.is_monotonic_increasing:
        raise ValueError("bars index must be sorted ascending; prefixes assume chronology.")

    full = _to_series(forecast_fn(bars))
    probe_ks = (
        [int(k) for k in cuts] if cuts is not None else _probe_cuts(n, n_probes, min_prefix_frac)
    )
    if not probe_ks:
        raise ValueError("no usable probe points; sample too short.")

    results: list[ProbeResult] = []
    compared = 0
    first_bad: ProbeResult | None = None

    for k in probe_ks:
        prefix = bars.iloc[:k]
        try:
            sp = _to_series(forecast_fn(prefix))
        except Exception as exc:  # noqa: BLE001 - a warm-up guard is legitimate
            results.append(
                ProbeResult(k, None, np.nan, np.nan, np.nan, True, f"skipped: raised {exc!r}")
            )
            continue
        if len(sp) == 0:
            results.append(ProbeResult(k, None, np.nan, np.nan, np.nan, True, "skipped: empty"))
            continue

        ts = sp.index[-1]
        pv = float(sp.iloc[-1])
        if ts not in full.index:
            bad = ProbeResult(
                k,
                ts,
                pv,
                np.nan,
                np.inf,
                False,
                "prefix emitted a timestamp the full run does not have",
            )
            results.append(bad)
            compared += 1
            first_bad = first_bad or bad
            continue

        # Compare the whole prefix, not only its last point: leaks such as
        # `close / close.max()` or `sign(x - mean(x))` can agree at the last point while
        # earlier values differ. The earliest disagreement localises the leak.
        aligned = full.reindex(sp.index)
        pv_arr = sp.to_numpy(dtype="float64")
        fv_arr = aligned.to_numpy(dtype="float64")
        p_fin, f_fin = np.isfinite(pv_arr), np.isfinite(fv_arr)
        missing = aligned.isna().to_numpy() & ~sp.isna().to_numpy() & ~np.isin(sp.index, full.index)
        comparable = (p_fin & f_fin) | (p_fin ^ f_fin)
        if missing.any():
            j = int(np.argmax(missing))
            bad = ProbeResult(
                k,
                sp.index[j],
                float(pv_arr[j]),
                np.nan,
                np.inf,
                False,
                "prefix emitted a timestamp the full run does not have",
            )
            results.append(bad)
            compared += 1
            first_bad = first_bad or bad
            continue
        if not comparable.any():
            # every aligned point is still warming up on both sides
            results.append(ProbeResult(k, ts, pv, np.nan, np.nan, True, "skipped: both NaN"))
            continue

        compared += 1
        deltas = np.full(pv_arr.shape, np.nan)
        both = p_fin & f_fin
        deltas[both] = np.abs(pv_arr[both] - fv_arr[both])
        deltas[p_fin ^ f_fin] = np.inf
        bad_mask = comparable & ~(deltas <= tol)
        if bad_mask.any():
            j = int(np.argmax(bad_mask))  # earliest offending timestamp
            delta = float(deltas[j])
            note = (
                "one side is NaN and the other is not"
                if not np.isfinite(delta)
                else "value rewritten by later data"
            )
            if j != len(pv_arr) - 1:
                note += f" (at prefix position {j} of {len(pv_arr) - 1}, not the last point)"
            res = ProbeResult(
                k, sp.index[j], float(pv_arr[j]), float(fv_arr[j]), delta, False, note
            )
        else:
            finite = deltas[np.isfinite(deltas)]
            res = ProbeResult(
                k,
                ts,
                pv,
                float(full.loc[ts]),
                float(finite.max()) if finite.size else 0.0,
                True,
                "",
            )
        results.append(res)
        if not res.ok and first_bad is None:
            first_bad = res

    if compared == 0:
        raise ValueError(
            "causality probe compared nothing: every prefix produced an empty or all-NaN "
            "forecast. Lengthen the sample, lower min_prefix_frac, or reduce the signal's "
            "warm-up. A vacuous pass would be worse than a failure."
        )

    return CausalReport(
        passed=first_bad is None,
        n_probes=len(probe_ks),
        n_compared=compared,
        tol=float(tol),
        probes=tuple(results),
        first_mismatch=first_bad,
    )


def assert_causal(
    forecast_fn: ForecastFn,
    bars: pd.DataFrame,
    n_probes: int = 8,
    tol: float = 1e-9,
    *,
    min_prefix_frac: float = 0.5,
    cuts: Sequence[int] | None = None,
) -> CausalReport:
    """Run :func:`check_causal` and raise :class:`LookaheadError` if any probe disagrees.

    The error message names the earliest disagreeing timestamp and the size of the difference.
    Returns the passing :class:`CausalReport`.
    """
    report = check_causal(
        forecast_fn, bars, n_probes=n_probes, tol=tol, min_prefix_frac=min_prefix_frac, cuts=cuts
    )
    if not report.passed:
        raise LookaheadError(report.summary())
    return report
