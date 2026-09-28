"""C7 short-term reversal re-expressed as a :class:`Forecast` for signal evaluation.

``research_lab.forward_registry.c7_plans`` remains the card's scoring authority. The signal
mirrors ``lab.reversal_daily_signal``: ``ret = close.diff()`` in points; a session fires when
``|ret|`` reaches the 0.80 quantile of the prior 60 sessions and forecasts ``-sign(ret)``.
Magnitude is ``|ret| / thresh`` (``magnitude="ratio"``) or direction only (``"binary"``, the
card's own content). Not modelled: the 1.0 × ATR(14) stop, the holdout cutoff, fills and fees
(costs apply at evaluation). The card trades the D+1 open-to-close move; score it with
:func:`c7_forward_returns`.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd

from shared.models.forecast import DEFAULT_CAP, DEFAULT_TARGET_ABS, Forecast

__all__ = [
    "C7_LOOKBACK",
    "C7_PCT",
    "C7_HORIZON",
    "c7_signal_frame",
    "c7_forecast",
    "c7_forward_returns",
    "c7_prices",
]

C7_LOOKBACK = 60
"""Trailing sessions for the threshold quantile (frozen by the card)."""

C7_PCT = 0.80
"""Threshold quantile (frozen by the card)."""

C7_HORIZON = pd.Timedelta(days=1)
"""One session: enter at the D+1 open, exit at the D+1 close (frozen by the card)."""


def _daily_index(daily: pd.DataFrame) -> pd.DatetimeIndex:
    """Return the frame's dates as an unnamed DatetimeIndex.

    Accepts a DatetimeIndex (``research_lab.daily.daily_bars``), a ``dt`` column
    (``lab.daily_bars``) or a date-like index; dropping the name makes both shapes compare equal.
    """
    if isinstance(daily.index, pd.DatetimeIndex):
        return daily.index.rename(None)
    if "dt" in daily.columns:
        return pd.DatetimeIndex(pd.to_datetime(daily["dt"])).rename(None)
    try:
        return pd.DatetimeIndex(pd.to_datetime(daily.index)).rename(None)
    except (TypeError, ValueError) as exc:
        raise TypeError(
            "daily frame needs a DatetimeIndex, a 'dt' column, or a date-like index."
        ) from exc


def c7_signal_frame(
    daily: pd.DataFrame,
    lookback: int = C7_LOOKBACK,
    pct: float = C7_PCT,
) -> pd.DataFrame:
    """Return the C7 signal intermediates; mirrors ``lab.reversal_daily_signal``.

    Columns: the input OHLC plus ``ret``, ``absr``, ``thresh``, ``fire`` and ``fade_dir``
    (+1 long, -1 short, 0 none), on a DatetimeIndex.
    """
    required = {"open", "high", "low", "close"}
    missing = required - set(daily.columns)
    if missing:
        raise KeyError(f"daily frame is missing columns {sorted(missing)}.")

    idx = _daily_index(daily)
    d = pd.DataFrame(
        {c: pd.Series(daily[c].to_numpy(), index=idx, dtype="float64") for c in sorted(required)}
    )
    if not d.index.is_monotonic_increasing:
        raise ValueError("daily frame must be sorted ascending by date.")

    # reuse a caller-supplied ret column so the two cannot disagree
    if "ret" in daily.columns:
        d["ret"] = pd.Series(daily["ret"].to_numpy(), index=idx, dtype="float64")
    else:
        d["ret"] = d["close"].diff()

    d["absr"] = d["ret"].abs()
    # quantile of prior bars only (shift 1): no look-ahead
    d["thresh"] = d["absr"].rolling(lookback).quantile(pct).shift(1)
    d["fire"] = (d["absr"] >= d["thresh"]).fillna(False)
    raw_dir = -np.sign(d["ret"].to_numpy())
    d["fade_dir"] = np.where(d["fire"].to_numpy() & np.isfinite(raw_dir), raw_dir, 0.0).astype(int)
    return d


def c7_forecast(
    daily: pd.DataFrame,
    lookback: int = C7_LOOKBACK,
    pct: float = C7_PCT,
    *,
    magnitude: Literal["ratio", "binary"] = "ratio",
    target_abs: float = DEFAULT_TARGET_ABS,
    cap: float = DEFAULT_CAP,
    min_periods: int = 20,
    spec_id: str = "C7",
    data_version: str = "",
) -> Forecast:
    """Return C7 as a +/-20 forecast formed at the D close, horizon one session.

    ``forecast_D = scale_and_cap(fade_dir_D * |ret_D| / thresh_D)``, or ``fade_dir_D`` alone with
    ``magnitude="binary"``. Scaling is causal-expanding with ``ignore_zeros=True``, so conviction
    is normalised over firing days only (C7 fires on about 20 % of sessions). Non-firing days
    are 0; the warm-up before ``min_periods`` firing observations is NaN. ``lookback`` and ``pct``
    are frozen card parameters, exposed only for tests.
    """
    sig = c7_signal_frame(daily, lookback=lookback, pct=pct)

    if magnitude == "binary":
        raw = pd.Series(sig["fade_dir"].to_numpy(dtype="float64"), index=sig.index)
    elif magnitude == "ratio":
        thresh = sig["thresh"].to_numpy()
        absr = sig["absr"].to_numpy()
        with np.errstate(invalid="ignore", divide="ignore"):
            ratio = np.where(thresh > 0.0, absr / thresh, np.nan)
        vals = sig["fade_dir"].to_numpy(dtype="float64") * ratio
        # a non-firing day is a flat view (0.0), not a missing one: turnover must see the exit
        vals = np.where(sig["fade_dir"].to_numpy() == 0, 0.0, vals)
        raw = pd.Series(vals, index=sig.index)
    else:
        raise ValueError(f"magnitude must be 'ratio' or 'binary', got {magnitude!r}.")

    return Forecast.from_raw(
        raw,
        horizon=C7_HORIZON,
        spec_id=spec_id,
        target_abs=target_abs,
        cap=cap,
        method="expanding",
        min_periods=min_periods,
        ignore_zeros=True,
        causal=True,
        data_version=data_version,
    )


def c7_forward_returns(daily: pd.DataFrame) -> pd.Series:
    """Return the move C7 trades, ``close(D+1) - open(D+1)`` in points, stamped at D.

    Pass it as ``evaluate(..., fwd=c7_forward_returns(daily))``: the close-to-close default adds
    an overnight leg the card never holds. The ATR stop is not applied.
    """
    idx = _daily_index(daily)
    o = pd.Series(daily["open"].to_numpy(), index=idx, dtype="float64")
    c = pd.Series(daily["close"].to_numpy(), index=idx, dtype="float64")
    return (c.shift(-1) - o.shift(-1)).rename("c7_fwd_open_to_close")


def c7_prices(daily: pd.DataFrame) -> pd.Series:
    """Return the daily close on a DatetimeIndex, the price series ``evaluate`` expects.

    It drives the decay curve and the price level at which the price-dependent fee is evaluated.
    """
    idx = _daily_index(daily)
    return pd.Series(daily["close"].to_numpy(), index=idx, dtype="float64").rename("close")
