"""Trading-day and intraday time grids for the synthetic VN30F1M market.

Trading days come from ``historical/research_lab/calendar_vn.py`` and expiries from
``daily.third_thursday``, the same calendar the research code uses.

Session structure (HNX derivatives, KRX era):
  08:45-09:00  opening periodic auction (ATO/LO orders) -> first print at 09:00
  09:00-11:30  continuous (morning, 150 minutes)
  11:30-13:00  lunch break
  13:00-14:30  continuous (afternoon, 90 minutes)
  14:30-14:45  closing periodic auction (ATC)
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from historical.research_lab import calendar_vn
from historical.research_lab.daily import third_thursday

TZ = "Asia/Ho_Chi_Minh"
MORNING = (dt.time(9, 0), dt.time(11, 30))
AFTERNOON = (dt.time(13, 0), dt.time(14, 30))


def trading_days(start: dt.date, n_days: int) -> list[dt.date]:
    """The first ``n_days`` VN30F1M trading days on or after ``start`` (real holiday calendar)."""
    horizon = start + dt.timedelta(days=int(n_days * 1.6) + 30)
    days = list(calendar_vn.trading_days(start, horizon))
    if len(days) < n_days:
        raise ValueError(f"calendar covers only {len(days)} trading days after {start}")
    return days[:n_days]


def expiry_days(days: list[dt.date]) -> set[dt.date]:
    """Effective expiry per month: the third Thursday, or the last trading day before it."""
    last = max(days)
    out: set[dt.date] = set()
    for y, m in sorted({(d.year, d.month) for d in days}):
        nominal = third_thursday(y, m).date()
        if nominal > last:
            continue  # expiry after the sample ends: the last sample day is not an expiry
        eff = nominal if calendar_vn.is_trading_day(nominal) else calendar_vn.previous_trading_day(nominal)
        if eff >= min(days):
            out.add(eff)
    return out


def minute_grid(day: dt.date) -> pd.DatetimeIndex:
    """Bar-open timestamps of the 240 continuous M1 bars of one session (tz-aware)."""
    parts = []
    for start, end in (MORNING, AFTERNOON):
        s = pd.Timestamp.combine(day, start).tz_localize(TZ)
        e = pd.Timestamp.combine(day, end).tz_localize(TZ)
        parts.append(pd.date_range(s, e, freq="1min", inclusive="left"))
    return parts[0].append(parts[1])


def intraday_vol_profile(n: int) -> np.ndarray:
    """U-shaped per-minute volatility multiplier (mean 1): busy open/close, quiet midday."""
    x = np.linspace(-1.0, 1.0, n)
    prof = 0.7 + 0.9 * x**2
    return prof / prof.mean()
