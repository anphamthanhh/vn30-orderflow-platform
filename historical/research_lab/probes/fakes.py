"""Seeded, deterministic test doubles for the resting-limit probe.

``FakeBroker`` implements ``BrokerPort`` with a seeded RNG and a virtual clock, so the driver,
interlocks, budgets and log chain run offline and reproducibly. Its fill rule is a per-poll
coin flip, not a market model: nothing measured against it says anything about VN30F1M.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass, field

from historical.research_lab.probes.resting_limit_probe import (
    ICT,
    TICK,
    BookSnapshot,
    OrderState,
    round_to_tick,
)


@dataclass
class _FakeOrder:
    order_id: str
    side: str
    limit_px: float
    qty: int
    status: str = "pending"
    filled_qty: int = 0
    avg_fill_px: float | None = None


class VirtualClock:
    """Manually advanced clock, so the driver never reads wall time."""

    def __init__(self, start: dt.datetime | None = None, step_s: float = 1.0) -> None:
        self.now = start or dt.datetime(2026, 9, 2, 10, 0, 0, tzinfo=ICT)
        self.step_s = float(step_s)

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + dt.timedelta(seconds=float(seconds))

    def sleep(self, seconds: float) -> None:
        """Drop-in for ``time.sleep`` that advances the virtual clock instead."""
        self.advance(seconds)


@dataclass
class FakeBroker:
    """In-memory ``BrokerPort``: seeded, no network, no wall clock.

    Args:
        mid: starting mid price.
        spread_ticks: constant spread in ticks.
        fill_prob_by_offset: fill probability per poll, keyed by ticks behind the touch.
        drift_per_poll: points the mid moves each poll (exercises adverse selection).
        depth: resting size at every ladder level (feeds ``queue_ahead_at_place``).
        force_marketable_fill: price fills one tick worse than the limit (the hard-stop case).
    """

    mid: float = 1900.0
    spread_ticks: int = 2
    fill_prob_by_offset: dict[int, float] = field(default_factory=lambda: {0: 1.0, 1: 0.0, 2: 0.0})
    drift_per_poll: float = 0.0
    depth: int = 5
    ladder_levels: int = 5
    seed: int = 7
    clock: VirtualClock | None = None
    force_marketable_fill: bool = False

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)
        self._orders: dict[str, _FakeOrder] = {}
        self._next_id = 1
        self.place_calls: list[tuple[str, str, float, int, str]] = []
        self.cancel_calls: list[str] = []
        self.clock = self.clock or VirtualClock()

    # -- BrokerPort ------------------------------------------------------
    def get_book(self, symbol: str) -> BookSnapshot:
        half = self.spread_ticks * TICK / 2.0
        bid = round_to_tick(self.mid - half)
        ask = round_to_tick(self.mid + half)
        bids = tuple((round_to_tick(bid - i * TICK), self.depth) for i in range(self.ladder_levels))
        asks = tuple((round_to_tick(ask + i * TICK), self.depth) for i in range(self.ladder_levels))
        return BookSnapshot(
            ts=self.clock().timestamp(),
            best_bid=bid,
            best_ask=ask,
            bid_size=self.depth,
            ask_size=self.depth,
            bids=bids,
            asks=asks,
        )

    def place_limit(self, symbol: str, side: str, price: float, qty: int, coid: str) -> str:
        self.place_calls.append((symbol, side, price, qty, coid))
        oid = f"fake-{self._next_id}"
        self._next_id += 1
        self._orders[oid] = _FakeOrder(order_id=oid, side=side, limit_px=price, qty=qty)
        return oid

    def cancel(self, order_id: str) -> None:
        self.cancel_calls.append(order_id)
        o = self._orders.get(order_id)
        if o is not None and o.status == "pending":
            o.status = "canceled"

    def get_open_orders(self, symbol: str | None = None) -> list[OrderState]:
        """Advance the market one poll and return all orders, terminal ones included.

        Mirrors DNSE ``GET /derivative/orders``, which lists the day's orders with a status.
        """
        self._tick_market()
        return [
            OrderState(
                order_id=o.order_id,
                status=o.status,
                filled_qty=o.filled_qty,
                avg_fill_px=o.avg_fill_px,
                side=o.side,
                limit_px=o.limit_px,
            )
            for o in self._orders.values()
        ]

    # -- internals -------------------------------------------------------
    def _tick_market(self) -> None:
        self.mid = round(self.mid + self.drift_per_poll, 6)
        book = BookSnapshot(
            ts=0.0,
            best_bid=round_to_tick(self.mid - self.spread_ticks * TICK / 2.0),
            best_ask=round_to_tick(self.mid + self.spread_ticks * TICK / 2.0),
        )
        for o in self._orders.values():
            if o.status != "pending":
                continue
            touch = book.best_bid if o.side == "buy" else book.best_ask
            offset = int(round(abs(touch - o.limit_px) / TICK))
            p = float(self.fill_prob_by_offset.get(offset, 0.0))
            if self._rng.random() < p:
                o.status = "filled"
                o.filled_qty = o.qty
                if self.force_marketable_fill:
                    # one tick worse than the limit: impossible for a resting order
                    o.avg_fill_px = round_to_tick(
                        o.limit_px + TICK if o.side == "buy" else o.limit_px - TICK
                    )
                else:
                    o.avg_fill_px = o.limit_px
