"""Forward-accrual registry: one driver scores every declared strategy card per session.

Cards are data (``CardSpec``). ``accrue`` scores each card at the ``WORSE_SIDE`` and
``REALISTIC`` cost bars into its own append-only, hash-chained ledger and never rewrites a row.
Missing sessions are explicit (``build_session_index``), ``gate`` withholds statistics below
the pre-registered minimum, and every status reports the signals needed for 80 % power
(``N = 6.18 * sigma^2 / delta^2``). Card rules are frozen in ``PRE_REGISTRATION.md``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(REPO_ROOT))

# --- cost scenarios: frozen bars from costs.py (Python mirror of Rust FillSim, ADR 0023) ---
# Hard import: without the cost model the forward lane stops rather than price from a copy.
from historical.backtest_engine.costs import CostScenario

STATUTORY = CostScenario.STATUTORY
REALISTIC = CostScenario.REALISTIC
WORSE_SIDE = CostScenario.WORSE_SIDE
_COSTS_SOURCE = "historical.backtest_engine.costs"

# Every forward row is scored at both; the first (WORSE_SIDE) is the one the gate reads.
SCORED_SCENARIOS = (WORSE_SIDE, REALISTIC)


def _half_spread(scn) -> float:
    """Slippage per fill in points for a cost scenario, whatever its attribute is called."""
    for attr in (
        "slip_per_fill_points",  # costs.CostScenario
        "half_spread_pts",
        "slippage_pts_per_side",
        "half_spread",
        "slippage_pts",
    ):
        v = getattr(scn, attr, None)
        if v is not None:
            return float(v)
    raise TypeError(f"cost scenario {scn!r} exposes no half-spread attribute")


def _scn_name(scn) -> str:
    return str(getattr(scn, "name", scn))


# --- errors (typed, so "blocked" never reads as "no signal") ---
class ForwardError(RuntimeError):
    """Base for every forward-lane failure."""


class LedgerConflict(ForwardError):
    """A recomputed row differs from the stored row; the ledger is never overwritten."""


class LedgerTampered(ForwardError):
    """The `prev_hash` chain does not verify."""


class InsufficientWarmup(ForwardError):
    """The source is shorter than the card's required warmup, so the card cannot fire."""


class BlockedSource(ForwardError):
    """The card's data source is unavailable or empty (distinct from "no signal")."""


class StaleTape(ForwardError):
    """The tape's last ICT session is behind the last completed trading day."""


class MissingSessions(ForwardError):
    """The accrued session sequence is not contiguous against the calendar."""


LEDGER_VERSION = 2
GENESIS_HASH = "0" * 64
SESSIONS_PER_MONTH = 21.0  # VN trading sessions per month; converts session counts to months

# Business days that are not trading sessions (ISO strings): holidays derived from the tape
# plus announced future closures. Tape gaps are excluded, so they stay MISSING_DATA.
from historical.research_lab.calendar_vn import NON_TRADING_DAYS  # noqa: E402


# --- trade plans: what a card proposes, before costs ---
@dataclass(frozen=True)
class TradePlan:
    """One forward trade at mid prices, independent of the cost scenario.

    ``stop_dist`` is the adverse stop distance in points (``stop_mult * ATR``) or None;
    ``d1_high``/``d1_low`` are entry-day extremes for stop-first resolution. ``hold_days`` counts
    ICT day boundaries between fill and exit, as Rust ``FillSim`` does: 0 for C7 and X1
    (D+1 open to D+1 close), at least 1 for N1 (D close to D+1 open).
    """

    card_id: str
    date_D: str
    date_D1: str
    side: int  # +1 long / -1 short
    entry_mid: float
    exit_mid: float
    stop_dist: float | None = None
    d1_high: float | None = None
    d1_low: float | None = None
    hold_days: int = 0
    meta: dict = field(default_factory=dict)


def _round_trip_fee_points(price: float, hold_days: int = 0) -> float:
    """Round-trip fee in points at ``price``, with the position fee for each ICT day held."""
    from historical.backtest_engine.fee_hurdle import round_trip_fee_points

    return float(round_trip_fee_points(price, hold_days=int(hold_days)))


def score_plan(plan: TradePlan, scn) -> dict:
    """Resolve one plan under one cost scenario; prices and P&L in points.

    Both legs fill on the worse side by the scenario's half-spread. The stop is measured from
    the filled entry and, with daily bars, a touched stop is assumed hit first (conservative).
    """
    hs = _half_spread(scn)
    side = int(plan.side)
    entry = float(plan.entry_mid) + side * hs
    stop_px = None
    stopped = False
    if plan.stop_dist is not None and np.isfinite(plan.stop_dist) and plan.stop_dist > 0:
        stop_px = entry - side * float(plan.stop_dist)
        if side > 0:
            stopped = plan.d1_low is not None and float(plan.d1_low) <= stop_px
        else:
            stopped = plan.d1_high is not None and float(plan.d1_high) >= stop_px
    raw_exit = float(stop_px) if stopped else float(plan.exit_mid)
    exit_px = raw_exit - side * hs
    gross = (exit_px - entry) * side
    fee = _round_trip_fee_points(entry, plan.hold_days)
    return {
        "scenario": _scn_name(scn),
        "half_spread_pts": hs,
        "entry": round(entry, 3),
        "stop_px": round(float(stop_px), 3) if stop_px is not None else None,
        "exit": round(exit_px, 3),
        "exit_reason": "stop" if stopped else "close",
        "gross_pts": round(float(gross), 3),
        "fee_pts": round(float(fee), 3),
        "slip_pts": round(2.0 * hs, 3),
        "net_pts": round(float(gross - fee), 3),
    }


# --- card declaration ---
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# 6.18 = (z_0.05 + z_0.20)^2 = (1.6449 + 0.8416)^2: 80 % power at one-sided alpha 0.05.
POWER_CONST = 6.18
Z_ALPHA_ONE_SIDED = 1.6449

UNDEFINED_N = 2**31 - 1
"""Sentinel ``n_required`` when the edge at the promotion bar is <= 0 (no N resolves it)."""


@dataclass(frozen=True)
class CardSpec:
    """A forward candidate declared as data: plan function, warmup, gate and power inputs.

    ``plan_fn(daily, cutoff_ict_date) -> list[TradePlan]`` is the only card-specific code.
    Power inputs are pre-registered from the development sample, not read off forward rows.
    """

    card_id: str
    family: str
    source: str  # "m1_daily" | "basis_daily"
    ledger_name: str
    plan_fn: Callable[..., list[TradePlan]]
    required_warmup: int  # daily sessions before the signal can evaluate
    effect_pts: float  # delta: per-trade edge at the promotion bar (points)
    sigma_pts: float  # observed per-trade dispersion (points)
    signals_per_year: float  # expected fire rate (declared, from the frozen gate)
    cutoff_ict_date: str = "2026-08-14"
    min_signals: int = 60  # pre-registered gate (PRE_REGISTRATION.md)
    min_months: float = 6.0
    missing_data_budget: int = 0  # forward sessions allowed to be MISSING_DATA
    hit_min: float = 0.52
    note: str = ""

    # --- power ---
    def power_is_defined(self) -> bool:
        """False when the edge at the promotion bar is <= 0: no sample size resolves the card."""
        return self.effect_pts > 0

    def n_required(self) -> int:
        """Signals for 80 % power at one-sided alpha 0.05: ``N = 6.18 * sigma^2 / delta^2``.

        Returns ``UNDEFINED_N`` when delta <= 0; squaring would otherwise hide the sign.
        """
        if not self.power_is_defined():
            return UNDEFINED_N
        return int(round(POWER_CONST * self.sigma_pts**2 / self.effect_pts**2))

    def years_to_verdict(self) -> float:
        """Years to accrue ``n_required`` signals at the declared rate; inf when undefined."""
        if self.signals_per_year <= 0 or not self.power_is_defined():
            return float("inf")
        return self.n_required() / self.signals_per_year

    def power_at(self, n: int) -> float:
        """Achieved power of a one-sided t/z test at sample size `n`."""
        if n <= 0 or self.sigma_pts <= 0:
            return 0.0
        return _norm_cdf(self.effect_pts * math.sqrt(n) / self.sigma_pts - Z_ALPHA_ONE_SIDED)

    def power_summary(self) -> dict:
        defined = self.power_is_defined()
        return {
            "card": self.card_id,
            "effect_pts": self.effect_pts,
            "sigma_pts": self.sigma_pts,
            # a descriptive string, so the sentinel cannot be read as a sample size
            "n_required_80pct": (
                self.n_required() if defined else "UNDEFINED (delta<=0 at promotion bar)"
            ),
            "signals_per_year": self.signals_per_year,
            "years_to_verdict": (
                round(self.years_to_verdict(), 1)
                if defined
                else "UNDEFINED (delta<=0 at promotion bar)"
            ),
            "power_at_min_signals": round(self.power_at(self.min_signals), 3),
            "min_signals": self.min_signals,
            "power_defined": defined,
            "status_if_undefined": (
                None if defined else "UNDERPOWERED-OBSERVATIONAL (automatic, PRE_REGISTRATION §A8)"
            ),
        }


# --- plan builders for the registered cards (frozen rules) ---
def c7_plans(
    daily: pd.DataFrame,
    cutoff_ict_date: str = "2026-08-14",
    lookback: int = 60,
    pct: float = 0.80,
    atr_period: int = 14,
    stop_mult: float = 1.0,
) -> list[TradePlan]:
    """C7 short-term reversal plans.

    Fade day D's return when ``|ret_D|`` reaches the 80th percentile of the prior 60 sessions;
    enter at the D+1 open with a 1.0 x ATR(14) adverse stop, else exit at the D+1 close.
    """
    from historical.research_lab import lab

    sig = lab.reversal_daily_signal(daily, lookback=lookback, pct=pct)
    atr = lab.wilder_atr(daily, period=atr_period).to_numpy()
    dates = pd.to_datetime(sig["dt"]).dt.date.to_numpy()
    o, hi, lo, cl = (sig[c].to_numpy() for c in ("open", "high", "low", "close"))
    ret, thresh = sig["ret"].to_numpy(), sig["thresh"].to_numpy()
    fire, fade = sig["fire"].to_numpy(), sig["fade_dir"].to_numpy()
    cutoff = pd.Timestamp(cutoff_ict_date).date()

    plans: list[TradePlan] = []
    for i in range(len(sig) - 1):  # need D+1
        if not fire[i] or fade[i] == 0:
            continue
        stop_dist = atr[i]
        if not np.isfinite(stop_dist) or stop_dist <= 0:
            continue  # ATR not warm yet
        j = i + 1
        if dates[j] <= cutoff:
            continue  # entry day not yet in the forward window
        plans.append(
            TradePlan(
                card_id="C7",
                date_D=str(dates[i]),
                date_D1=str(dates[j]),
                side=int(fade[i]),
                entry_mid=float(o[j]),
                exit_mid=float(cl[j]),
                stop_dist=float(stop_mult) * float(stop_dist),
                d1_high=float(hi[j]),
                d1_low=float(lo[j]),
                meta={
                    "ret_D": round(float(ret[i]), 3),
                    "thresh": round(float(thresh[i]), 3),
                    "atr14": round(float(stop_dist), 3),
                },
            )
        )
    return plans


def n1_plans(
    daily: pd.DataFrame,
    cutoff_ict_date: str = "2026-08-14",
    rv_window: int = 20,
    volz_window: int = 60,
    extreme_q: float = 0.80,
) -> list[TradePlan]:
    """N1 volatility-conditioned overnight drift plans.

    On a causal high-volatility day, enter at the D close in the direction of
    ``sign(CLV - 0.5)`` and exit at the D+1 open.
    """
    d = daily.sort_index().copy()
    hi, lo, cl = (d[c].astype(float) for c in ("high", "low", "close"))
    rng = (hi - lo).replace(0, np.nan)
    clv = (cl - lo) / rng
    rv = (hi - lo).rolling(rv_window).mean()
    rv_z = (rv - rv.rolling(rv_window).mean()) / rv.rolling(rv_window).std()
    thr = rv_z.rolling(volz_window).quantile(extreme_q).shift(1)  # causal
    fire = (rv_z >= thr).fillna(False).to_numpy()

    dates = pd.to_datetime(d["dt"]).dt.date.to_numpy()
    close = cl.to_numpy()
    nxt_open = d["open"].shift(-1).to_numpy()
    clv_a = clv.to_numpy()
    rvz_a = rv_z.to_numpy()
    cutoff = pd.Timestamp(cutoff_ict_date).date()

    plans: list[TradePlan] = []
    for i in range(len(d) - 1):
        if not fire[i] or not np.isfinite(clv_a[i]):
            continue
        if dates[i] <= cutoff:  # entered at the D close
            continue
        if not np.isfinite(nxt_open[i]):
            continue
        plans.append(
            TradePlan(
                card_id="N1",
                date_D=str(dates[i]),
                date_D1=str(dates[i + 1]),
                side=1 if clv_a[i] >= 0.5 else -1,
                entry_mid=float(close[i]),
                exit_mid=float(nxt_open[i]),
                # overnight hold: position fee per ICT calendar day (Fri -> Mon = 3)
                hold_days=int((dates[i + 1] - dates[i]).days),
                meta={"clv": round(float(clv_a[i]), 3), "rv_z": round(float(rvz_a[i]), 3)},
            )
        )
    return plans


def n1_required_warmup(rv_window: int = 20, volz_window: int = 60) -> int:
    """Daily sessions N1 needs before its gate can evaluate: 99 at the frozen 20/60.

    First evaluable index ``2*(rv_window-1) + (volz_window-1) + 1`` (rolling mean, z-score,
    trailing quantile, then ``shift(1)``); the required row count is one more.
    """
    first_evaluable = 2 * (rv_window - 1) + (volz_window - 1) + 1
    return first_evaluable + 1


def x1_plans(
    daily_basis: pd.DataFrame,
    cutoff_ict_date: str = "2026-08-14",
    lookback: int = 20,
    extreme_q: float = 0.80,
) -> list[TradePlan]:
    """X1 basis-reversion plans.

    Fade a causal extreme-|z| end-of-day basis; enter at the D+1 open and exit at the D+1
    close with no stop. D -> D+1 pairs that cross a contract roll are excluded.
    """
    from historical.research_lab import lab
    from historical.research_lab.basis_screen import _flag_roll_boundary

    if daily_basis is None or len(daily_basis) == 0:
        raise BlockedSource(
            "BLOCKED: basis source is empty — `default.basis_m1` is a VIEW over an "
            "INNER JOIN of ohlcv_m1 x VN30INDEX; with nothing populating VN30INDEX "
            "the join returns no rows. This is NOT 'no signal'."
        )
    sig = lab.basis_signals(daily_basis, lookback=lookback, extreme_q=extreme_q)
    dates = pd.to_datetime(sig["d"]).dt.date.to_numpy()
    f_open, f_close = sig["f_open"].to_numpy(), sig["f_close"].to_numpy()
    basis_z = sig["basis_z"].to_numpy()
    would = sig["would_signal"].to_numpy()
    is_roll = _flag_roll_boundary(sig["d"])
    cutoff = pd.Timestamp(cutoff_ict_date).date()

    plans: list[TradePlan] = []
    for i in range(len(sig) - 1):
        if would[i] not in ("long", "short") or is_roll[i]:
            continue
        j = i + 1
        if dates[j] <= cutoff:
            continue
        entry_mid, exit_mid = float(f_open[j]), float(f_close[j])
        if not (np.isfinite(entry_mid) and np.isfinite(exit_mid)):
            continue
        plans.append(
            TradePlan(
                card_id="X1",
                date_D=str(dates[i]),
                date_D1=str(dates[j]),
                side=1 if would[i] == "long" else -1,
                entry_mid=entry_mid,
                exit_mid=exit_mid,
                meta={"basis_z": round(float(basis_z[i]), 3)},
            )
        )
    return plans


# --- registry: adding a card means adding one CardSpec here ---
CARDS: tuple[CardSpec, ...] = (
    CardSpec(
        card_id="C7",
        family="mean-reversion",
        source="m1_daily",
        ledger_name="c7_reversal_ledger.jsonl",
        plan_fn=c7_plans,
        # shifted 60-session quantile; ATR(14) must also warm up
        required_warmup=62,
        # Power inputs: frozen design at WORSE_SIDE, development sample (PRE_REGISTRATION.md).
        effect_pts=0.1385,
        sigma_pts=15.99,
        signals_per_year=55.0,  # measured on the development sample
        missing_data_budget=0,  # a missing session shifts the session-indexed rolling(60)
        note="fade prior-day extreme, 1.0xATR(14) stop; power inputs per §A9 (promotion bar)",
    ),
    CardSpec(
        card_id="N1",
        family="overnight",
        source="m1_daily",
        ledger_name="n1_overnight_ledger.jsonl",
        plan_fn=n1_plans,
        required_warmup=n1_required_warmup(20, 60),  # = 99
        # Power inputs: frozen causal design at WORSE_SIDE on the development sample
        # (PRE_REGISTRATION.md). delta <= 0, so n_required() returns UNDEFINED_N.
        effect_pts=-0.153646,
        sigma_pts=8.465032,
        signals_per_year=50.78,  # measured on the development sample
        missing_data_budget=0,
        note="high-vol night, side=sign(CLV-0.5), overnight hold; power UNDEFINED per §A13",
    ),
    CardSpec(
        card_id="X1",
        family="basis",
        source="basis_daily",
        ledger_name="x1_basis_ledger.jsonl",
        plan_fn=x1_plans,
        required_warmup=21,  # 20-session z + shift(1)
        # Power inputs unverified: delta is a hypothesis-generation figure and sigma an
        # estimate; re-measurement on the frozen design is pending (see PRE_REGISTRATION.md).
        effect_pts=1.468,
        sigma_pts=8.0,
        signals_per_year=48.2,
        missing_data_budget=0,
        note=(
            "fade extreme EOD basis z, enter D+1 open / exit D+1 close; "
            "POWER INPUTS UNVERIFIED -- hypothesis-generation-only delta (§A3) + estimated "
            "sigma; re-measurement owed (§A13)"
        ),
    ),
)

CARDS_BY_ID: dict[str, CardSpec] = {c.card_id: c for c in CARDS}


def register(card: CardSpec) -> None:
    """Add a card at runtime (tests, experiments); production cards belong in ``CARDS``."""
    global CARDS
    if card.card_id in CARDS_BY_ID:
        raise ValueError(f"card {card.card_id} already registered")
    CARDS = CARDS + (card,)
    CARDS_BY_ID[card.card_id] = card


# --- ledger: append-only, hash-chained ---
# Content fields. recorded_at, tape_sha256 and the hashes are provenance and change per run.
_CONTENT_KEYS = (
    "v",
    "card",
    "date_D",
    "date_D1",
    "side",
    "entry_mid",
    "exit_mid",
    "stop_dist",
    "d1_high",
    "d1_low",
    "hold_days",
    "signal",
    "scores",
)


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_digest(row: dict) -> str:
    return hashlib.sha256(_canon({k: row.get(k) for k in _CONTENT_KEYS}).encode()).hexdigest()


# Content keys added after v2 rows existed, with the value older rows imply. A stored row
# without the key matches only when the recomputed value equals that implied value.
_SCHEMA_ADDED_AFTER_V2: dict = {"hold_days": 0}


def _same_content(old: dict, new: dict) -> bool:
    """True when a stored row and a freshly recomputed one describe the same trade."""
    a = {k: old.get(k) for k in _CONTENT_KEYS}
    b = {k: new.get(k) for k in _CONTENT_KEYS}
    for key, implied in _SCHEMA_ADDED_AFTER_V2.items():
        if key not in old and b.get(key) == implied:
            a.pop(key, None)
            b.pop(key, None)
    return _canon(a) == _canon(b)


def row_hash(row: dict, prev_hash: str) -> str:
    """SHA-256 over ``prev_hash`` and the canonical JSON of the row (``row_hash`` excluded)."""
    payload = {k: v for k, v in row.items() if k != "row_hash"}
    payload["prev_hash"] = prev_hash
    return hashlib.sha256((prev_hash + "|" + _canon(payload)).encode()).hexdigest()


def row_key(row: dict) -> tuple:
    return (row.get("card"), row.get("date_D"), row.get("date_D1"))


def sha256_file(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_frame(df: pd.DataFrame) -> str:
    """Digest for a non-file source (e.g. a ClickHouse basis pull)."""
    return hashlib.sha256(
        df.to_csv(index=False).encode("utf-8", errors="replace")  # deterministic serialization
    ).hexdigest()


class _DirLock:
    """Advisory lock via atomic ``mkdir``; a stale lock is broken after ``timeout`` seconds."""

    def __init__(self, target: Path, timeout: float = 30.0):
        self.path = Path(str(target) + ".lock")
        self.timeout = timeout

    def __enter__(self):
        deadline = time.time() + self.timeout
        while True:
            try:
                self.path.mkdir(parents=False, exist_ok=False)
                return self
            except FileExistsError:
                if time.time() > deadline:
                    # break a stale lock rather than block accrual forever
                    try:
                        self.path.rmdir()
                        continue
                    except OSError:
                        raise ForwardError(f"could not acquire ledger lock {self.path}")
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            self.path.rmdir()
        except OSError:
            pass
        return False


def read_ledger(path: Path | str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def is_chained_ledger(path: Path | str) -> bool:
    """True when ``path`` already holds v2 chained rows."""
    rows = read_ledger(path)
    return bool(rows) and "row_hash" in rows[0]


def verify_chain(path: Path | str) -> dict:
    """Recompute the hash chain end to end; raise ``LedgerTampered`` at the first edited row."""
    rows = read_ledger(path)
    prev = GENESIS_HASH
    for i, r in enumerate(rows):
        if r.get("prev_hash") != prev:
            raise LedgerTampered(
                f"{path}: row {i} ({row_key(r)}) prev_hash mismatch "
                f"(stored {r.get('prev_hash')!r}, expected {prev!r})"
            )
        want = row_hash(r, prev)
        if r.get("row_hash") != want:
            raise LedgerTampered(
                f"{path}: row {i} ({row_key(r)}) row_hash mismatch — content was edited"
            )
        prev = want
    return {"path": str(path), "rows": len(rows), "head": prev, "ok": True}


# Migrated v1 rows carry only the worse-side score, so they are compared on v1 fields only.
_LEGACY_CONTENT_KEYS = ("card", "date_D", "date_D1", "side", "entry_mid", "signal")
_LEGACY_SCORE_KEYS = ("entry", "exit", "exit_reason", "gross_pts", "fee_pts", "net_pts")


def _legacy_digest(row: dict) -> str:
    core = {k: row.get(k) for k in _LEGACY_CONTENT_KEYS}
    ws = (row.get("scores") or {}).get(_scn_name(WORSE_SIDE), {})
    core["ws"] = {k: ws.get(k) for k in _LEGACY_SCORE_KEYS}
    return hashlib.sha256(_canon(core).encode()).hexdigest()


def migrate_legacy_ledger(path: Path | str, card_id: str) -> dict:
    """Upgrade a pre-chain v1 ledger to the v2 chained form; idempotent.

    Keeps a ``.v1.bak`` copy and every recorded number under the scenario it was recorded at
    (``WORSE_SIDE``); no ``REALISTIC`` score is back-filled.
    """
    p = Path(path)
    rows = read_ledger(p)
    if not rows or "row_hash" in rows[0]:
        return {"migrated": 0, "already_chained": bool(rows)}
    side_col, meta_cols, _ = _LEGACY_META_COLS[card_id]
    p.with_suffix(p.suffix + ".v1.bak").write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    out, prev = [], GENESIS_HASH
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for r in rows:
        rec = {
            "v": LEDGER_VERSION,
            "card": card_id,
            "date_D": r["date_D"],
            "date_D1": r["date_D1"],
            "side": int(r.get(side_col, 0)),
            "entry_mid": r.get("entry_mid"),
            "exit_mid": None,
            "stop_dist": None,
            "d1_high": None,
            "d1_low": None,
            "signal": {m: r.get(m) for m in meta_cols},
            "scores": {
                _scn_name(WORSE_SIDE): {
                    "scenario": _scn_name(WORSE_SIDE),
                    "half_spread_pts": 0.5,
                    "entry": r.get("entry"),
                    "stop_px": r.get("stop_px"),
                    "exit": r.get("exit"),
                    "exit_reason": r.get("exit_reason", "close"),
                    "gross_pts": r.get("gross_pts"),
                    "fee_pts": r.get("fee_pts"),
                    "slip_pts": 1.0,
                    "net_pts": r.get("net_pts"),
                }
            },
            "migrated_from": "v1",
            "recorded_at": stamp,
            "tape_sha256": "",
            "prev_hash": prev,
        }
        rec["row_hash"] = row_hash(rec, prev)
        prev = rec["row_hash"]
        out.append(rec)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text("".join(_canon(r) + "\n" for r in out), encoding="utf-8")
    os.replace(tmp, p)
    return {"migrated": len(out), "backup": str(p.with_suffix(p.suffix + ".v1.bak"))}


def make_row(plan: TradePlan, scenarios: Sequence = SCORED_SCENARIOS) -> dict:
    """Build the content part of a ledger row (no provenance or hashes)."""
    return {
        "v": LEDGER_VERSION,
        "card": plan.card_id,
        "date_D": plan.date_D,
        "date_D1": plan.date_D1,
        "side": int(plan.side),
        "entry_mid": round(float(plan.entry_mid), 3),
        "exit_mid": round(float(plan.exit_mid), 3),
        "stop_dist": round(float(plan.stop_dist), 3) if plan.stop_dist is not None else None,
        "d1_high": round(float(plan.d1_high), 3) if plan.d1_high is not None else None,
        "d1_low": round(float(plan.d1_low), 3) if plan.d1_low is not None else None,
        # fee basis stored with the row, so a later change of convention is detectable
        "hold_days": int(plan.hold_days),
        "signal": dict(plan.meta),
        "scores": {_scn_name(s): score_plan(plan, s) for s in scenarios},
    }


def append_rows(
    path: Path | str,
    rows: Iterable[dict],
    tape_sha256: str = "",
    recorded_at: str | None = None,
) -> dict:
    """Append rows to a chained, append-only ledger; return appended/skipped/total counts.

    Rows already stored with identical content are skipped; a changed row raises
    ``LedgerConflict`` (the source table is mutable). Each line is one ``os.write`` to an
    ``O_APPEND`` handle, under a directory lock.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    stamp = recorded_at or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    appended, skipped, conflicts = 0, 0, []
    with _DirLock(p):
        existing = read_ledger(p)
        if existing and "row_hash" not in existing[0]:
            raise LedgerConflict(
                f"{p} is a pre-chain v1 ledger. Run "
                "`forward_registry.migrate_legacy_ledger(path, card_id)` once (it keeps a "
                ".v1.bak and preserves every recorded number) before appending."
            )
        if existing:
            verify_chain(p)  # never extend a broken chain
        by_key = {row_key(r): r for r in existing}
        prev = existing[-1]["row_hash"] if existing else GENESIS_HASH
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            for content in rows:
                k = row_key(content)
                old = by_key.get(k)
                if old is not None:
                    if old.get("migrated_from") == "v1":
                        same = _legacy_digest(old) == _legacy_digest(content)
                        dig = _legacy_digest
                    else:
                        same = _same_content(old, content)
                        dig = content_digest
                    if same:
                        skipped += 1
                        continue
                    conflicts.append((k, dig(old), dig(content)))
                    continue
                rec = dict(content)
                rec["recorded_at"] = stamp
                rec["tape_sha256"] = tape_sha256
                rec["prev_hash"] = prev
                rec["row_hash"] = row_hash(rec, prev)
                os.write(fd, (_canon(rec) + "\n").encode("utf-8"))
                prev = rec["row_hash"]
                by_key[k] = rec
                appended += 1
        finally:
            os.close(fd)
    if conflicts:
        raise LedgerConflict(
            f"{p}: {len(conflicts)} recomputed row(s) DIFFER from what is already "
            f"recorded (first: {conflicts[0][0]}; stored {conflicts[0][1][:12]} vs "
            f"recomputed {conflicts[0][2][:12]}). The ledger is append-only — it is "
            "never rewritten. Investigate the mutable source tape before proceeding."
        )
    return {"appended": appended, "skipped": skipped, "total": len(by_key)}


def ledger_frame(path: Path | str, scenario=WORSE_SIDE) -> pd.DataFrame:
    """Flatten a v2 ledger to per-trade columns under one cost scenario; v1 rows pass through."""
    rows = read_ledger(path)
    if not rows:
        return pd.DataFrame()
    name = _scn_name(scenario)
    out = []
    for r in rows:
        if "scores" not in r:  # legacy v1 row (worse-side by construction)
            out.append(r)
            continue
        s = r["scores"].get(name)
        if s is None:
            continue
        out.append(
            {
                "card": r.get("card"),
                "date_D": r["date_D"],
                "date_D1": r["date_D1"],
                "side": r.get("side"),
                "scenario": name,
                "entry": s["entry"],
                "exit": s["exit"],
                "exit_reason": s["exit_reason"],
                "gross_pts": s["gross_pts"],
                "fee_pts": s["fee_pts"],
                "net_pts": s["net_pts"],
                "recorded_at": r.get("recorded_at"),
            }
        )
    return pd.DataFrame(out)


# --- legacy v1 row format ---
_LEGACY_META_COLS = {
    "C7": ("fade_dir", ["ret_D", "thresh", "atr14"], True),
    "N1": ("side", ["clv", "rv_z"], False),
    "X1": ("side", ["basis_z"], False),
}


def legacy_frame(plans: Sequence[TradePlan], card_id: str, scenario=WORSE_SIDE) -> pd.DataFrame:
    """Score plans under one scenario and return them in the legacy per-card v1 row format."""
    side_col, meta_cols, with_stop = _LEGACY_META_COLS[card_id]
    rows = []
    for p in plans:
        s = score_plan(p, scenario)
        row = {"v": 1, "date_D": p.date_D, "date_D1": p.date_D1, side_col: int(p.side)}
        for m in meta_cols:
            row[m] = p.meta.get(m)
        row["entry_mid"] = round(float(p.entry_mid), 3)
        row["entry"] = s["entry"]
        if with_stop:
            row["stop_px"] = s["stop_px"]
        row["exit"] = s["exit"]
        if with_stop:
            row["exit_reason"] = s["exit_reason"]
        row["gross_pts"] = s["gross_pts"]
        row["fee_pts"] = s["fee_pts"]
        row["net_pts"] = s["net_pts"]
        # no hold_days column: test_forward_registry pins the v1 columns; make_row records it
        rows.append(row)
    return pd.DataFrame(rows)


# --- session index: missing sessions are explicit ---
def expected_sessions(start, end, holidays: Iterable[str] = ()) -> list[dt.date]:
    """Expected trading sessions in [start, end]: business days minus non-trading days."""
    skip = set(NON_TRADING_DAYS) | {str(h) for h in holidays}
    days = pd.bdate_range(pd.Timestamp(start), pd.Timestamp(end))
    return [d.date() for d in days if str(d.date()) not in skip]


def build_session_index(
    tape_dates: Iterable[dt.date],
    start,
    end,
    fired_by_date: dict | None = None,
    holidays: Iterable[str] = (),
) -> list[dict]:
    """One row per expected session with ``status``, the cards that fired and ``is_expiry``.

    ``status`` is ``MISSING_DATA`` (no bar on the tape), ``accrued`` (a card entered a trade)
    or ``no_signal``. Missing sessions are flagged because they shift session-indexed windows.
    """
    from historical.research_lab.daily import third_thursday

    have = {d if isinstance(d, dt.date) else pd.Timestamp(d).date() for d in tape_dates}
    fired = fired_by_date or {}
    idx = pd.DatetimeIndex(sorted(have))
    expiry_days: set[dt.date] = set()
    for y, m in sorted({(t.year, t.month) for t in idx}):
        nominal = third_thursday(y, m)
        prior = idx[idx <= nominal]
        if len(prior) and (nominal - prior.max()).days <= 7:
            expiry_days.add(prior.max().date())

    rows = []
    for d in expected_sessions(start, end, holidays):
        cards = sorted(fired.get(d, fired.get(str(d), ())))
        if d not in have:
            status = "MISSING_DATA"
        elif cards:
            status = "accrued"
        else:
            status = "no_signal"
        rows.append(
            {
                "date": str(d),
                "status": status,
                "cards": cards,
                "is_expiry": d in expiry_days,
            }
        )
    return rows


def write_sessions(path: Path | str, rows: Sequence[dict]) -> Path:
    """Atomically rewrite the session index; it is derived data, so late bars can update it."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text("".join(_canon(r) + "\n" for r in rows), encoding="utf-8")
    os.replace(tmp, p)
    return p


def session_health(rows: Sequence[dict], since: str | None = None) -> dict:
    sel = [r for r in rows if since is None or r["date"] > since]
    missing = [r["date"] for r in sel if r["status"] == "MISSING_DATA"]
    return {
        "expected": len(sel),
        "accrued": sum(1 for r in sel if r["status"] == "accrued"),
        "no_signal": sum(1 for r in sel if r["status"] == "no_signal"),
        "missing_data": len(missing),
        "missing_dates": missing,
        "contiguous": not missing,
    }


def accrued_months(ledger: pd.DataFrame, sessions: Sequence[dict] | None = None) -> float:
    """Accrual duration in months, counted in sessions (``/ SESSIONS_PER_MONTH``).

    Counting sessions rather than calendar span means a data outage delays the verdict.
    """
    if sessions is not None:
        n = sum(1 for r in sessions if r["status"] in ("accrued", "no_signal"))
    elif ledger is None or len(ledger) == 0:
        n = 0
    else:
        n = int(pd.Series(ledger["date_D1"]).nunique())
    return n / SESSIONS_PER_MONTH


# --- gate: blinding, missing-data block, power ---
_SUPPRESSED_KEYS = (
    "hit_rate",
    "total_net_pts",
    "net_per_trade_pts",
    "gross_per_trade_pts",
    "months",
    "criteria",
    "by_scenario",
)


def _stats(df: pd.DataFrame) -> dict:
    net = df["net_pts"].astype(float)
    gross = df["gross_pts"].astype(float)
    return {
        "hit_rate": round(float((net > 0).mean()), 3),
        "total_net_pts": round(float(net.sum()), 2),
        "net_per_trade_pts": round(float(net.mean()), 3),
        "gross_per_trade_pts": round(float(gross.mean()), 3),
        "fee_per_trade_pts": round(float(df["fee_pts"].astype(float).mean()), 3),
    }


def log_peek(peek_log: Path | str, card_id: str, n: int, reason: str = "") -> int:
    """Append an unblinded look to the peek log and return its touch number.

    ``PRE_REGISTRATION.md`` allows at most two holdout touches.
    """
    p = Path(peek_log)
    p.parent.mkdir(parents=True, exist_ok=True)
    touch = len(read_ledger(p)) + 1
    rec = {
        "touch": touch,
        "card": card_id,
        "n_at_peek": n,
        "reason": reason,
        "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, (_canon(rec) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return touch


def gate(
    ledger: pd.DataFrame,
    card: CardSpec,
    sessions: Sequence[dict] | None = None,
    force_peek: bool = False,
    peek_log: Path | str | None = None,
    by_scenario: dict | None = None,
) -> dict:
    """Apply the pre-registered accrual gate to one card's ledger.

    More ``MISSING_DATA`` sessions than the card's budget gives ``BLOCKED_MISSING_DATA``.
    Below ``min_signals`` or ``min_months`` only ``{"n": n, "verdict": "ACCRUING"}`` is
    returned, unless ``force_peek`` unblinds it (recorded in ``peek_log``). Past the gate it is
    ``FORWARD-POSITIVE`` (net > 0 and hit rate > ``hit_min``) or ``KILL``.
    """
    n = int(len(ledger)) if ledger is not None else 0
    health = session_health(sessions, since=card.cutoff_ict_date) if sessions else None
    if health and health["missing_data"] > card.missing_data_budget:
        res = {
            "n": n,
            "verdict": "BLOCKED_MISSING_DATA",
            "missing_sessions": health["missing_data"],
            "missing_dates": health["missing_dates"],
            "missing_budget": card.missing_data_budget,
        }
        return res

    months = accrued_months(ledger, sessions)
    enough = n >= card.min_signals and months >= card.min_months
    if not enough and not force_peek:
        # blinded: no statistics below the gate
        return {"n": n, "verdict": "ACCRUING"}

    res: dict = {"n": n, "months": round(months, 1)}
    if n:
        res.update(_stats(ledger))
        if by_scenario:
            res["by_scenario"] = by_scenario
    if enough:
        passes = bool(res.get("total_net_pts", 0) > 0 and res.get("hit_rate", 0) > card.hit_min)
        res["verdict"] = "FORWARD-POSITIVE" if passes else "KILL"
    else:
        res["verdict"] = "ACCRUING"
    if force_peek:
        res["unblinded_peek"] = True
        if peek_log is not None:
            res["peek_touch"] = log_peek(peek_log, card.card_id, n, reason="--force-peek")
    return res


def card_status(card: CardSpec, gate_res: dict) -> dict:
    """Merge the gate result with the card's power summary and its power at the current n."""
    out = {"card": card.card_id, "family": card.family, **gate_res, **card.power_summary()}
    n = int(gate_res.get("n", 0))
    out["power_at_n"] = round(card.power_at(n), 4)
    out["underpowered_at_gate"] = card.power_at(card.min_signals) < 0.5
    return out


def format_status(st: dict) -> str:
    lines = [f"[{st['card']}] {st.get('verdict')} — n={st.get('n', 0)}"]
    if "missing_sessions" in st:
        lines.append(
            f"  MISSING_DATA {st['missing_sessions']} > budget {st['missing_budget']}: "
            f"{', '.join(st['missing_dates'][:8])}"
        )
    if "hit_rate" in st:
        lines.append(
            f"  hit {st['hit_rate']:.0%} | net {st['total_net_pts']:+.2f}pt "
            f"({st['net_per_trade_pts']:+.3f}/trade) | gross {st['gross_per_trade_pts']:+.3f}/trade"
        )
        for scn, s in (st.get("by_scenario") or {}).items():
            lines.append(
                f"    {scn:<10} net {s['total_net_pts']:+.2f}pt "
                f"({s['net_per_trade_pts']:+.3f}/trade) | hit {s['hit_rate']:.0%}"
            )
    if st.get("unblinded_peek"):
        lines.append(f"  *** UNBLINDED PEEK (touch #{st.get('peek_touch', '?')}) ***")
    # n_required_80pct and years_to_verdict are strings when power is undefined
    if not st.get("power_defined", True):
        lines.append(
            f"  POWER: delta {st['effect_pts']}pt, sigma {st['sigma_pts']}pt -> "
            f"N_required(80%) = {st['n_required_80pct']} "
            f"-- no sample size answers this question; "
            f"{st.get('status_if_undefined') or 'UNDERPOWERED-OBSERVATIONAL'}"
        )
    else:
        lines.append(
            f"  POWER: delta {st['effect_pts']}pt, sigma {st['sigma_pts']}pt -> "
            f"N_required(80%) = {st['n_required_80pct']:,} signals "
            f"~ {st['years_to_verdict']:.1f} years at {st['signals_per_year']}/yr; "
            f"power at the n>={st['min_signals']} gate = {st['power_at_min_signals']:.0%}"
        )
    if st.get("underpowered_at_gate"):
        lines.append(
            "  ^^ the pre-registered gate has power ~= alpha: it CANNOT distinguish "
            "the claimed edge from zero. This is not 'accruing evidence'."
        )
    return "\n".join(lines)


# --- driver: one pass over every registered card ---
def accrue(
    sources: dict,
    ledger_dir: Path | str,
    cards: Sequence[CardSpec] = CARDS,
    tape_digests: dict | None = None,
    strict_warmup: bool = True,
    scenarios: Sequence = SCORED_SCENARIOS,
) -> dict:
    """Score every card into its own ledger in one pass over the sources.

    ``sources`` maps ``CardSpec.source`` (``"m1_daily"``, ``"basis_daily"``) to a daily frame.
    A card that raises ``ForwardError`` (missing or short source, ledger conflict) is marked
    ``BLOCKED`` without stopping the others. Returns ``cards`` (status per card),
    ``fired_by_date`` and ``costs_source``.
    """
    ldir = Path(ledger_dir)
    ldir.mkdir(parents=True, exist_ok=True)
    digests = tape_digests or {}
    out: dict[str, dict] = {}
    fired_by_date: dict[str, set] = {}

    for card in cards:
        src = sources.get(card.source)
        try:
            if src is None or len(src) == 0:
                raise BlockedSource(
                    f"BLOCKED: source '{card.source}' unavailable or empty for {card.card_id}"
                )
            if strict_warmup and len(src) < card.required_warmup + 1:
                raise InsufficientWarmup(
                    f"{card.card_id}: source '{card.source}' has {len(src)} sessions but the "
                    f"frozen cell needs {card.required_warmup} warmup + 1 = "
                    f"{card.required_warmup + 1}. The card CANNOT fire as configured — "
                    "widen the export window (do NOT retune the cell)."
                )
            plans = card.plan_fn(src, card.cutoff_ict_date)
            rows = [make_row(p, scenarios) for p in plans]
            path = ldir / card.ledger_name
            wrote = append_rows(path, rows, tape_sha256=digests.get(card.source, ""))
            for p in plans:
                fired_by_date.setdefault(p.date_D1, set()).add(card.card_id)
            per_scn = {}
            for scn in scenarios:
                df = ledger_frame(path, scn)
                per_scn[_scn_name(scn)] = _stats(df) if len(df) else {}
            primary = ledger_frame(path, scenarios[0])
            g = gate(primary, card, by_scenario=per_scn if len(primary) else None)
            st = card_status(card, g)
            st.update(
                {
                    "status": "ACCRUED" if wrote["appended"] else "NO_NEW_SIGNAL",
                    "appended": wrote["appended"],
                    "skipped": wrote["skipped"],
                    "ledger": str(path),
                    "warmup_sessions": len(src),
                    "required_warmup": card.required_warmup,
                }
            )
        except ForwardError as e:
            st = card_status(card, {"n": 0, "verdict": "BLOCKED"})
            st.update({"status": "BLOCKED", "error": f"{type(e).__name__}: {e}"})
        out[card.card_id] = st

    return {
        "cards": out,
        "fired_by_date": {k: sorted(v) for k, v in fired_by_date.items()},
        "costs_source": _COSTS_SOURCE,
    }


def power_table(cards: Sequence[CardSpec] = CARDS) -> pd.DataFrame:
    """Power summary of each card as a DataFrame."""
    return pd.DataFrame([c.power_summary() for c in cards])


def main(argv: list[str]) -> int:
    """`python historical/research_lab/forward_registry.py [--power] [--verify <ledger>]`"""
    if "--verify" in argv:
        print(json.dumps(verify_chain(argv[argv.index("--verify") + 1]), indent=2))
        return 0
    tbl = power_table()
    print(f"Registered forward cards ({len(CARDS)}) | costs from: {_COSTS_SOURCE}")
    print(tbl.to_string(index=False))
    print(
        "\nN_required = 6.18 * sigma^2 / delta^2 (80% power, one-sided alpha=0.05).\n"
        "A card whose power at its own n-gate is ~5-6% is testing nothing."
    )
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
