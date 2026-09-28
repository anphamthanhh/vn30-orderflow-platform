"""Synthetic VN30F1M trade tape and 10-level order book, in the L2 fixture schemas.

Tape : event_timestamp, price, volume, aggressor   (B = buyer-initiated, S = seller, U = auction print)
Book : event_timestamp, bid_prices, bid_volumes, offer_prices, offer_volumes, total_bid_qty, total_offer_qty

One vectorised step per second of continuous trading. A latent AR(1) order-flow imbalance ``x``
tilts the aggressor mix, skews the touch queues and nudges the mid, so imbalance leads price. A
per-day regime shapes the mid: ``normal`` (random walk), ``trend`` (drift) or ``balance`` (mean
reversion to the open). The spread (1-4 ticks, median 0.2 pt) widens in the closing auction
(14:30-14:45), when the book updates but nothing trades. Trades arrive Poisson with a U-shaped
intraday intensity; the 09:00:00 and 14:45:00 auctions each print once with aggressor ``U``.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.signal import lfilter

from .session_grid import AFTERNOON, MORNING, intraday_vol_profile

TICK = 0.1
LEVELS = 10
SPREAD_TICKS = np.array([1, 2, 3, 4])
SPREAD_PROBS = np.array([0.35, 0.45, 0.15, 0.05])
ATC_SPREAD_TICKS = np.array([2, 3, 4, 6])
ATC_START, ATC_END = dt.time(14, 30), dt.time(14, 45)


@dataclass(frozen=True)
class TapeParams:
    seed: int = 20260923
    days: tuple[str, ...] = ("2021-03-04", "2021-03-05")  # DEV range, not expiry days
    regimes: tuple[str, ...] = ("normal", "normal")      # per day: normal | trend | balance
    start_mid: float = 1250.0
    trades_per_sec: float = 4.0
    imbalance_ar: float = 0.97
    imbalance_sd: float = 0.25
    trend_ticks_per_min: float = 0.6
    balance_pull_per_sec: float = 0.004


def _seconds(day: dt.date, start: dt.time, end: dt.time, inclusive: str = "left") -> pd.DatetimeIndex:
    return pd.date_range(pd.Timestamp.combine(day, start), pd.Timestamp.combine(day, end), freq="1s", inclusive=inclusive)


def _mid_path(p: TapeParams, regime: str, x: np.ndarray, open_mid: float, rng) -> np.ndarray:
    n = len(x)
    move = rng.random(n) < 0.08 + 0.1 * np.abs(np.tanh(x))
    steps = TICK * move * np.where(rng.random(n) < 0.7, np.sign(x), -np.sign(x))
    if regime == "trend":
        steps = steps + TICK * (rng.random(n) < p.trend_ticks_per_min / 60.0)
    if regime == "balance":  # AR(1) deviation from the open: mean-reverting mid
        dev = lfilter([1.0], [1.0, -(1.0 - p.balance_pull_per_sec)], steps)
        return np.round((open_mid + dev) / TICK) * TICK
    return np.round((open_mid + np.cumsum(steps)) / TICK) * TICK


def _quotes(mid: np.ndarray, spread_choices: np.ndarray, probs: np.ndarray | None, rng):
    half = TICK * rng.choice(spread_choices, size=len(mid), p=probs) / 2.0
    bid = np.round(np.floor((mid - half) / TICK + 1e-9) * TICK, 1)
    ask = np.round(np.maximum(np.ceil((mid + half) / TICK - 1e-9) * TICK, bid + TICK), 1)
    return bid, ask


def _book(ts: pd.DatetimeIndex, bid: np.ndarray, ask: np.ndarray, x: np.ndarray, rng) -> pd.DataFrame:
    n, depth = len(ts), np.arange(1, LEVELS + 1)
    bv = rng.lognormal(2.3, 0.7, (n, LEVELS)) * (1 + 0.15 * depth)
    av = rng.lognormal(2.3, 0.7, (n, LEVELS)) * (1 + 0.15 * depth)
    bv[:, 0] *= 1 + 0.6 * np.tanh(x)  # queue imbalance at the touch follows the latent flow
    av[:, 0] *= 1 - 0.6 * np.tanh(x)
    bv, av = np.maximum(1, bv.round()).astype(int), np.maximum(1, av.round()).astype(int)
    hidden = rng.uniform(12, 20, (n, 2))
    bps = np.round(bid[:, None] - TICK * (depth - 1), 1)
    aps = np.round(ask[:, None] + TICK * (depth - 1), 1)
    fmt = lambda row: "[" + ",".join(f"{v:g}" for v in row) + "]"  # noqa: E731 - ClickHouse array text
    return pd.DataFrame({
        "event_timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
        "bid_prices": [fmt(r) for r in bps], "bid_volumes": [fmt(r) for r in bv],
        "offer_prices": [fmt(r) for r in aps], "offer_volumes": [fmt(r) for r in av],
        "total_bid_qty": bv.sum(1) + (bv.sum(1) * hidden[:, 0]).astype(int),
        "total_offer_qty": av.sum(1) + (av.sum(1) * hidden[:, 1]).astype(int),
    })


def simulate_l2(p: TapeParams = TapeParams()) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Full-session tape and one book snapshot per second (continuous + closing auction), per day."""
    rng = np.random.default_rng(p.seed)
    mid_level, x_prev = p.start_mid, 0.0
    tapes, books = [], []
    for iso, regime in zip(p.days, p.regimes):
        day = dt.date.fromisoformat(iso)
        secs = _seconds(day, *MORNING).append(_seconds(day, *AFTERNOON))
        x, _ = lfilter([1.0], [1.0, -p.imbalance_ar], rng.normal(0.0, p.imbalance_sd, len(secs)),
                       zi=[p.imbalance_ar * x_prev])
        mid = _mid_path(p, regime, x, mid_level, rng)
        bid, ask = _quotes(mid, SPREAD_TICKS, SPREAD_PROBS, rng)
        counts = rng.poisson(p.trades_per_sec * intraday_vol_profile(len(secs)))
        idx = np.repeat(np.arange(len(secs)), counts)
        buyer = rng.random(len(idx)) < 1.0 / (1.0 + np.exp(-1.5 * x[idx]))
        cont = pd.DataFrame({"ts": secs[idx], "price": np.where(buyer, ask[idx], bid[idx]),
                             "volume": rng.geometric(0.35, len(idx)), "aggressor": np.where(buyer, "B", "S")})
        atc = _seconds(day, ATC_START, ATC_END, inclusive="both")
        atc_mid = np.full(len(atc), mid[-1])
        abid, aask = _quotes(atc_mid, ATC_SPREAD_TICKS, None, rng)
        auctions = pd.DataFrame({"ts": [secs[0], atc[-1]], "price": [round(mid_level, 1), round(mid[-1], 1)],
                                 "volume": rng.integers(50, 800, 2), "aggressor": ["U", "U"]})
        tapes.append(pd.concat([auctions.iloc[:1], cont, auctions.iloc[1:]], ignore_index=True))
        books.append(_book(secs.append(atc), np.concatenate([bid, abid]), np.concatenate([ask, aask]),
                           np.concatenate([x, np.zeros(len(atc))]), rng))
        mid_level, x_prev = float(mid[-1]), float(x[-1])
    tape = pd.concat(tapes, ignore_index=True)
    tape.insert(0, "event_timestamp", tape.pop("ts").dt.strftime("%Y-%m-%d %H:%M:%S"))
    tape["price"] = tape["price"].round(1)
    return tape, pd.concat(books, ignore_index=True)


# Fixture windows (tape: second-of-day ranges; book: minutes of day): the open, both sides of the
# lunch break (11:29 / 13:00), mid-session minutes and the closing auction print at 14:45:00.
TAPE_WINDOWS = [(32400, 32460), (37500, 37560), (39600, 39660), (41340, 41400),
                (46800, 46860), (47340, 47400), (53040, 53101)]
BOOK_MINUTES = (540, 541, 689, 780, 884, 885)


def window(df: pd.DataFrame, *, seconds: list[tuple[int, int]] | None = None,
           minutes: tuple[int, ...] | None = None) -> pd.DataFrame:
    """Keep rows whose ICT time-of-day falls in the given second ranges or whole minutes."""
    t = pd.to_datetime(df["event_timestamp"])
    sod = t.dt.hour * 3600 + t.dt.minute * 60 + t.dt.second
    mask = np.zeros(len(df), dtype=bool)
    for lo, hi in seconds or []:
        mask |= (sod >= lo) & (sod < hi)
    for m in minutes or ():
        mask |= (sod // 60) == m
    return df[mask].reset_index(drop=True)
