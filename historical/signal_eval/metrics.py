"""Signal metrics for a :class:`~shared.models.forecast.Forecast`, computed without a backtest.

Information coefficient, IC decay and half-life, IC stability, turnover, trading cost in Sharpe
units and quantile returns. Returns and costs are in index points, not percent. Costs come
from ``historical/backtest_engine/costs.py`` at the signal's own price level, because the fee
depends on price.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, Literal, Sequence

import numpy as np
import pandas as pd

from historical.backtest_engine.costs import (
    DEFAULT_SCENARIO,
    CostScenario,
    round_trip_cost,
    round_trip_fee_points,
)
from shared.models.forecast import Forecast

__all__ = [
    "forward_returns",
    "information_coefficient",
    "ic_by_period",
    "ic_decay",
    "information_ratio",
    "turnover_per_year",
    "cost_in_sharpe_units",
    "quantile_returns",
    "evaluate",
]

_DAYS_PER_YEAR = 365.25


# --- helpers ----------------------------------------------------------------
def _jf(x: object) -> float | None:
    """JSON-safe float: ``None`` for non-finite or non-numeric input (``NaN`` is not JSON)."""
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _periods_per_year(index: pd.DatetimeIndex) -> float:
    """Observations per calendar year measured over the index's realised span."""
    if len(index) < 2:
        return float("nan")
    span_days = (index[-1] - index[0]) / pd.Timedelta(days=1)
    if span_days <= 0:
        return float("nan")
    return float(len(index) - 1) * _DAYS_PER_YEAR / span_days


def _series_fingerprint(s: pd.Series, label: str) -> dict:
    """Length, first and last timestamp, and SHA-1 of the values and index of ``s``."""
    arr = np.ascontiguousarray(s.to_numpy(dtype="float64"))
    idx = np.ascontiguousarray(pd.DatetimeIndex(s.index).asi8)
    h = hashlib.sha1()
    h.update(arr.tobytes())
    h.update(idx.tobytes())
    return {
        "label": label,
        "n": int(len(s)),
        "first": pd.Timestamp(s.index[0]).isoformat(),
        "last": pd.Timestamp(s.index[-1]).isoformat(),
        "sha1": h.hexdigest(),
    }


def _aligned(fc_values: pd.Series, other: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    """Inner-join two series on their index and drop any pair with a NaN."""
    frame = pd.concat([fc_values.rename("f"), pd.Series(other).rename("r")], axis=1, join="inner")
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna()
    return frame["f"].to_numpy(dtype="float64"), frame["r"].to_numpy(dtype="float64")


def _rank_corr(a: np.ndarray, b: np.ndarray, method: str) -> float:
    """Spearman or Pearson correlation; NaN for fewer than 3 pairs or a constant input."""
    if a.size < 3:
        return float("nan")
    if method == "spearman":
        a = pd.Series(a).rank().to_numpy()
        b = pd.Series(b).rank().to_numpy()
    elif method != "pearson":
        raise ValueError(f"unknown method {method!r}; use 'spearman' or 'pearson'.")
    if np.std(a) == 0.0 or np.std(b) == 0.0:
        # a constant series carries no rank information
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


# --- forward returns --------------------------------------------------------
def forward_returns(prices: pd.Series, lag: int = 1) -> pd.Series:
    """Forward price change in index points, ``price[t+lag] - price[t]``, stored at ``t``.

    It aligns with the forecast made at ``t``; the last ``lag`` values are NaN. This is the
    prediction target, so the forward shift is intended.
    """
    if lag < 1:
        raise ValueError("lag must be >= 1.")
    p = pd.Series(prices).astype("float64")
    return (p.shift(-lag) - p).rename(f"fwd_{lag}")


# --- information coefficient ------------------------------------------------
def information_coefficient(
    fc: Forecast | pd.Series,
    fwd_returns: pd.Series,
    method: Literal["spearman", "pearson"] = "spearman",
) -> float:
    """Correlation (IC) between the forecast and the forward return it predicts.

    Spearman by default: a rank IC resists fat-tailed outliers and is unchanged by positive
    rescaling of the forecast. Returns a value in [-1, 1], or NaN with fewer than 3 aligned
    pairs or a constant input.
    """
    values = fc.values if isinstance(fc, Forecast) else pd.Series(fc)
    f, r = _aligned(values, fwd_returns)
    return _rank_corr(f, r, method)


def ic_by_period(
    fc: Forecast | pd.Series,
    fwd_returns: pd.Series,
    period: str = "ME",
    method: Literal["spearman", "pearson"] = "spearman",
    min_obs: int = 5,
) -> pd.Series:
    """IC per resampling period (monthly by default); skips periods under ``min_obs`` pairs."""
    values = fc.values if isinstance(fc, Forecast) else pd.Series(fc)
    frame = pd.concat(
        [values.rename("f"), pd.Series(fwd_returns).rename("r")], axis=1, join="inner"
    )
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna()
    if frame.empty:
        return pd.Series(dtype="float64")
    out: dict[pd.Timestamp, float] = {}
    for stamp, chunk in frame.groupby(pd.Grouper(freq=period)):
        if len(chunk) < min_obs:
            continue
        out[stamp] = _rank_corr(
            chunk["f"].to_numpy(dtype="float64"), chunk["r"].to_numpy(dtype="float64"), method
        )
    return pd.Series(out, dtype="float64").dropna()


def information_ratio(
    fc: Forecast | pd.Series,
    fwd_returns: pd.Series,
    period: str = "ME",
    method: Literal["spearman", "pearson"] = "spearman",
    min_obs: int = 5,
) -> float:
    """IC stability: ``mean(IC) / std(IC)`` across resampling periods.

    Separates an effect that recurs from one strong period; with few periods it is noisy, so
    read it with the number of periods. Returns NaN with fewer than 2 periods or zero IC
    variance.
    """
    ics = ic_by_period(fc, fwd_returns, period=period, method=method, min_obs=min_obs)
    if len(ics) < 2:
        return float("nan")
    sd = float(ics.std(ddof=1))
    if not np.isfinite(sd) or sd == 0.0:
        return float("nan")
    return float(ics.mean()) / sd


def ic_decay(
    fc: Forecast | pd.Series,
    prices: pd.Series,
    lags: Sequence[int] = (1, 5, 21),
    method: Literal["spearman", "pearson"] = "spearman",
) -> dict[str, Any]:
    """IC at several forward horizons (in bars) and an exponential half-life.

    The half-life fits ``|IC(l)| ∝ exp(-lambda * l)`` by least squares on ``log|IC|`` and
    equals ``ln(2) / lambda``; it is None with fewer than two non-zero ICs or no resolvable
    decay. Returns ``{"method", "lags", "ic": {"<lag>": ic}, "half_life_bars"}``.
    """
    lags = tuple(int(x) for x in lags)
    if not lags or any(x < 1 for x in lags):
        raise ValueError("lags must be a non-empty sequence of positive ints.")
    values = fc.values if isinstance(fc, Forecast) else pd.Series(fc)
    ics = {
        lag: information_coefficient(values, forward_returns(prices, lag), method) for lag in lags
    }

    usable = [(lag, abs(v)) for lag, v in ics.items() if np.isfinite(v) and abs(v) > 1e-12]
    half_life: float | None = None
    if len(usable) >= 2:
        x = np.array([lag for lag, _ in usable], dtype="float64")
        y = np.log(np.array([v for _, v in usable], dtype="float64"))
        slope = float(np.polyfit(x, y, 1)[0])
        lam = -slope
        # A flat IC can fit a slope of ~1e-17 rather than 0; require a decay that is
        # resolvable against the fit's scale before reporting a half-life.
        scale = float(np.max(np.abs(y))) if y.size else 0.0
        lam_floor = max(1e-9, 1e-9 * max(scale, 1.0))
        if lam > lam_floor:
            half_life = float(math.log(2.0) / lam)

    return {
        "method": method,
        "lags": list(lags),
        "ic": {str(lag): _jf(v) for lag, v in ics.items()},
        "half_life_bars": _jf(half_life) if half_life is not None else None,
    }


# --- turnover ---------------------------------------------------------------
def turnover_per_year(fc: Forecast) -> float:
    """Annual turnover implied by the forecast, in units of full position.

    With position ``p_t = f_t / cap`` in [-1, 1]::

        turnover_per_year = periods_per_year * mean(|p_t - p_{t-1}|)

    A full round trip is two units. Measured before any buffering or rounding, so it bounds
    actual trading from above. Warm-up NaNs are excluded, not treated as a move from flat.
    """
    p = fc.values / fc.cap
    delta = p.diff().abs()
    delta = delta[np.isfinite(delta.to_numpy())]
    if delta.empty:
        return float("nan")
    ppy = _periods_per_year(pd.DatetimeIndex(fc.index))
    if not np.isfinite(ppy):
        return float("nan")
    return float(delta.mean() * ppy)


# --- cost in Sharpe units ---------------------------------------------------
def cost_in_sharpe_units(
    fc: Forecast,
    prices: pd.Series,
    cost_per_round_trip_points: float | None = None,
    *,
    scenario: CostScenario = DEFAULT_SCENARIO,
    price_for_cost: float | None = None,
) -> dict[str, Any]:
    """Annual trading cost of the forecast in Sharpe units (Carver, *Systematic Trading*, 2015).

    With position ``p_t = f_t / cap``::

        C       = turnover_per_year(fc) / 2 * cost_rt        (points per year)
        pnl_t   = p_{t-1} * (price_t - price_{t-1})
        sr_cost = C / (std(pnl) * sqrt(periods_per_year))

    ``sr_gross`` uses the same P&L, so ``sr_net = sr_gross - sr_cost``. ``cost_rt`` is
    ``costs.round_trip_cost`` for ``scenario`` at ``price_for_cost`` (default: median price)
    and ``fc.hold_days`` unless ``cost_per_round_trip_points`` is given. Returns the three
    Sharpes and every intermediate; raises ``ValueError`` below 3 aligned observations.
    """
    p = pd.Series(prices).astype("float64")
    joined = pd.concat([fc.values.rename("f"), p.rename("px")], axis=1, join="inner").dropna()
    if len(joined) < 3:
        raise ValueError("cost_in_sharpe_units needs >= 3 aligned forecast/price observations.")

    px_level = float(price_for_cost) if price_for_cost is not None else float(joined["px"].median())
    fee_pts = round_trip_fee_points(px_level, qty=1, hold_days=fc.hold_days)
    if cost_per_round_trip_points is None:
        cost_rt = round_trip_cost(px_level, hold_days=fc.hold_days, scenario=scenario, qty=1)
        cost_source = f"costs.round_trip_cost(price={px_level:.2f}, scenario={scenario.value.name})"
    else:
        cost_rt = float(cost_per_round_trip_points)
        cost_source = "caller-supplied override"

    turnover = turnover_per_year(fc)
    round_trips = turnover / 2.0 if np.isfinite(turnover) else float("nan")
    annual_cost_pts = round_trips * cost_rt

    pos = (joined["f"] / fc.cap).shift(1)
    pnl = pos * joined["px"].diff()
    pnl = pnl.replace([np.inf, -np.inf], np.nan).dropna()
    ppy = _periods_per_year(pd.DatetimeIndex(joined.index))
    sd = float(pnl.std(ddof=1)) if len(pnl) > 1 else float("nan")
    sigma_annual = sd * math.sqrt(ppy) if np.isfinite(sd) and np.isfinite(ppy) else float("nan")

    sr_cost = (
        annual_cost_pts / sigma_annual
        if np.isfinite(annual_cost_pts) and np.isfinite(sigma_annual) and sigma_annual > 0
        else float("nan")
    )
    mean_annual = float(pnl.mean()) * ppy if np.isfinite(ppy) and len(pnl) > 1 else float("nan")
    sr_gross = (
        mean_annual / sigma_annual
        if np.isfinite(mean_annual) and np.isfinite(sigma_annual) and sigma_annual > 0
        else float("nan")
    )

    return {
        "sr_cost": _jf(sr_cost),
        "sr_gross": _jf(sr_gross),
        "sr_net": (
            _jf(sr_gross - sr_cost) if np.isfinite(sr_gross) and np.isfinite(sr_cost) else None
        ),
        "cost_round_trip_points": _jf(cost_rt),
        "fee_only_round_trip_points": _jf(fee_pts),
        "cost_source": cost_source,
        "scenario": scenario.value.name,
        "price_for_cost": _jf(px_level),
        "hold_days": int(fc.hold_days),
        "turnover_per_year": _jf(turnover),
        "round_trips_per_year": _jf(round_trips),
        "annual_cost_points": _jf(annual_cost_pts),
        "annual_vol_points": _jf(sigma_annual),
        "periods_per_year": _jf(ppy),
        "n_pnl_obs": int(len(pnl)),
    }


# --- quantiles --------------------------------------------------------------
def quantile_returns(
    fc: Forecast | pd.Series,
    fwd_returns: pd.Series,
    n: int = 5,
) -> pd.DataFrame:
    """Forward-return statistics per forecast quantile bucket, to check the response is monotone.

    Buckets use quantiles of the whole evaluated span, so the table is an in-sample
    description; it never feeds back into the signal. Returns a frame indexed by ``bucket``
    (1 = lowest forecast) with columns ``n``, ``mean``, ``median``, ``fc_min``, ``fc_max``.
    """
    if n < 2:
        raise ValueError("n must be >= 2.")
    values = fc.values if isinstance(fc, Forecast) else pd.Series(fc)
    frame = pd.concat(
        [values.rename("f"), pd.Series(fwd_returns).rename("r")], axis=1, join="inner"
    )
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna()
    if len(frame) < n:
        return pd.DataFrame(columns=["n", "mean", "median", "fc_min", "fc_max"])
    # a sparse signal has many exact zeros, so edges can coincide and buckets merge
    buckets = pd.qcut(frame["f"], n, labels=False, duplicates="drop") + 1
    grouped = frame.groupby(buckets)
    out = pd.DataFrame(
        {
            "n": grouped["r"].size(),
            "mean": grouped["r"].mean(),
            "median": grouped["r"].median(),
            "fc_min": grouped["f"].min(),
            "fc_max": grouped["f"].max(),
        }
    )
    out.index.name = "bucket"
    return out


# --- all metrics ------------------------------------------------------------
def evaluate(
    fc: Forecast,
    prices: pd.Series,
    cost_points: float | None = None,
    *,
    fwd: pd.Series | None = None,
    lags: Sequence[int] = (1, 5, 21),
    n_quantiles: int = 5,
    ic_method: Literal["spearman", "pearson"] = "spearman",
    ic_period: str = "ME",
    scenario: CostScenario = DEFAULT_SCENARIO,
    price_for_cost: float | None = None,
) -> dict[str, Any]:
    """Run every metric in this module on ``fc`` and return one JSON-serialisable dict.

    Keys: ``forecast``, ``ic``, ``decay``, ``turnover``, ``cost``, ``quantiles`` and ``inputs``
    (fingerprints of the forecast, prices and forward returns). ``prices`` are in index points;
    ``cost_points`` overrides the cost model. ``fwd`` defaults to the one-bar close-to-close
    change; pass the traded return when a design does not trade close to close (for example
    ``historical.signals.forecast_c7``, which trades the next day's open-to-close move).
    """
    if not isinstance(fc, Forecast):
        raise TypeError("evaluate() takes a Forecast; build one first.")
    px = pd.Series(prices).astype("float64")
    fwd_1 = forward_returns(px, 1) if fwd is None else pd.Series(fwd).astype("float64")

    ic = information_coefficient(fc, fwd_1, ic_method)
    ir = information_ratio(fc, fwd_1, period=ic_period, method=ic_method)
    ics = ic_by_period(fc, fwd_1, period=ic_period, method=ic_method)
    decay = ic_decay(fc, px, lags=lags, method=ic_method)
    cost = cost_in_sharpe_units(
        fc, px, cost_points, scenario=scenario, price_for_cost=price_for_cost
    )
    qt = quantile_returns(fc, fwd_1, n=n_quantiles)

    n_aligned = int(
        pd.concat([fc.values.rename("f"), fwd_1.rename("r")], axis=1, join="inner")
        .dropna()
        .shape[0]
    )

    return {
        "forecast": {
            "spec_id": fc.spec_id,
            "horizon": str(fc.horizon),
            "horizon_days": _jf(fc.horizon_days),
            "hold_days": int(fc.hold_days),
            "cap": _jf(fc.cap),
            "causal_claimed": bool(fc.causal),
            "n": len(fc),
            "n_active": int(fc.active().sum()),
            "avg_abs": _jf(fc.avg_abs()),
            "avg_abs_active": _jf(fc.avg_abs(conditional=True)),
            "periods_per_year": _jf(fc.periods_per_year()),
        },
        "ic": {
            "method": ic_method,
            "ic": _jf(ic),
            "n_obs": n_aligned,
            "information_ratio": _jf(ir),
            "ic_period": ic_period,
            "n_periods": int(len(ics)),
            "ic_period_mean": _jf(ics.mean()) if len(ics) else None,
            "ic_period_std": _jf(ics.std(ddof=1)) if len(ics) > 1 else None,
            "fwd_source": "caller-supplied" if fwd is not None else "close-to-close lag 1",
        },
        "decay": decay,
        "turnover": {
            "turnover_per_year": _jf(turnover_per_year(fc)),
            "definition": "annualised mean |delta(position)|, position = forecast/cap",
        },
        "cost": cost,
        "quantiles": [
            {
                "bucket": int(b),
                "n": int(row["n"]),
                "mean": _jf(row["mean"]),
                "median": _jf(row["median"]),
                "fc_min": _jf(row["fc_min"]),
                "fc_max": _jf(row["fc_max"]),
            }
            for b, row in qt.iterrows()
        ],
        "inputs": {
            "forecast": fc.fingerprint(),
            "prices": _series_fingerprint(px, "prices"),
            "forward_returns": _series_fingerprint(fwd_1, "forward_returns"),
        },
    }
