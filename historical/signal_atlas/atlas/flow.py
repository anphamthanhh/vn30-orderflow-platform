# -*- coding: utf-8 -*-
"""Order-flow state: tick delta, CVD, exhaustion divergence, VPIN and a chandelier stop.

Ports of ``orderflow-engine/src/{delta,cvd,vpin}.rs`` and ``signal-engine/src/trailing.rs``.
CVD is a path, not a level: its value depends on the reset policy chosen in :func:`cvd_series`.
"""

from __future__ import annotations

from collections import deque

import numpy as np


def tick_delta(volume, side):
    """Return ``(buy, sell, delta)`` volume for one print; port of ``DeltaCalculator::on_tick``.

    An unrecognised side yields zero delta: an unclassified aggressor is evidence for neither side.
    """
    v = volume if (np.isfinite(volume) and volume >= 0.0) else 0.0
    if side in ("B", "BUY", "Buy", "buy"):
        return v, 0.0, v
    if side in ("S", "SELL", "Sell", "sell"):
        return 0.0, v, -v
    return 0.0, 0.0, 0.0


class CvdTracker:
    """Cumulative volume delta with an ``initial_value`` seam for rollover."""

    def __init__(self):
        self.cumulative_delta = 0.0
        self.initial_value = 0.0

    def update(self, delta, ts=None):
        if np.isfinite(delta):
            self.cumulative_delta += delta
        return self.current_value()

    def current_value(self):
        return self.initial_value + self.cumulative_delta

    def reset(self):
        self.cumulative_delta = 0.0
        self.initial_value = 0.0

    def rollover(self):
        """Keep the current level and restart accumulation from it (lunch break, 13:00 ICT)."""
        self.initial_value += self.cumulative_delta
        self.cumulative_delta = 0.0


class DivergenceDetector:
    """CVD exhaustion divergence; port of ``orderflow-engine/src/cvd.rs``.

    The window holds the last ``lookback`` bars including the current one; detection starts once
    ``lookback + 2`` valid bars are seen. Bearish: the close is within 0.1 % of the rolling high
    while CVD has fallen more than ``cvd_threshold`` from a positive rolling high; bullish mirrors
    it with a negative rolling low. ``strength`` is the relative move capped at 1.0; ``max(1)`` in
    the denominator guards against a near-zero reference.
    """

    def __init__(self, lookback: int, cvd_threshold: float, min_strength: float):
        self.lookback = max(1, int(lookback))
        self.cvd_threshold = cvd_threshold if np.isfinite(cvd_threshold) else 0.3
        self.min_strength = min_strength if np.isfinite(min_strength) else 0.3
        self.bars = deque()
        self.bars_seen = 0
        self.probe = {}

    def on_bar(self, ts, close, high, low, cvd):
        self.probe = {}
        if not all(np.isfinite(x) for x in (close, high, low, cvd)) or high < low:
            return None
        self.bars.append((ts, close, high, low, cvd))
        while len(self.bars) > self.lookback:
            self.bars.popleft()
        self.bars_seen += 1
        if self.bars_seen < self.lookback + 2:
            self.probe = {"warm": False}
            return None

        closes = [b[1] for b in self.bars]
        cvds = [b[4] for b in self.bars]
        max_close, min_close = max(closes), min(closes)
        max_cvd, min_cvd = max(cvds), min(cvds)

        bear_price = close >= max_close * 0.999
        bear_cvd = cvd <= max_cvd * (1.0 - self.cvd_threshold)
        bear_sign = max_cvd > 0.0
        bull_price = close <= min_close * 1.001
        bull_cvd = cvd >= min_cvd * (1.0 + self.cvd_threshold)
        bull_sign = min_cvd < 0.0

        self.probe = {
            "warm": True,
            "max_close": max_close,
            "min_close": min_close,
            "max_cvd": max_cvd,
            "min_cvd": min_cvd,
            "bear_price_at_high": bear_price,
            "bear_cvd_collapsed": bear_cvd,
            "bear_ref_positive": bear_sign,
            "bull_price_at_low": bull_price,
            "bull_cvd_recovered": bull_cvd,
            "bull_ref_negative": bull_sign,
        }

        if bear_price and bear_cvd and bear_sign:
            strength = min(abs(max_cvd - cvd) / max(abs(max_cvd), 1.0), 1.0)
            self.probe["strength"] = strength
            if strength >= self.min_strength:
                return {
                    "type": "bearish",
                    "strength": strength,
                    "price": close,
                    "cvd": cvd,
                    "ts": ts,
                }
            return None
        if bull_price and bull_cvd and bull_sign:
            strength = min(abs(cvd - min_cvd) / max(abs(min_cvd), 1.0), 1.0)
            self.probe["strength"] = strength
            if strength >= self.min_strength:
                return {
                    "type": "bullish",
                    "strength": strength,
                    "price": close,
                    "cvd": cvd,
                    "ts": ts,
                }
        return None

    def reset(self):
        self.bars.clear()
        self.bars_seen = 0


class VpinCalculator:
    """VPIN over volume-synchronised buckets; port of ``orderflow-engine/src/vpin.rs``.

    ``VPIN = mean(|buy - sell| / bucket_size)`` over the last ``n_buckets`` completed buckets
    (Easley, Lopez de Prado and O'Hara). The regime is toxic above ``toxic``, elevated above
    ``elevated`` (strict ``>``), otherwise normal; it stays normal until warm, so a cold filter
    never blocks.
    """

    def __init__(
        self,
        bucket_size: float = 500.0,
        n_buckets: int = 50,
        toxic: float = 0.7,
        elevated: float = 0.5,
    ):
        self.bucket_size = bucket_size if (np.isfinite(bucket_size) and bucket_size > 0) else 1.0
        self.n_buckets = max(1, int(n_buckets))
        self.toxic_threshold = toxic
        self.elevated_threshold = elevated
        self.cur_buy = 0.0
        self.cur_sell = 0.0
        self.deltas = deque()
        self.sum_deltas = 0.0

    def add_trade(self, buy_vol, sell_vol):
        buy = buy_vol if (np.isfinite(buy_vol) and buy_vol > 0) else 0.0
        sell = sell_vol if (np.isfinite(sell_vol) and sell_vol > 0) else 0.0
        self.cur_buy += buy
        self.cur_sell += sell
        completed = False
        if self.cur_buy + self.cur_sell >= self.bucket_size:
            abs_delta = min(
                max(abs(self.cur_buy - self.cur_sell) / max(self.bucket_size, 1.0), 0.0), 1.0
            )
            self.deltas.append(abs_delta)
            self.sum_deltas += abs_delta
            while len(self.deltas) > self.n_buckets:
                self.sum_deltas -= self.deltas.popleft()
            self.cur_buy = self.cur_sell = 0.0
            completed = True
        return self.current_vpin(), completed

    def current_vpin(self):
        if not self.deltas:
            return 0.0
        return min(max(self.sum_deltas / len(self.deltas), 0.0), 1.0)

    def is_warm(self):
        return len(self.deltas) >= max(self.n_buckets // 5, 5)

    def regime(self):
        if not self.is_warm():
            return "normal"
        v = self.current_vpin()
        if v > self.toxic_threshold:
            return "toxic"
        if v > self.elevated_threshold:
            return "elevated"
        return "normal"

    def reset(self):
        self.cur_buy = self.cur_sell = 0.0
        self.deltas.clear()
        self.sum_deltas = 0.0


class TrailingStop:
    """Chandelier trailing stop; port of ``signal-engine/src/trailing.rs``.

    Initial stop ``entry -/+ k_init * ATR`` (``fallback_stop_pts`` while ATR is unavailable), then
    ``extreme -/+ k_eff * ATR`` from the best price since entry; ``k_eff`` is ``k_trail``, scaled
    by ``elevated_tighten`` in an elevated VPIN regime. The stop is tick-rounded, never widens and
    moves only in steps of at least one tick.
    """

    def __init__(
        self,
        atr_period=10,
        k_init=2.0,
        k_trail=1.5,
        elevated_tighten=0.75,
        tick_size=0.1,
        fallback_stop_pts=1.5,
    ):
        self.k_init = k_init
        self.k_trail = k_trail
        self.elevated_tighten = elevated_tighten
        self.tick = tick_size
        self.fallback_stop_pts = fallback_stop_pts
        self.atr_period = atr_period
        self.position = None  # dict(direction, entry_price, entry_ts, stop, extreme)

    def _round(self, price):
        return round(price / self.tick) * self.tick

    def open(self, direction, entry, ts, atr, regime="normal"):
        if (
            self.position is not None
            or not np.isfinite(entry)
            or direction not in ("long", "short")
        ):
            return
        dist = (
            self.k_init * atr
            if (atr is not None and np.isfinite(atr) and atr > 0)
            else self.fallback_stop_pts
        )
        stop = self._round(entry - dist) if direction == "long" else self._round(entry + dist)
        self.position = {
            "direction": direction,
            "entry_price": entry,
            "entry_ts": ts,
            "stop": stop,
            "extreme": entry,
        }

    def on_tick(self, price, ts, atr, regime="normal"):
        """Process a price; return ``('hit', stop)``, ``('updated', stop)`` or ``(None, stop)``."""
        if self.position is None or not np.isfinite(price):
            return None, None
        pos = self.position
        crossed = price <= pos["stop"] if pos["direction"] == "long" else price >= pos["stop"]
        if crossed:
            stop = pos["stop"]
            self.position = None
            return "hit", stop
        if pos["direction"] == "long":
            pos["extreme"] = max(pos["extreme"], price)
        else:
            pos["extreme"] = min(pos["extreme"], price)
        if atr is None or not np.isfinite(atr) or atr <= 0:
            return None, pos["stop"]
        k_eff = self.k_trail * self.elevated_tighten if regime == "elevated" else self.k_trail
        if pos["direction"] == "long":
            cand = self._round(pos["extreme"] - k_eff * atr)
            if cand - pos["stop"] >= self.tick - 1e-9:
                pos["stop"] = cand
                return "updated", cand
        else:
            cand = self._round(pos["extreme"] + k_eff * atr)
            if pos["stop"] - cand >= self.tick - 1e-9:
                pos["stop"] = cand
                return "updated", cand
        return None, pos["stop"]

    def force_exit(self, price):
        if self.position is None:
            return None, None
        stop = self.position["stop"]
        self.position = None
        return "hit", stop


# --- series ---
def cvd_series(ticks, reset="none"):
    """Return CVD over a tick frame under one reset policy.

    ``reset='none'`` (whole tape), ``'session'`` (each segment open) or ``'day_rollover'``
    (reset at 09:00, roll over at 13:00).
    """
    signed = np.asarray(ticks["signed"], dtype="float64")
    day = np.asarray(ticks["day"], dtype="int64")
    seg = np.asarray(ticks["seg"], dtype="int64")
    out = np.empty(signed.size, dtype="float64")
    tracker = CvdTracker()
    prev_key = None
    for i in range(signed.size):
        if reset == "session":
            key = (day[i], seg[i])
            if key != prev_key:
                tracker.reset()
                prev_key = key
        elif reset == "day_rollover":
            key = (day[i], seg[i])
            if prev_key is None or key[0] != prev_key[0]:
                tracker.reset()
            elif key[1] != prev_key[1] and key[1] == 1:
                tracker.rollover()
            prev_key = key
        out[i] = tracker.update(signed[i])
    return out


def vpin_series(ticks, bucket_size=500.0, n_buckets=50, reset_daily=True):
    """Return VPIN and its regime for every tick, optionally resetting at each new day."""
    vp = VpinCalculator(bucket_size, n_buckets)
    vals = np.empty(len(ticks), dtype="float64")
    regimes = np.empty(len(ticks), dtype=object)
    day = np.asarray(ticks["day"], dtype="int64")
    prev_day = None
    for i, (side, vol) in enumerate(zip(ticks["side"], ticks["volume"])):
        if reset_daily and prev_day is not None and day[i] != prev_day:
            vp.reset()
        prev_day = day[i]
        b, s, _ = tick_delta(vol, side)
        vp.add_trade(b, s)
        vals[i] = vp.current_vpin()
        regimes[i] = vp.regime()
    return vals, regimes
