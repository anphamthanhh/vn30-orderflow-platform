"""Synthetic VN30 cash index and VN30F1M front-month futures at M1 resolution, in index points.

  index      S_t : GBM with a U-shaped intraday volatility profile and overnight gaps.
  fair basis     : S_t * carry * days_to_expiry / 365 (cost of carry net of dividends).
  deviation  b_t : Ornstein-Uhlenbeck noise around the fair basis.
  futures    F_t : S_t + fair + b_t on the 0.1-point tick grid.
  expiry day     : the basis decays to 0 over the last hour; settlement is the HNX trimmed mean
                   of the last 30 index values (3 highest and 3 lowest dropped), so the basis
                   against the closing index is not zero. The next month then becomes front.

``planted_reversion_pts = k`` plants a known effect: session D+1 drifts by ``-k * z_D`` points,
where ``z_D`` is the causal 20-session z-score of the end-of-day basis; ``k = 0`` is the null.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.signal import lfilter

from .session_grid import expiry_days, intraday_vol_profile, minute_grid, trading_days

TICK = 0.1
Z_LOOKBACK = 20
BARS_PER_SESSION = 240  # 150 morning (09:00-11:30) + 90 afternoon (13:00-14:30) continuous minutes


@dataclass(frozen=True)
class MarketParams:
    seed: int = 20260923
    # inside the research DEV range (holdout_lock.json), so the holdout guard accepts the tape
    start: dt.date = dt.date(2021, 1, 4)
    n_days: int = 250
    s0: float = 1900.0
    daily_vol: float = 0.012  # ~19% annualised
    overnight_vol: float = 0.004
    carry_annual: float = 0.02
    dev_sigma_pts: float = 1.5  # stationary std of the basis deviation
    dev_half_life_min: float = 45.0
    # 0: the deviation is redrawn each morning (the closing basis says nothing about the next
    # session); 1: it persists overnight and mean-reverts next session, which a fade rule captures
    dev_overnight_persistence: float = 0.0
    planted_reversion_pts: float = 0.0


def _trimmed_settlement(last30: np.ndarray) -> float:
    """HNX final settlement: mean of the last 30 index values without the 3 highest and 3 lowest."""
    s = np.sort(last30)
    return float(s[3:-3].mean())


def _on_tick(x: np.ndarray) -> np.ndarray:
    return np.round(x / TICK) * TICK


def simulate(p: MarketParams, with_m1: bool = True) -> tuple[pd.DataFrame | None, pd.DataFrame]:
    """Return (m1, daily); ``m1`` is None when ``with_m1=False`` (fast path for Monte Carlo).

    m1    : ts, index_close, front_close, basis, contract, open/high/low/close/volume (futures)
    daily : d, f_open, f_close, basis_eod, basis_avg, is_expiry  (the basis_screen input)
    """
    rng = np.random.default_rng(p.seed)
    days = trading_days(p.start, p.n_days)
    expiries = sorted(expiry_days(days))
    n = BARS_PER_SESSION
    prof = intraday_vol_profile(n)
    vol = p.daily_vol / np.sqrt(n) * prof
    phi = 2.0 ** (-1.0 / p.dev_half_life_min)
    dev_eps = p.dev_sigma_pts * np.sqrt(1.0 - phi**2)
    fade_in = np.clip(np.arange(n)[::-1] / 60.0, 0.0, 1.0)  # expiry-day convergence weights

    s, dev = p.s0, rng.normal(0.0, p.dev_sigma_pts)
    eod_basis: list[float] = []
    frames, daily = [], []
    for day in days:
        front = next((e for e in expiries if e >= day), None)
        dte = (front - day).days if front else 30
        drift_pts = 0.0
        if p.planted_reversion_pts and len(eod_basis) >= Z_LOOKBACK:
            hist = np.asarray(eod_basis[-Z_LOOKBACK:])
            z = (hist[-1] - hist.mean()) / (hist.std(ddof=1) or 1.0)
            drift_pts = -p.planted_reversion_pts * z
        s *= np.exp(rng.normal(0.0, p.overnight_vol))
        rho = p.dev_overnight_persistence
        dev = rho * dev + np.sqrt(1.0 - rho**2) * rng.normal(0.0, p.dev_sigma_pts)
        idx = s * np.exp(np.cumsum(rng.normal(0.0, 1.0, n) * vol + drift_pts / n / s))
        # OU recursion dev_t = phi*dev_{t-1} + eps_t, vectorised as an IIR filter
        devs, _ = lfilter([1.0], [1.0, -phi], rng.normal(0.0, dev_eps, n), zi=[phi * dev])
        dev = float(devs[-1])
        fair = idx * p.carry_annual * max(dte, 0) / 365.0
        is_expiry = front == day
        if is_expiry:
            fair, devs = fair * fade_in, devs * fade_in
        fut = _on_tick(idx + fair + devs)
        if is_expiry:
            fut[-1] = _on_tick(np.array([_trimmed_settlement(idx[-30:])]))[0]
        basis = fut - idx
        vol_lots = np.maximum(1, rng.lognormal(3.0, 0.6, n) * prof).astype(int)
        wiggle = np.abs(rng.normal(0.0, 1.0, (n, 2))) * (vol * idx)[:, None]
        if with_m1:
            grid = minute_grid(day)
            assert len(grid) == n, f"session grid has {len(grid)} bars, expected {n}"
            prev = np.concatenate([[fut[0]], fut[:-1]])
            frames.append(pd.DataFrame({
                "ts": grid, "index_close": np.round(idx, 2), "front_close": fut,
                "basis": np.round(basis, 3), "contract": f"VN30F{front:%y%m}" if front else "VN30F-next",
                "open": prev, "high": _on_tick(np.maximum(prev, fut) + wiggle[:, 0]),
                "low": _on_tick(np.minimum(prev, fut) - wiggle[:, 1]), "close": fut, "volume": vol_lots,
            }))
        daily.append((day.isoformat(), fut[0], fut[-1], round(float(basis[-1]), 3),
                      round(float(basis.mean()), 3), is_expiry))
        eod_basis.append(float(basis[-1]))
        s = float(idx[-1])
        if is_expiry:  # the new front month opens with an unrelated deviation
            dev = 0.0
    daily_df = pd.DataFrame(daily, columns=["d", "f_open", "f_close", "basis_eod", "basis_avg", "is_expiry"])
    return (pd.concat(frames, ignore_index=True) if with_m1 else None), daily_df
