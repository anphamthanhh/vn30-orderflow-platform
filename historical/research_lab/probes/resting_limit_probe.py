"""Resting-limit fill probe for VN30F1M on a DNSE demo account (see ADR 0023).

Places one qty-1 limit per (side, offset) behind the touch, cancels it if unfilled after a
5–60 s horizon and appends one hash-chained JSONL row per order, including the fill-hazard
inputs ``spread_ticks``, ``queue_ahead_at_place`` and ``vol_1min_at_place``. Fail-closed
interlocks: (1) a missing or denylisted account is refused at construction; (2) a crossing
price raises before send, never clamped; (3) dry run by default, live sends need ``--live``
and ``DNSE_PROBE_LIVE``; (4) order, concurrency and fill budgets, with a hard stop on a fill
worse than the limit; (5) continuous-session placement only, cancel-all at 14:43 ICT.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import statistics
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Protocol, Sequence, runtime_checkable

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(REPO_ROOT))

from historical.backtest_engine.costs import TICK  # noqa: E402  — the tick authority

# --- safety constants ---
REAL_ACCOUNT_DENYLIST: tuple[str, ...] = ("0001234567",)  # placeholder account number
"""Accounts the probe refuses to be constructed against (fail-closed)."""


def canonical_account(value: object) -> str:
    """Normalise an account number to its digits without leading zeros.

    Makes the denylist also match variants such as ``"0001-234-567"`` or ``" 1234567 "``.
    """
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits.lstrip("0") or digits


_DENIED_CANONICAL = frozenset(canonical_account(a) for a in REAL_ACCOUNT_DENYLIST)


def is_denied_account(value: object) -> bool:
    """True when ``value`` denotes an account the probe must never trade."""
    canon = canonical_account(value)
    return bool(canon) and canon in _DENIED_CANONICAL


LIVE_ENV_VAR = "DNSE_PROBE_LIVE"
LIVE_ENV_VALUE = "yes-place-demo-orders"
"""Value ``LIVE_ENV_VAR`` must hold, in addition to ``--live``, before any order is sent."""

DEFAULT_LOG_PATH = REPO_ROOT / "historical" / "research_lab" / "forward" / "limit_probe_log.jsonl"

# --- ICT session windows (VN30F1M continuous session). ----------------------
ICT = dt.timezone(dt.timedelta(hours=7))
MORNING_OPEN_MIN = 9 * 60  # 09:00
MORNING_CLOSE_MIN = 11 * 60 + 30  # 11:30
AFTERNOON_OPEN_MIN = 13 * 60  # 13:00
AFTERNOON_CLOSE_MIN = 14 * 60 + 45  # 14:45
CANCEL_ALL_MIN = 14 * 60 + 43  # 14:43: cancel every open probe order

LEDGER_VERSION = 1
GENESIS_HASH = "0" * 64

# Ticks behind the touch: 0 joins the best price, 1 and 2 rest further from mid.
DEFAULT_OFFSETS: tuple[int, ...] = (0, 1, 2)


# --- errors (typed, so a safety refusal never reads as "no fill") ---
class ProbeError(RuntimeError):
    """Base for every probe failure."""


class RealAccountRefused(ProbeError):
    """The account is missing or denylisted, or the endpoint is not the paper-trading API."""


class LiveSendNotAuthorized(ProbeError):
    """``--live`` was requested without the environment authorization."""


class CrossingLimitRejected(ProbeError):
    """A limit price would cross the book on arrival; rejected before send, never clamped."""


class BudgetExhausted(ProbeError):
    """An order, concurrency or fill budget is spent, or the probe has already hard-stopped."""


class UnexpectedMarketableFill(ProbeError):
    """A probe order took liquidity (filled worse than its limit, or on arrival); hard stop."""


class OutsideSession(ProbeError):
    """Placement outside the continuous session, or with a horizon past the window end."""


class LedgerTampered(ProbeError):
    """The probe log's hash chain does not verify."""


# --- market and order value objects ---
@dataclass(frozen=True)
class BookSnapshot:
    """Top of book at an instant, with optional ``(price, size)`` ladders ordered best first.

    Without a ladder, ``queue_ahead_at_place`` is None away from the touch, never imputed.
    """

    ts: float
    best_bid: float
    best_ask: float
    bid_size: int | None = None
    ask_size: int | None = None
    bids: tuple[tuple[float, int], ...] = ()
    asks: tuple[tuple[float, int], ...] = ()

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid

    @property
    def spread_ticks(self) -> float:
        return round(self.spread / TICK, 3)

    def is_crossed_or_locked(self) -> bool:
        return self.best_ask <= self.best_bid

    def size_at(self, side: str, price: float) -> int | None:
        """Resting size at ``price`` on ``side``'s own book, i.e. the queue a new order joins.

        Without a ladder only the touch is known (L1 size); otherwise None, never a stand-in 0.
        """
        ladder = self.bids if side == "buy" else self.asks
        for px, sz in ladder:
            if _same_price(px, price):
                return int(sz)
        if ladder:
            return 0  # ladder present and this level is not in it => empty level
        touch = self.best_bid if side == "buy" else self.best_ask
        if _same_price(touch, price):
            sz = self.bid_size if side == "buy" else self.ask_size
            return None if sz is None else int(sz)
        return None


@dataclass(frozen=True)
class OrderState:
    """Broker-reported state of one probe order.

    ``status`` is ``pending``, ``partially_filled``, ``filled``, ``canceled`` or ``rejected``.
    """

    order_id: str
    status: str
    filled_qty: int = 0
    avg_fill_px: float | None = None
    side: str | None = None
    limit_px: float | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in ("filled", "canceled", "rejected")

    @property
    def is_filled(self) -> bool:
        return self.status == "filled" and self.filled_qty > 0


@runtime_checkable
class BrokerPort(Protocol):
    """Venue interface the probe needs; ``fakes.FakeBroker`` implements it offline."""

    def get_book(self, symbol: str) -> BookSnapshot:
        """Current top of book (plus ladder if the feed carries one)."""

    def place_limit(self, symbol: str, side: str, price: float, qty: int, coid: str) -> str:
        """Place a resting limit (``side`` is ``"buy"`` or ``"sell"``); return the order id."""

    def cancel(self, order_id: str) -> None:
        """Cancel one order. Must be idempotent for an already-terminal order."""

    def get_open_orders(self, symbol: str | None = None) -> list[OrderState]:
        """Current known state of this session's orders."""


# --- helpers ---
def _same_price(a: float, b: float) -> bool:
    return abs(a - b) < TICK / 2.0


def round_to_tick(price: float) -> float:
    """Snap ``price`` to the venue tick grid; the venue rejects off-grid prices."""
    return round(round(price / TICK) * TICK, 6)


def minute_of_day_ict(now: dt.datetime) -> int:
    ict = now.astimezone(ICT)
    return ict.hour * 60 + ict.minute


def second_of_day_ict(now: dt.datetime) -> float:
    """Seconds since ICT midnight; the horizon guard needs sub-minute precision."""
    ict = now.astimezone(ICT)
    return ict.hour * 3600.0 + ict.minute * 60.0 + ict.second + ict.microsecond / 1e6


def session_phase(now: dt.datetime) -> str:
    """``"morning"`` | ``"afternoon"`` | ``"closed"`` for an ICT wall clock."""
    m = minute_of_day_ict(now)
    if MORNING_OPEN_MIN <= m < MORNING_CLOSE_MIN:
        return "morning"
    if AFTERNOON_OPEN_MIN <= m < AFTERNOON_CLOSE_MIN:
        return "afternoon"
    return "closed"


def window_end_minute(now: dt.datetime) -> int | None:
    """ICT minute by which a probe order in the current window must be resolved; None if closed.

    The afternoon window ends at the 14:43 cancel-all rather than the 14:45 close.
    """
    phase = session_phase(now)
    if phase == "morning":
        return MORNING_CLOSE_MIN
    if phase == "afternoon":
        return CANCEL_ALL_MIN
    return None


def mask_account(account_no: str) -> str:
    """Mask all but the last four characters of an account number for the log."""
    acct = str(account_no or "")
    return f"****{acct[-4:]}" if len(acct) >= 4 else "****"


class _MidHistory:
    """Trailing 60 s of mid observations for the ``vol_1min_at_place`` hazard input.

    Realised volatility is the population stdev of the observed mids, in points: a regime
    tag for conditioning the fill hazard, not a forecast.
    """

    def __init__(self, window_s: float = 60.0) -> None:
        self.window_s = float(window_s)
        self._obs: deque[tuple[float, float]] = deque()

    def observe(self, ts: float, mid: float) -> None:
        self._obs.append((float(ts), float(mid)))
        cutoff = float(ts) - self.window_s
        while self._obs and self._obs[0][0] < cutoff:
            self._obs.popleft()

    def realized_vol(self) -> float | None:
        if len(self._obs) < 2:
            return None
        return round(statistics.pstdev([m for _, m in self._obs]), 4)


# --- append-only, hash-chained log ---
# Same chain as forward_registry, re-implemented to avoid importing pandas on a trading host;
# test_resting_limit_probe.py::test_hash_chain_matches_forward_registry pins the parity.
def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def row_hash(row: dict, prev_hash: str) -> str:
    """SHA-256 over ``prev_hash`` and the canonical JSON of the row (``row_hash`` excluded)."""
    payload = {k: v for k, v in row.items() if k != "row_hash"}
    payload["prev_hash"] = prev_hash
    return hashlib.sha256((prev_hash + "|" + _canon(payload)).encode()).hexdigest()


def read_log(path: Path | str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def verify_chain(path: Path | str) -> dict:
    """Recompute the chain end to end; raise on the first row that was edited."""
    rows = read_log(path)
    prev = GENESIS_HASH
    for i, r in enumerate(rows):
        if r.get("prev_hash") != prev:
            raise LedgerTampered(
                f"{path}: row {i} prev_hash mismatch "
                f"(stored {r.get('prev_hash')!r}, expected {prev!r})"
            )
        want = row_hash(r, prev)
        if r.get("row_hash") != want:
            raise LedgerTampered(f"{path}: row {i} row_hash mismatch — content was edited")
        prev = want
    return {"path": str(path), "rows": len(rows), "head": prev, "ok": True}


def append_rows(path: Path | str, rows: Iterable[dict]) -> int:
    """Append rows to the chained log with one ``O_APPEND`` write per line; return the count.

    Never read-modify-write, so a concurrent reader always sees whole lines.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = read_log(p)
    if existing:
        verify_chain(p)  # never extend a broken chain
    prev = existing[-1]["row_hash"] if existing else GENESIS_HASH
    n = 0
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        for content in rows:
            rec = dict(content)
            rec["prev_hash"] = prev
            rec["row_hash"] = row_hash(rec, prev)
            os.write(fd, (_canon(rec) + "\n").encode("utf-8"))
            prev = rec["row_hash"]
            n += 1
    finally:
        os.close(fd)
    return n


# --- config and record ---
@dataclass(frozen=True)
class ProbeConfig:
    """Probe parameters, validated on construction (qty 1, 5–60 s horizon, at most 20 orders)."""

    account_no: str
    symbol: str = "VN30F1M"
    qty: int = 1
    offsets_ticks: tuple[int, ...] = DEFAULT_OFFSETS
    horizon_s: float = 60.0
    max_orders_per_session: int = 20
    max_concurrent: int = 2
    max_fills: int | None = None  # default: same as the order budget
    poll_interval_s: float = 1.0
    spacing_s: float = 30.0  # seconds between batches, so probe orders do not cluster
    log_path: Path = DEFAULT_LOG_PATH

    def __post_init__(self) -> None:
        if self.qty != 1:
            raise ValueError("probe qty is fixed at 1")
        if not 5.0 <= self.horizon_s <= 60.0:
            raise ValueError("horizon_s must be in the 5-60 s range")
        if self.max_orders_per_session <= 0 or self.max_orders_per_session > 20:
            raise ValueError("max_orders_per_session must be in 1..20")
        if self.max_concurrent <= 0 or self.max_concurrent > self.max_orders_per_session:
            raise ValueError("max_concurrent must be in 1..max_orders_per_session")

    @property
    def fill_budget(self) -> int:
        return self.max_orders_per_session if self.max_fills is None else int(self.max_fills)


@dataclass
class ProbeRecord:
    """One probe order from placement to resolution; one log row."""

    ts_place: float
    side: str
    offset_ticks: int
    limit_px: float
    best_bid: float
    best_ask: float
    mid: float
    spread_ticks: float
    queue_ahead_at_place: int | None
    vol_1min_at_place: float | None
    horizon_s: float
    session_phase: str
    symbol: str
    qty: int
    account: str
    dry_run: bool
    order_id: str | None = None
    ts_resolve: float | None = None
    filled: bool | None = None
    fill_px: float | None = None
    mid_at_resolve: float | None = None
    resolve_reason: str | None = None  # e.g. "filled", "horizon_cancel", "aborted", "dry_run"
    time_to_fill_s: float | None = None

    def to_row(self) -> dict:
        row = {"v": LEDGER_VERSION, **asdict(self)}
        row["recorded_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        return row


# --- probe ---
class RestingLimitProbe:
    """Places qty-1 resting limits at the configured offsets and logs each outcome.

    Account and live-send checks run in ``__init__``, so no later code path can skip them.
    """

    def __init__(
        self,
        broker: BrokerPort,
        config: ProbeConfig,
        *,
        dry_run: bool = True,
        clock: Callable[[], dt.datetime] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        # interlock 1: refuse a missing or denylisted account before the object exists
        acct = str(config.account_no or "").strip()
        if not acct:
            raise RealAccountRefused(
                "no account configured. The probe refuses to run against an unknown "
                "account: 'unknown' cannot be proven to be the demo account."
            )
        if is_denied_account(acct):
            raise RealAccountRefused(
                f"account {mask_account(acct)} is on REAL_ACCOUNT_DENYLIST. This probe "
                "places live orders and runs on the DEMO account ONLY."
            )

        # interlock 3: live sends need dry_run=False and LIVE_ENV_VAR set to LIVE_ENV_VALUE
        environ = os.environ if env is None else env
        self.dry_run = bool(dry_run)
        if not self.dry_run:
            got = environ.get(LIVE_ENV_VAR)
            if got != LIVE_ENV_VALUE:
                raise LiveSendNotAuthorized(
                    f"--live requires {LIVE_ENV_VAR}={LIVE_ENV_VALUE!r} in the environment "
                    f"(got {got!r}). Two independent gates are required so no single "
                    "stray flag or stray export can place an order."
                )

        self.broker = broker
        self.config = config
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.sleep = sleep_fn or time.sleep
        self.mids = _MidHistory()

        self.orders_placed = 0
        self.fills = 0
        self.records: list[ProbeRecord] = []
        self._live: dict[str, ProbeRecord] = {}
        self._seq = 0
        self.aborted_reason: str | None = None

    # -- pricing ---------------------------------------------------------
    def limit_price_for(self, book: BookSnapshot, side: str, offset_ticks: int) -> float:
        """Limit price for ``side`` at ``offset_ticks`` behind the touch (0 joins the best)."""
        if side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")
        if offset_ticks < 0:
            raise ValueError("offset_ticks must be >= 0 — a negative offset crosses the book")
        if side == "buy":
            return round_to_tick(book.best_bid - offset_ticks * TICK)
        return round_to_tick(book.best_ask + offset_ticks * TICK)

    def _assert_resting(self, book: BookSnapshot, side: str, price: float) -> None:
        """Interlock 2: raise ``CrossingLimitRejected`` unless ``price`` would rest."""
        if book.is_crossed_or_locked():
            raise CrossingLimitRejected(
                f"book is crossed/locked (bid {book.best_bid} >= ask {book.best_ask}); "
                "no price can be proven to rest. Refusing to send."
            )
        if side == "buy" and price >= book.best_ask - TICK / 2.0:
            raise CrossingLimitRejected(
                f"buy limit {price} >= best_ask {book.best_ask}: this order would TAKE, "
                "not rest. Rejected before send (never clamped — a clamp would relabel "
                "a taker fill as a maker fill and destroy the measurement)."
            )
        if side == "sell" and price <= book.best_bid + TICK / 2.0:
            raise CrossingLimitRejected(
                f"sell limit {price} <= best_bid {book.best_bid}: this order would TAKE, "
                "not rest. Rejected before send."
            )

    # -- session ---------------------------------------------------------
    def _assert_placeable(self, now: dt.datetime) -> str:
        """Interlock 5: the session must be open and the horizon must end before its window."""
        phase = session_phase(now)
        if phase == "closed":
            raise OutsideSession(
                f"{now.astimezone(ICT):%H:%M} ICT is outside the continuous session "
                "(09:00-11:30 / 13:00-14:45). Refusing to place."
            )
        end = window_end_minute(now)
        assert end is not None  # phase != closed
        # refuse an order that would still be live at the cutoff instant (>=, not >)
        if second_of_day_ict(now) + float(self.config.horizon_s) >= end * 60.0:
            raise OutsideSession(
                f"a {self.config.horizon_s:.0f}s horizon from "
                f"{now.astimezone(ICT):%H:%M:%S} ICT would still be live at the window end "
                f"({end // 60:02d}:{end % 60:02d}). Refusing to place an order the "
                "cancel-all rule would have to kill mid-measurement."
            )
        return phase

    def _assert_budget(self) -> None:
        """Interlock 4: raise ``BudgetExhausted`` if a budget is spent or the probe stopped."""
        if self.aborted_reason:
            raise BudgetExhausted(f"probe already hard-stopped: {self.aborted_reason}")
        if self.orders_placed >= self.config.max_orders_per_session:
            raise BudgetExhausted(
                f"order budget spent: {self.orders_placed}/"
                f"{self.config.max_orders_per_session} for this session"
            )
        if self.fills >= self.config.fill_budget:
            raise BudgetExhausted(f"fill budget spent: {self.fills}/{self.config.fill_budget}")
        if len(self._live) >= self.config.max_concurrent:
            raise BudgetExhausted(
                f"max_concurrent reached: {len(self._live)}/{self.config.max_concurrent} live"
            )

    # -- placement -------------------------------------------------------
    def place_probe(self, side: str, offset_ticks: int) -> ProbeRecord:
        """Place one qty-1 resting limit and return its unresolved record.

        In dry run nothing is sent and ``order_id`` stays None, but every interlock and the
        budget still apply.
        """
        now = self.clock()
        phase = self._assert_placeable(now)
        self._assert_budget()

        book = self.broker.get_book(self.config.symbol)
        self.mids.observe(book.ts, book.mid)
        price = self.limit_price_for(book, side, offset_ticks)
        self._assert_resting(book, side, price)

        rec = ProbeRecord(
            ts_place=book.ts,
            side=side,
            offset_ticks=int(offset_ticks),
            limit_px=price,
            best_bid=book.best_bid,
            best_ask=book.best_ask,
            mid=round(book.mid, 4),
            spread_ticks=book.spread_ticks,
            queue_ahead_at_place=book.size_at(side, price),
            vol_1min_at_place=self.mids.realized_vol(),
            horizon_s=float(self.config.horizon_s),
            session_phase=phase,
            symbol=self.config.symbol,
            qty=self.config.qty,
            account=mask_account(self.config.account_no),
            dry_run=self.dry_run,
        )

        self._seq += 1
        if self.dry_run:
            rec.resolve_reason = "dry_run"
            rec.filled = None
        else:
            coid = f"probe-{int(book.ts)}-{self._seq}"
            rec.order_id = self.broker.place_limit(
                self.config.symbol, side, price, self.config.qty, coid
            )
            self._live[rec.order_id] = rec

        self.orders_placed += 1
        self.records.append(rec)
        return rec

    # -- resolution ------------------------------------------------------
    def _states(self) -> dict[str, OrderState]:
        return {o.order_id: o for o in self.broker.get_open_orders(self.config.symbol)}

    def _resolve_fill(self, rec: ProbeRecord, st: OrderState, ts: float) -> None:
        fill_px = st.avg_fill_px if st.avg_fill_px is not None else rec.limit_px
        # interlock 4b: a resting order fills at its limit or better; worse means it took liquidity
        adverse = (rec.side == "buy" and fill_px > rec.limit_px + TICK / 2.0) or (
            rec.side == "sell" and fill_px < rec.limit_px - TICK / 2.0
        )
        if adverse:
            self.aborted_reason = (
                f"unexpected marketable fill: {rec.side} limit {rec.limit_px} filled at {fill_px}"
            )
            rec.resolve_reason = "aborted"
            rec.ts_resolve = ts
            rec.filled = True
            rec.fill_px = fill_px
            raise UnexpectedMarketableFill(self.aborted_reason)
        rec.filled = True
        rec.fill_px = fill_px
        rec.ts_resolve = ts
        rec.time_to_fill_s = round(ts - rec.ts_place, 3)
        rec.resolve_reason = "filled"
        self.fills += 1

    def watch(self, records: Sequence[ProbeRecord]) -> None:
        """Poll until each order fills or its horizon elapses, then cancel the rest.

        Each cancelled order is polled once more, so a fill that raced the cancel counts.
        """
        pending = {r.order_id: r for r in records if r.order_id is not None}
        if not pending:
            return
        deadline = max(r.ts_place + r.horizon_s for r in pending.values())
        while pending:
            states = self._states()
            now_ts = self._now_ts()
            for oid, rec in list(pending.items()):
                st = states.get(oid)
                if st is not None and st.is_filled:
                    self._resolve_fill(rec, st, now_ts)
                    rec.mid_at_resolve = self._safe_mid()
                    pending.pop(oid)
                    self._live.pop(oid, None)
                elif st is not None and st.status in ("canceled", "rejected"):
                    rec.filled = False
                    rec.ts_resolve = now_ts
                    rec.resolve_reason = st.status
                    rec.mid_at_resolve = self._safe_mid()
                    pending.pop(oid)
                    self._live.pop(oid, None)
            if not pending:
                break
            if now_ts >= deadline:
                break
            self.sleep(self.config.poll_interval_s)

        for oid, rec in list(pending.items()):
            self._cancel_and_settle(oid, rec, "horizon_cancel")

    def _cancel_and_settle(self, oid: str, rec: ProbeRecord, reason: str) -> None:
        try:
            self.broker.cancel(oid)
        except Exception as exc:  # noqa: BLE001 — a failed cancel must not lose the row
            rec.resolve_reason = f"cancel_error:{type(exc).__name__}"
        st = self._states().get(oid)
        now_ts = self._now_ts()
        if st is not None and st.is_filled:
            # the fill won the race with the cancel
            self._resolve_fill(rec, st, now_ts)
        else:
            rec.filled = False
            rec.ts_resolve = now_ts
            # keep a cancel_error reason: an unconfirmed cancel is not a clean expiry
            if not (rec.resolve_reason or "").startswith("cancel_error"):
                rec.resolve_reason = reason
        rec.mid_at_resolve = self._safe_mid()
        self._live.pop(oid, None)

    def cancel_all(self, reason: str = "session_cancel") -> int:
        """Cancel every live probe order. Called at 14:43 ICT and on any abort."""
        n = 0
        for oid, rec in list(self._live.items()):
            try:
                self._cancel_and_settle(oid, rec, reason)
            except UnexpectedMarketableFill:
                # already recorded as "aborted"; re-raising here would skip the log flush
                self._live.pop(oid, None)
            n += 1
        return n

    def _now_ts(self) -> float:
        return self.clock().timestamp()

    def _safe_mid(self) -> float | None:
        try:
            book = self.broker.get_book(self.config.symbol)
        except Exception:  # noqa: BLE001 — a feed hiccup must not lose the row
            return None
        self.mids.observe(book.ts, book.mid)
        return round(book.mid, 4)

    # -- driver ----------------------------------------------------------
    def run_session(self, max_rounds: int = 10) -> dict:
        """Run up to ``max_rounds`` passes of the (side, offset) plan, then flush the log.

        Budget caps and session limits end the run quietly and a summary dict is returned;
        ``UnexpectedMarketableFill`` is re-raised after cancel-all and a flush.
        """
        plan = [(side, off) for off in self.config.offsets_ticks for side in ("buy", "sell")]
        stop_reason = "plan_complete"
        rounds = 0
        try:
            while rounds < max_rounds:
                rounds += 1
                for i in range(0, len(plan), self.config.max_concurrent):
                    batch = plan[i : i + self.config.max_concurrent]
                    placed: list[ProbeRecord] = []
                    for side, off in batch:
                        now = self.clock()
                        if minute_of_day_ict(now) >= CANCEL_ALL_MIN and (
                            session_phase(now) == "afternoon"
                        ):
                            self.cancel_all("session_cancel")
                            stop_reason = "session_cutoff_1443"
                            raise _StopSession
                        try:
                            placed.append(self.place_probe(side, off))
                        except BudgetExhausted as exc:
                            stop_reason = str(exc)
                            raise _StopSession from None
                        except (OutsideSession, CrossingLimitRejected) as exc:
                            # a locked book skips this pair; a closed window ends the run
                            stop_reason = str(exc)
                            if isinstance(exc, OutsideSession):
                                raise _StopSession from None
                    self.watch(placed)
                    if self.config.spacing_s > 0:
                        self.sleep(self.config.spacing_s)
        except _StopSession:
            pass
        except UnexpectedMarketableFill:
            self.cancel_all("abort_marketable_fill")
            self.flush()
            raise

        self.cancel_all("session_cancel")
        written = self.flush()
        return {
            "orders_placed": self.orders_placed,
            "fills": self.fills,
            "rows_written": written,
            "stop_reason": stop_reason,
            "dry_run": self.dry_run,
            "log": str(self.config.log_path),
        }

    def flush(self) -> int:
        """Append every record collected so far, then clear the buffer."""
        rows = [r.to_row() for r in self.records]
        self.records = []
        if not rows:
            return 0
        return append_rows(self.config.log_path, rows)


class _StopSession(Exception):
    """Internal control-flow signal; never escapes ``run_session``."""


# --- DNSE plumbing ---
_DEFAULT_PAPER_BASE = "https://services.entrade.com.vn/papertrade-entrade-api"
_DEFAULT_AUTH_URL = "https://services.entrade.com.vn/dnse-auth-service/login"


class _HttpStatusError(ProbeError):
    """HTTP error response (status >= 400); the status lets ``cancel`` stay idempotent."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _jwt_claims(token: str) -> dict:
    """Decode a JWT payload without verifying the signature; ``{}`` if malformed.

    The token comes straight from the auth endpoint over TLS and only its claims are read.
    """
    import base64

    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:  # noqa: BLE001 — a malformed token is "no claims", not a crash
        return {}


def _jwt_expiry(token: str) -> float:
    """Unix expiry from the `exp` claim; 0 when absent (forces a re-login)."""
    try:
        return float(_jwt_claims(token).get("exp") or 0.0)
    except (TypeError, ValueError):
        return 0.0


# --- DNSE paper-trading binding ---
class DnseDemoBroker:
    """``BrokerPort`` adapter for the DNSE paper-trading (demo) API.

    Auth: ``POST {DNSE_AUTH_URL}`` with ``DNSE_USERNAME``/``DNSE_PASSWORD`` returns a JWT that
    alone authorises demo orders; ``investorId`` is read from its claims unless
    ``ENTRADE_INVESTOR_ID`` is set. Orders use ``{base}/derivative/orders`` (``POST``, ``GET``,
    ``DELETE /{id}``) with side ``NB``/``NS``, ``orderType`` ``LO`` and the investor's
    ``bankMarginPortfolioId``. The base (``ENTRADE_API_PAPER_BASE``) must be a papertrade URL;
    the book is read from the ingest service's Redis hash ``md:state:{symbol}``.
    """

    def __init__(self, account_no: str, symbol: str = "VN30F1M") -> None:
        if is_denied_account(account_no):
            raise RealAccountRefused(
                f"{mask_account(account_no)} is the real account — demo binding refuses it."
            )
        self.account_no = account_no
        self.symbol = symbol

        # paper-trading base only, checked before any order can be built
        self.base_url = os.environ.get("ENTRADE_API_PAPER_BASE", _DEFAULT_PAPER_BASE)
        if "papertrade" not in self.base_url:
            raise RealAccountRefused(
                f"ENTRADE_API_PAPER_BASE={self.base_url!r} is not a papertrade base. "
                "This probe never touches the live trading API."
            )
        self.order_type = os.environ.get("DNSE_PROBE_ORDER_TYPE", "LO").strip() or "LO"
        self.http_timeout_s = float(os.environ.get("PROBE_HTTP_TIMEOUT_S", "10"))
        self.book_max_age_s = float(os.environ.get("PROBE_BOOK_MAX_AGE_S", "15"))

        self._redis_host = os.environ.get(
            "PROBE_REDIS_HOST", os.environ.get("GATEWAY_REDIS_HOST", "localhost")
        )
        self._redis_port = int(
            os.environ.get("PROBE_REDIS_PORT", os.environ.get("GATEWAY_REDIS_PORT", "6379"))
        )
        self._redis_db = int(os.environ.get("PROBE_REDIS_DB", "0"))
        self._redis_client = None

        self._jwt: str = ""
        self._jwt_exp: float = 0.0
        self._investor_id_cache: int | None = None
        self._margin_id_cache: int | None = None
        self._venue_symbol_cache: str = ""

    # -- market data ------------------------------------------------------
    def get_book(self, symbol: str) -> BookSnapshot:
        """Top of book and depth ladder from the Redis hash ``md:state:{symbol}``.

        Reads the same book the market-data archive records, so every offset has a queue size.
        A book older than ``PROBE_BOOK_MAX_AGE_S`` (default 15 s) raises ``ProbeError``.
        """
        client = self._redis()
        raw = client.hgetall(f"md:state:{symbol}")
        if not raw:
            raise ProbeError(
                f"redis key md:state:{symbol} is empty or absent on "
                f"{self._redis_host}:{self._redis_port} — is the ingest service running?"
            )

        def _arr(field: str) -> list[float]:
            blob = raw.get(field)
            if not blob:
                return []
            try:
                return [float(x) for x in json.loads(blob)]
            except (ValueError, TypeError) as exc:
                raise ProbeError(f"md:state:{symbol}.{field} is not a JSON array: {exc}") from exc

        bid_px, bid_sz = _arr("bid_prices"), _arr("bid_volumes")
        ask_px, ask_sz = _arr("offer_prices"), _arr("offer_volumes")
        if not bid_px or not ask_px:
            raise ProbeError(
                f"md:state:{symbol} carries no book (bids={len(bid_px)}, asks={len(ask_px)}) — "
                "the session may not have opened yet."
            )

        try:
            updated = int(raw.get("updated_timestamp") or 0)
        except (TypeError, ValueError):
            updated = 0
        age = time.time() - updated if updated else float("inf")
        if age > self.book_max_age_s:
            raise ProbeError(
                f"book for {symbol} is {age:.1f}s old (limit {self.book_max_age_s:.0f}s). "
                "Refusing to price a resting limit off a stale book."
            )

        def _ladder(px: list[float], sz: list[float]) -> tuple[tuple[float, int], ...]:
            return tuple((p, int(s)) for p, s in zip(px, sz) if p > 0)

        bids, asks = _ladder(bid_px, bid_sz), _ladder(ask_px, ask_sz)
        return BookSnapshot(
            ts=float(updated) if updated else time.time(),
            best_bid=bids[0][0] if bids else bid_px[0],
            best_ask=asks[0][0] if asks else ask_px[0],
            bid_size=bids[0][1] if bids else None,
            ask_size=asks[0][1] if asks else None,
            bids=bids,
            asks=asks,
        )

    # -- orders -----------------------------------------------------------
    def place_limit(self, symbol: str, side: str, price: float, qty: int, coid: str) -> str:
        """POST a resting limit and return the venue order id.

        ``orderType`` defaults to ``LO`` (override: ``DNSE_PROBE_ORDER_TYPE``). Whether an ``LO``
        away from the touch actually rests is checked afterwards by :meth:`assert_resting`.
        """
        venue_symbol = self._venue_symbol(symbol)
        body = {
            "investorId": self._investor_id(),
            "symbol": venue_symbol,
            "side": "NB" if side == "buy" else "NS",
            "orderType": self.order_type,
            "price": float(price),
            "quantity": int(qty),
            "bankMarginPortfolioId": self._margin_portfolio_id(),
            # venue support unconfirmed; if echoed, it links venue orders to probe records
            "clientOrderId": coid,
        }
        payload = self._request("POST", "/derivative/orders", json_body=body)
        order_id = payload.get("id") or payload.get("orderId") or payload.get("order_id")
        if order_id is None:
            raise ProbeError(f"place returned no order id: {json.dumps(payload)[:400]}")
        return str(order_id)

    def cancel(self, order_id: str) -> None:
        """Cancel idempotently: 400/404/409 mean nothing is resting; other errors raise."""
        try:
            self._request("DELETE", f"/derivative/orders/{order_id}")
        except _HttpStatusError as exc:
            if exc.status in (400, 404, 409):
                return
            raise

    def get_open_orders(self, symbol: str | None = None) -> list[OrderState]:
        """Return the day's orders, terminal ones included (the resolution poll), normalised."""
        payload = self._request("GET", "/derivative/orders")
        rows = (
            payload if isinstance(payload, list) else payload.get("orders", payload.get("data", []))
        )
        out: list[OrderState] = []
        for row in rows or []:
            if symbol and row.get("symbol") not in (symbol, self._venue_symbol_cache):
                continue
            raw_side = str(row.get("side") or "")
            fill_qty = int(row.get("fillQuantity") or row.get("filledQuantity") or 0)
            avg = row.get("averagePrice")
            out.append(
                OrderState(
                    order_id=str(row.get("id") or row.get("orderId") or ""),
                    status=self._normalise_status(row.get("orderStatus")),
                    filled_qty=fill_qty,
                    avg_fill_px=float(avg) if avg not in (None, "", 0) else None,
                    side=("buy" if raw_side == "NB" else "sell" if raw_side == "NS" else None),
                    limit_px=float(row["price"]) if row.get("price") not in (None, "") else None,
                )
            )
        return out

    def assert_resting(self, order_id: str) -> OrderState:
        """Return the state of ``order_id``, failing closed if it did not rest.

        Call on the first order of the first live session. Raises ``UnexpectedMarketableFill``
        if it filled on arrival and ``ProbeError`` if it was rejected or is absent.
        """
        for state in self.get_open_orders():
            if state.order_id != str(order_id):
                continue
            if state.status == "filled":
                raise UnexpectedMarketableFill(
                    f"order {order_id} filled on arrival: orderType={self.order_type!r} does not "
                    "rest on this venue. Every fill-rate measurement downstream would be invalid."
                )
            if state.status == "rejected":
                raise ProbeError(
                    f"order {order_id} was REJECTED — orderType={self.order_type!r} or a required "
                    "field is wrong. Override with DNSE_PROBE_ORDER_TYPE once the venue's "
                    "vocabulary is known."
                )
            return state
        raise ProbeError(f"order {order_id} is absent from GET /derivative/orders")

    # -- internals --------------------------------------------------------
    @staticmethod
    def _normalise_status(raw: object) -> str:
        key = str(raw or "").strip().lower().replace("_", "").replace("-", "")
        return {
            "pendingnew": "pending",
            "new": "pending",
            "pending": "pending",
            "partiallyfilled": "partially_filled",
            "filled": "filled",
            "canceled": "canceled",
            "cancelled": "canceled",
            "rejected": "rejected",
            "expired": "canceled",
        }.get(key, "pending")

    def _redis(self):
        if self._redis_client is None:
            import redis  # optional dependency, imported on first use

            self._redis_client = redis.Redis(
                host=self._redis_host,
                port=self._redis_port,
                db=self._redis_db,
                decode_responses=True,
                socket_timeout=self.http_timeout_s,
            )
        return self._redis_client

    def _token(self) -> str:
        if self._jwt and time.time() < self._jwt_exp - 60:
            return self._jwt
        user = os.environ.get("DNSE_USERNAME", "")
        pwd = os.environ.get("DNSE_PASSWORD", "")
        if not user or not pwd:
            raise ProbeError("DNSE_USERNAME / DNSE_PASSWORD are required for a live probe.")
        import requests

        resp = requests.post(
            os.environ.get("DNSE_AUTH_URL", _DEFAULT_AUTH_URL),
            json={"username": user, "password": pwd},
            timeout=self.http_timeout_s,
        )
        if resp.status_code >= 400:
            raise ProbeError(f"auth failed {resp.status_code}: {resp.text[:200]}")
        token = (resp.json() or {}).get("token")
        if not token:
            raise ProbeError("auth response carried no token")
        self._jwt = str(token).strip()
        self._jwt_exp = _jwt_expiry(self._jwt)
        return self._jwt

    def _request(self, method: str, path: str, json_body: dict | None = None):
        import requests

        url = f"{self.base_url.rstrip('/')}{path}"
        resp = requests.request(
            method,
            url,
            json=json_body,
            headers={"Authorization": f"Bearer {self._token()}"},
            timeout=self.http_timeout_s,
        )
        if resp.status_code >= 400:
            raise _HttpStatusError(
                resp.status_code, f"{method} {path} -> {resp.status_code}: {resp.text[:300]}"
            )
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    def _investor_id(self) -> int:
        if self._investor_id_cache is not None:
            return self._investor_id_cache
        override = os.environ.get("ENTRADE_INVESTOR_ID", "").strip()
        if override:
            self._investor_id_cache = int(override)
            return self._investor_id_cache
        claims = _jwt_claims(self._token())
        for key in ("investorId", "investorIdStr"):
            if claims.get(key) not in (None, ""):
                self._investor_id_cache = int(claims[key])
                return self._investor_id_cache
        raise ProbeError("investorId absent from JWT claims; set ENTRADE_INVESTOR_ID")

    def _margin_portfolio_id(self) -> int:
        if self._margin_id_cache is not None:
            return self._margin_id_cache
        override = os.environ.get("ENTRADE_BANK_MARGIN_PORTFOLIO_ID", "").strip()
        if override:
            self._margin_id_cache = int(override)
            return self._margin_id_cache
        payload = self._request(
            "GET", f"/investors/{self._investor_id()}/derivative_margin_portfolios"
        )
        rows = payload if isinstance(payload, list) else payload.get("data", [])
        for row in rows or []:
            pid = row.get("bankMarginPortfolioId") or row.get("id")
            if pid:
                self._margin_id_cache = int(pid)
                return self._margin_id_cache
        raise ProbeError(
            "no bankMarginPortfolioId found; the venue answers order-400 without it. "
            "Set ENTRADE_BANK_MARGIN_PORTFOLIO_ID."
        )

    def _venue_symbol(self, display: str) -> str:
        """Resolve a display symbol such as ``VN30F1M`` to the venue's contract code.

        Uses ``DNSE_CONTRACT_SYMBOL_ID`` if set, else ``GET /derivatives``; never builds the code.
        """
        if self._venue_symbol_cache:
            return self._venue_symbol_cache
        pinned = os.environ.get("DNSE_CONTRACT_SYMBOL_ID", "").strip()
        if pinned:
            self._venue_symbol_cache = pinned
            return pinned
        payload = self._request("GET", "/derivatives")
        rows = payload if isinstance(payload, list) else payload.get("data", [])
        for row in rows or []:
            if str(row.get("displaySymbol") or row.get("name") or "") == display:
                code = row.get("symbol") or row.get("symbolId")
                if code:
                    self._venue_symbol_cache = str(code)
                    return self._venue_symbol_cache
        raise ProbeError(
            f"could not resolve {display} to a venue symbol from GET /derivatives; "
            "pin it with DNSE_CONTRACT_SYMBOL_ID"
        )


# --- CLI ---
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Resting-limit fill probe for the DNSE paper-trading account."
    )
    ap.add_argument("--account", default=os.environ.get("DNSE_ACCOUNT_NO", ""))
    ap.add_argument("--symbol", default="VN30F1M")
    ap.add_argument("--horizon", type=float, default=60.0, help="seconds, 5-60")
    ap.add_argument("--max-orders", type=int, default=20)
    ap.add_argument("--max-concurrent", type=int, default=2)
    ap.add_argument("--offsets", default="0,1,2")
    ap.add_argument("--log", default=str(DEFAULT_LOG_PATH))
    ap.add_argument("--fake", action="store_true", help="run the driver against FakeBroker")
    ap.add_argument("--verify", metavar="LOG", help="verify a probe log's hash chain and exit")
    # --live alone does not send: LIVE_ENV_VAR must also hold LIVE_ENV_VALUE
    ap.add_argument(
        "--live",
        action="store_true",
        help=(
            f"actually send orders. Requires {LIVE_ENV_VAR}={LIVE_ENV_VALUE!r} as well. "
            "Without this flag the probe is a DRY RUN and places nothing."
        ),
    )
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.verify:
        print(json.dumps(verify_chain(args.verify), indent=2))
        return 0

    cfg = ProbeConfig(
        account_no=args.account,
        symbol=args.symbol,
        horizon_s=args.horizon,
        max_orders_per_session=args.max_orders,
        max_concurrent=args.max_concurrent,
        offsets_ticks=tuple(int(x) for x in str(args.offsets).split(",") if x.strip()),
        log_path=Path(args.log),
    )
    if args.fake:
        from historical.research_lab.probes.fakes import FakeBroker

        broker: BrokerPort = FakeBroker()
    else:
        broker = DnseDemoBroker(cfg.account_no, cfg.symbol)

    probe = RestingLimitProbe(broker, cfg, dry_run=not args.live)
    print(json.dumps(probe.run_session(), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
