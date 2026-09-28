"""Signal layer: a vol-scaled, capped, horizon-tagged forecast, independent of position sizing.

Convention (pysystemtrade): sign is direction (0 = no view); magnitude is conviction, scaled
causally so the long-run mean of ``|forecast|`` is about 10; values are capped at ±20.
``horizon`` is a mandatory wall-clock ``pandas.Timedelta``, since the position fee accrues per
calendar day held (``historical/backtest_engine/costs.py``).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable, Literal, Sequence

import numpy as np
import pandas as pd

__all__ = [
    "DEFAULT_CAP",
    "DEFAULT_TARGET_ABS",
    "Forecast",
    "scale_and_cap",
    "combine",
    "to_position_target",
]

DEFAULT_CAP = 20.0
"""The pysystemtrade cap: 2x the target average absolute forecast."""

DEFAULT_TARGET_ABS = 10.0
"""Target long-run mean of ``|forecast|``, the common scale across signals."""

_CAP_TOL = 1e-9
"""Slack on the cap check, so a float round-trip of an exactly-capped value passes."""

_DAYS_PER_YEAR = 365.25


def _as_timedelta(horizon: object) -> pd.Timedelta:
    """Coerce ``horizon`` to a strictly positive :class:`pandas.Timedelta`."""
    if horizon is None:
        raise ValueError(
            "Forecast.horizon is mandatory: a forecast without a holding period is "
            "uninterpretable (see module docstring)."
        )
    try:
        td = pd.Timedelta(horizon)
    except (ValueError, TypeError) as exc:
        raise TypeError(
            f"Forecast.horizon must be coercible to pd.Timedelta, got {horizon!r}"
        ) from exc
    if pd.isna(td):
        raise ValueError("Forecast.horizon is NaT -- a missing horizon is not allowed.")
    if td <= pd.Timedelta(0):
        raise ValueError(f"Forecast.horizon must be strictly positive, got {td!r}.")
    return td


def _periods_per_year(index: pd.DatetimeIndex) -> float:
    """Observations per calendar year, counted over the index's realised span.

    Unlike ``365.25 / median spacing``, this gives ~250 (not ~365) on a daily trading grid.
    """
    if len(index) < 2:
        return float("nan")
    span_days = (index[-1] - index[0]) / pd.Timedelta(days=1)
    if span_days <= 0:
        return float("nan")
    return float(len(index) - 1) * _DAYS_PER_YEAR / span_days


@dataclass(frozen=True)
class Forecast:
    """A vol-scaled, capped, horizon-tagged view on one instrument.

    Attributes:
        values: float Series on a strictly increasing DatetimeIndex, stamped when the view forms.
        horizon: mandatory holding period (``pandas.Timedelta``).
        spec_id: identifier of the producing spec (e.g. ``"C7"``).
        causal: the producer's claim of no lookahead; ``assert_causal`` proves it.
        data_version: fingerprint or tag of the input data, for reproducibility.
        cap: symmetric bound on ``|values|``.

    Violations raise ``TypeError``/``ValueError``; :meth:`capped` (out-of-range values) and
    :meth:`from_raw` (raw units) build compliant instances.
    """

    values: pd.Series
    horizon: pd.Timedelta
    spec_id: str
    causal: bool = True
    data_version: str = ""
    cap: float = DEFAULT_CAP

    # -- construction / validation -------------------------------------------
    def __post_init__(self) -> None:
        if not isinstance(self.values, pd.Series):
            raise TypeError(f"Forecast.values must be a pd.Series, got {type(self.values)!r}")
        if not isinstance(self.spec_id, str) or not self.spec_id:
            raise ValueError("Forecast.spec_id must be a non-empty string.")

        cap = float(self.cap)
        if not np.isfinite(cap) or cap <= 0:
            raise ValueError(f"Forecast.cap must be finite and > 0, got {self.cap!r}")
        object.__setattr__(self, "cap", cap)
        object.__setattr__(self, "horizon", _as_timedelta(self.horizon))

        idx = self.values.index
        if not isinstance(idx, pd.DatetimeIndex):
            raise TypeError(
                "Forecast.values must have a DatetimeIndex -- the horizon is wall-clock "
                f"and needs a wall-clock index. Got {type(idx)!r}."
            )
        if len(idx) == 0:
            raise ValueError("Forecast.values is empty.")
        if not idx.is_monotonic_increasing:
            raise ValueError(
                "Forecast.values index must be monotonically increasing. An unsorted "
                "index silently breaks every rolling/causal estimate downstream."
            )
        if idx.has_duplicates:
            raise ValueError("Forecast.values index has duplicate timestamps.")

        vals = self.values.astype("float64")
        vals.name = self.spec_id
        object.__setattr__(self, "values", vals)

        arr = vals.to_numpy()
        finite = np.isfinite(arr)
        if not finite.any():
            raise ValueError(
                "Forecast.values is all-NaN/non-finite: there is no forecast here. "
                "A warm-up prefix of NaN is fine; an entirely empty series is not."
            )
        worst = float(np.max(np.abs(arr[finite])))
        if worst > cap + _CAP_TOL:
            raise ValueError(
                f"Forecast.values breaches the cap: max|f| = {worst:.6f} > cap = {cap}. "
                "Un-capped forecasts let one outlier dominate the whole book's sizing. "
                "Build it with Forecast.capped(...) or scale it with scale_and_cap(...)."
            )

    @classmethod
    def capped(
        cls,
        values: pd.Series,
        horizon: object,
        spec_id: str,
        *,
        causal: bool = True,
        data_version: str = "",
        cap: float = DEFAULT_CAP,
    ) -> "Forecast":
        """Build a compliant forecast by clipping ``values`` to ±``cap``.

        For a series already in forecast units; raw units need :meth:`from_raw`, since clipping
        does not rescale.
        """
        cap = float(cap)
        return cls(
            values=pd.Series(values).astype("float64").clip(-cap, cap),
            horizon=horizon,
            spec_id=spec_id,
            causal=causal,
            data_version=data_version,
            cap=cap,
        )

    @classmethod
    def from_raw(
        cls,
        raw: pd.Series,
        horizon: object,
        spec_id: str,
        *,
        target_abs: float = DEFAULT_TARGET_ABS,
        cap: float = DEFAULT_CAP,
        method: Literal["expanding", "rolling"] = "expanding",
        window: int | None = None,
        min_periods: int = 20,
        ignore_zeros: bool = False,
        smooth_span: int | None = None,
        causal: bool = True,
        data_version: str = "",
    ) -> "Forecast":
        """Scale a raw signal causally with :func:`scale_and_cap`, then wrap it.

        The usual entry point for a new signal in its natural units (points, z-score, ratio).
        """
        scaled = scale_and_cap(
            raw,
            target_abs=target_abs,
            cap=cap,
            method=method,
            window=window,
            min_periods=min_periods,
            ignore_zeros=ignore_zeros,
            smooth_span=smooth_span,
        )
        return cls(
            values=scaled,
            horizon=horizon,
            spec_id=spec_id,
            causal=causal,
            data_version=data_version,
            cap=float(cap),
        )

    # -- read-only views ------------------------------------------------------
    def __len__(self) -> int:
        return int(len(self.values))

    @property
    def index(self) -> pd.DatetimeIndex:
        return self.values.index  # type: ignore[return-value]

    @property
    def horizon_days(self) -> float:
        """Horizon in fractional calendar days."""
        return float(self.horizon / pd.Timedelta(days=1))

    @property
    def hold_days(self) -> int:
        """Whole calendar days in the horizon (0 = intraday, 1 = one overnight).

        The ``hold_days`` argument of ``costs.round_trip_fee_points``.
        """
        return int(max(0, int(np.floor(self.horizon_days + 1e-9))))

    def periods_per_year(self) -> float:
        """Observations per calendar year on this forecast's own index."""
        return _periods_per_year(self.index)

    def horizon_in_bars(self) -> float:
        """Horizon expressed in bars of this index, derived from its observation rate."""
        ppy = self.periods_per_year()
        if not np.isfinite(ppy):
            return float("nan")
        return self.horizon_days * ppy / _DAYS_PER_YEAR

    def active(self) -> pd.Series:
        """Boolean mask of bars where a view exists (finite and non-zero)."""
        arr = self.values.to_numpy()
        return pd.Series(np.isfinite(arr) & (arr != 0.0), index=self.index)

    def avg_abs(self, *, conditional: bool = False) -> float:
        """Realised mean of ``|forecast|`` over finite bars (target about 10).

        ``conditional=True`` averages over active bars only, the relevant reading for a sparse
        event signal that is flat most of the time.
        """
        arr = self.values.to_numpy()
        mask = np.isfinite(arr)
        if conditional:
            mask = mask & (arr != 0.0)
        if not mask.any():
            return float("nan")
        return float(np.mean(np.abs(arr[mask])))

    def fingerprint(self) -> dict:
        """Return metadata and a SHA-1 of values and index, so a metric can name its input."""
        arr = np.ascontiguousarray(self.values.to_numpy(dtype="float64"))
        idx = np.ascontiguousarray(self.index.asi8)
        h = hashlib.sha1()
        h.update(arr.tobytes())
        h.update(idx.tobytes())
        return {
            "spec_id": self.spec_id,
            "n": len(self),
            "first": self.index[0].isoformat(),
            "last": self.index[-1].isoformat(),
            "horizon": str(self.horizon),
            "cap": self.cap,
            "causal_claimed": bool(self.causal),
            "data_version": self.data_version,
            "sha1": h.hexdigest(),
        }


def scale_and_cap(
    raw: pd.Series,
    target_abs: float = DEFAULT_TARGET_ABS,
    cap: float = DEFAULT_CAP,
    *,
    method: Literal["expanding", "rolling"] = "expanding",
    window: int | None = None,
    min_periods: int = 20,
    ignore_zeros: bool = False,
    smooth_span: int | None = None,
) -> pd.Series:
    """Scale a raw signal to forecast units with a causal scalar, then cap it::

        scalar_t   = target_abs / mean(|raw|) over observations up to and including t
        forecast_t = clip(raw_t * scalar_t, -cap, +cap)

    ``raw`` must already carry the trading direction in its sign. The output is NaN during
    warm-up (``min_periods``) and passes ``historical.signal_eval.causal.assert_causal``.

    Args:
        method: ``"expanding"`` (all history) or ``"rolling"`` (last ``window`` observations).
        ignore_zeros: estimate ``mean(|raw|)`` from non-zero values only (sparse event signals).
        smooth_span: optional EWM span applied to ``mean(|raw|)`` to damp scalar-driven turnover.
    """
    s = pd.Series(raw).astype("float64")
    target_abs = float(target_abs)
    cap = float(cap)
    if target_abs <= 0:
        raise ValueError("target_abs must be > 0.")
    if cap <= 0:
        raise ValueError("cap must be > 0.")
    if min_periods < 1:
        raise ValueError("min_periods must be >= 1.")

    absr = s.abs()
    if ignore_zeros:
        absr = absr.where(absr > 0.0)

    if method == "expanding":
        mean_abs = absr.expanding(min_periods=min_periods).mean()
    elif method == "rolling":
        if not window or window < min_periods:
            raise ValueError("method='rolling' needs window >= min_periods.")
        mean_abs = absr.rolling(window, min_periods=min_periods).mean()
    else:
        raise ValueError(f"unknown method {method!r}; use 'expanding' or 'rolling'.")

    if smooth_span:
        mean_abs = mean_abs.ewm(span=int(smooth_span), min_periods=min_periods).mean()

    # A zero/negative/NaN mean has no scalar; emit NaN rather than an infinity.
    mean_abs = mean_abs.where(mean_abs > 0.0)
    scalar = target_abs / mean_abs
    return (s * scalar).clip(-cap, cap).rename(s.name)


def combine(
    forecasts: Sequence[Forecast] | Iterable[Forecast],
    weights: Sequence[float] | None = None,
    div_multiplier: float = 1.0,
    *,
    spec_id: str | None = None,
    horizon: object | None = None,
    cap: float | None = None,
) -> Forecast:
    """Blend forecasts by weighted average, apply ``div_multiplier`` and re-cap::

        combined_t = clip(div_multiplier * sum_i w_i * f_i,t, -cap, +cap)

    ``weights`` are non-negative, equal by default, and normalised to sum to 1. Inputs are
    outer-joined; a missing sleeve's weight is redistributed over the sleeves present, and a
    timestamp with no sleeve stays NaN. ``div_multiplier`` (forecast diversification multiplier)
    offsets the shrinkage of ``|forecast|`` from blending imperfectly correlated inputs; the
    caller estimates it causally. ``horizon`` and ``cap`` are required when the inputs differ.
    """
    fcs = list(forecasts)
    if not fcs:
        raise ValueError("combine() needs at least one Forecast.")
    if any(not isinstance(f, Forecast) for f in fcs):
        raise TypeError("combine() takes Forecast objects.")

    if horizon is None:
        horizons = {f.horizon for f in fcs}
        if len(horizons) != 1:
            raise ValueError(
                f"combine() got mixed horizons {sorted(str(h) for h in horizons)}; a blend "
                "of different holding periods has no defined horizon -- pass horizon=... "
                "explicitly to state what you mean."
            )
        horizon = fcs[0].horizon
    if cap is None:
        caps = {f.cap for f in fcs}
        if len(caps) != 1:
            raise ValueError(f"combine() got mixed caps {sorted(caps)}; pass cap=... explicitly.")
        cap = fcs[0].cap
    cap = float(cap)

    if weights is None:
        w = np.full(len(fcs), 1.0 / len(fcs))
    else:
        w = np.asarray(list(weights), dtype="float64")
        if w.shape != (len(fcs),):
            raise ValueError(f"weights must have length {len(fcs)}, got {w.shape}.")
        if np.any(w < 0) or not np.isfinite(w).all():
            raise ValueError("weights must be finite and non-negative.")
        total = float(w.sum())
        if total <= 0:
            raise ValueError("weights must sum to > 0.")
        w = w / total

    frame = pd.concat([f.values for f in fcs], axis=1)
    frame.columns = pd.RangeIndex(len(fcs))
    vals = frame.to_numpy(dtype="float64")
    present = np.isfinite(vals)
    wm = present * w[None, :]
    denom = wm.sum(axis=1)
    num = (np.where(present, vals, 0.0) * wm).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        blended = np.where(denom > 0, num / denom, np.nan)

    out = pd.Series(blended * float(div_multiplier), index=frame.index)
    name = spec_id or "combo(" + "+".join(f.spec_id for f in fcs) + ")"
    versions = sorted({f.data_version for f in fcs if f.data_version})
    return Forecast.capped(
        out,
        horizon=horizon,
        spec_id=name,
        causal=all(f.causal for f in fcs),
        data_version=";".join(versions),
        cap=cap,
    )


def to_position_target(fc: Forecast, capital_contracts: int) -> pd.Series:
    """Map a forecast to a signed target position in contracts: ``capital_contracts * f_t / cap``.

    A forecast at the cap requests the full ``capital_contracts`` and 0 requests flat; NaN
    (warm-up) stays NaN, since no view yet is not a flat view. The result is fractional:
    rounding, volatility targeting, margin and position limits are left to the caller.
    """
    if capital_contracts <= 0:
        raise ValueError("capital_contracts must be > 0.")
    return (fc.values * (float(capital_contracts) / fc.cap)).rename(f"{fc.spec_id}_target")
