"""TPO point of control and value area per ICT session (feature prof_tpo_poc_pts_v1).

Mirrors ``build_tpo_profile_from_ticks`` in ``market-profile-engine`` and delegates the value
area to ``shared/quant_core/profile.py``. The engine returns ``(poc, vah, val)`` and quant_core
``(poc, val, vah)``, so values are bound to names when unpacked and ``val <= poc <= vah`` is
checked on every observation.

CLI (same output as ``l2_dump --feature prof_tpo_poc_pts_v1``)::

    python -m historical.l2.producers.prof_tpo \\
        --feature prof_tpo_poc_pts_v1 --tape <tape.csv> \\
        [--emit observation|profile] [--out <path>]
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Iterable, NamedTuple, Sequence

if __package__ in (None, ""):  # pragma: no cover - `python path/to/prof_tpo.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from historical.l2.prof_tape import PricePrint, ict_date_string, ict_day_key, read_price_tape
from shared.quant_core.profile import value_area

FEATURE = "prof_tpo_poc_pts_v1"
FEATURES = (FEATURE,)

#: ``market_types::PRICE_SCALE``: a ``Price`` is an i64 in ten-thousandths of a point.
PRICE_SCALE = 10_000
#: VN30F1M tick size (0.1 pt, ``historical.backtest_engine.costs.TICK``) in ``PRICE_SCALE`` units.
TICK_SIZE_SCALED = 1_000

#: Defaults declared in ``specs/features/prof_tpo_poc_pts_v1.yaml``; exposed as arguments so the
#: parity test can show that changing one changes the result.
DEFAULT_PRICE_BUCKET_TICK = 1  # `price_grouping` in the engine
DEFAULT_TPO_PERIOD_MIN = 30  # market_profile_engine::DEFAULT_TPO_BUCKET_MINUTES
DEFAULT_VA_PCT = 0.70  # market_profile_engine::DEFAULT_VALUE_AREA_PCT


class Observation(NamedTuple):
    """Scalar reduction of one session's TPO profile (an ``l2_observation`` row).

    Prices are integer ticks, exact by construction; the ``*_pts`` properties convert to points.
    """

    session: str
    prints: int
    levels: int
    tpo_total: int
    poc_tick: int
    val_tick: int
    vah_tick: int
    va_width_ticks: int

    @property
    def poc_pts(self) -> float:
        return self.poc_tick / 10.0

    @property
    def val_pts(self) -> float:
        return self.val_tick / 10.0

    @property
    def vah_pts(self) -> float:
        return self.vah_tick / 10.0

    @property
    def va_width_pts(self) -> float:
        return self.va_width_ticks / 10.0


class ProfileRow(NamedTuple):
    """One price level of a session's TPO histogram (an ``l2_profile`` row)."""

    session: str
    price_tick: int
    tpo_count: int


# --- bucketing, mirroring market-profile-engine ---


def _round_ties_away(x: float) -> int:
    """``f64::round``: nearest integer, ties away from zero (Python's ``round`` is banker's)."""
    floor = math.floor(x)
    frac = x - floor
    if frac > 0.5:
        return floor + 1
    if frac < 0.5:
        return floor
    return floor + 1 if x >= 0.0 else floor


def price_to_scaled(price: float) -> int:
    """``Price::from_f64``: points to ``PRICE_SCALE`` units, rounded like ``f64::round``."""
    if not math.isfinite(price):
        raise ValueError(f"non-finite price {price!r}")
    return _round_ties_away(price * PRICE_SCALE)


def bucket_tick(price: float, price_bucket_tick: int = DEFAULT_PRICE_BUCKET_TICK) -> int:
    """Bucket index of ``price``, as in the engine's ``bucket_price`` (which returns index × step).

    With ``price_bucket_tick = 1`` the index is the price in whole ticks. Rounds with
    ``(ratio + 0.5).floor()`` as the engine does here, not with ``f64::round``.
    """
    if price_bucket_tick <= 0:
        raise ValueError(f"price_bucket_tick must be > 0, got {price_bucket_tick}")
    step = TICK_SIZE_SCALED * price_bucket_tick
    ratio = price_to_scaled(price) / step
    return math.floor(ratio + 0.5) if ratio >= 0.0 else math.ceil(ratio - 0.5)


def tpo_period_id(epoch_s: int, tpo_period_min: int = DEFAULT_TPO_PERIOD_MIN) -> int:
    """TPO period index as in ``tpo_bucket_id``: UTC epoch seconds / period, truncated.

    The engine uses a zero-padded string id; only the number of distinct ids per level is used,
    so integers give the same result. Periods are UTC-anchored, like the engine's.
    """
    if tpo_period_min <= 0:
        raise ValueError(f"tpo_period_min must be > 0, got {tpo_period_min}")
    period = tpo_period_min * 60
    quotient = abs(epoch_s) // period
    return quotient if epoch_s >= 0 else -quotient


# --- profile and value area ---


def tpo_histogram(
    prints: Iterable[PricePrint],
    price_bucket_tick: int = DEFAULT_PRICE_BUCKET_TICK,
    tpo_period_min: int = DEFAULT_TPO_PERIOD_MIN,
) -> dict[int, int]:
    """``{price_tick: number of distinct TPO periods that traded there}``, ascending by tick.

    As in ``build_tpo_profile_from_ticks``, a TPO counts periods, not prints.
    """
    periods_by_tick: dict[int, set[int]] = {}
    for p in prints:
        tick = bucket_tick(p.price, price_bucket_tick)
        periods_by_tick.setdefault(tick, set()).add(tpo_period_id(p.epoch_s, tpo_period_min))
    return {tick: len(periods) for tick, periods in sorted(periods_by_tick.items())}


class ValueArea(NamedTuple):
    """Value area with named fields, so no caller depends on a tuple order."""

    poc_tick: int
    val_tick: int
    vah_tick: int


def value_area_ticks(histogram: dict[int, int], va_pct: float = DEFAULT_VA_PCT) -> ValueArea:
    """Value area of a TPO histogram, delegated to ``shared/quant_core/profile.py``.

    Delegating keeps a single Python definition of the POC and expansion tie-break rules.
    """
    area = value_area(histogram, va_pct)
    if area is None:
        raise ValueError("empty TPO profile has no value area")
    poc, val, vah = area  # quant_core order is (poc, val, vah)
    return ValueArea(poc_tick=int(poc), val_tick=int(val), vah_tick=int(vah))


def assert_band_invariants(obs: Observation) -> None:
    """Raise ``ValueError`` unless ``val <= poc <= vah`` and ``va_width == vah - val``.

    A value area contains its POC by construction, so a failure points to a VAH/VAL swap.
    """
    if not obs.val_tick <= obs.poc_tick <= obs.vah_tick:
        raise ValueError(
            f"{obs.session}: value area is not val <= poc <= vah "
            f"(val={obs.val_tick} poc={obs.poc_tick} vah={obs.vah_tick}). "
            f"The most likely cause is a VAH/VAL swap: market-profile-engine "
            f"returns (poc, vah, val) while three other implementations return "
            f"(poc, val, vah)."
        )
    if obs.va_width_ticks != obs.vah_tick - obs.val_tick:
        raise ValueError(f"{obs.session}: va_width {obs.va_width_ticks} is not vah - val")


# --- sessions ---


def group_by_session(prints: Iterable[PricePrint]) -> list[tuple[str, list[PricePrint]]]:
    """Split a tape into ICT calendar days, ascending; the lunch break does not split a session.

    Implements the spec's ``session: ict_full`` and ``seam_13h: none``.
    """
    by_day: dict[int, list[PricePrint]] = {}
    for p in prints:
        by_day.setdefault(ict_day_key(p.epoch_s), []).append(p)
    return [(ict_date_string(day), rows) for day, rows in sorted(by_day.items())]


def observations(
    prints: Iterable[PricePrint],
    price_bucket_tick: int = DEFAULT_PRICE_BUCKET_TICK,
    tpo_period_min: int = DEFAULT_TPO_PERIOD_MIN,
    va_pct: float = DEFAULT_VA_PCT,
) -> list[Observation]:
    """Per-session scalar reduction (``l2_observation`` rows), with band invariants checked."""
    out: list[Observation] = []
    for session, rows in group_by_session(prints):
        histogram = tpo_histogram(rows, price_bucket_tick, tpo_period_min)
        area = value_area_ticks(histogram, va_pct)
        obs = Observation(
            session=session,
            prints=len(rows),
            levels=len(histogram),
            tpo_total=sum(histogram.values()),
            poc_tick=area.poc_tick,
            val_tick=area.val_tick,
            vah_tick=area.vah_tick,
            va_width_ticks=area.vah_tick - area.val_tick,
        )
        assert_band_invariants(obs)
        out.append(obs)
    return out


def profile_rows(
    prints: Iterable[PricePrint],
    price_bucket_tick: int = DEFAULT_PRICE_BUCKET_TICK,
    tpo_period_min: int = DEFAULT_TPO_PERIOD_MIN,
) -> list[ProfileRow]:
    """Per-session TPO histogram by integer tick (``l2_profile`` rows)."""
    out: list[ProfileRow] = []
    for session, rows in group_by_session(prints):
        for tick, count in sorted(tpo_histogram(rows, price_bucket_tick, tpo_period_min).items()):
            out.append(ProfileRow(session=session, price_tick=tick, tpo_count=count))
    return out


def produce(
    tape: str | Path,
    feature: str = FEATURE,
    emit: str = "observation",
    price_bucket_tick: int = DEFAULT_PRICE_BUCKET_TICK,
    tpo_period_min: int = DEFAULT_TPO_PERIOD_MIN,
    va_pct: float = DEFAULT_VA_PCT,
) -> Sequence[Observation] | Sequence[ProfileRow]:
    """Compute the feature from a frozen price tape (the L2 write path's entry point)."""
    if feature != FEATURE:
        raise ValueError(f"unknown feature {feature!r}; known: {FEATURE}")
    prints = read_price_tape(tape)
    if emit == "observation":
        return observations(prints, price_bucket_tick, tpo_period_min, va_pct)
    if emit == "profile":
        return profile_rows(prints, price_bucket_tick, tpo_period_min)
    raise ValueError(f"unknown --emit {emit!r}; known: observation, profile")


# --- dump format: integers only, byte-identical to l2_dump ---

OBSERVATION_HEADER = (
    "session\tprints\tlevels\ttpo_total\tpoc_tick\tval_tick\tvah_tick\tva_width_ticks"
)
PROFILE_HEADER = "session\tprice_tick\ttpo_count"


def write_observations(rows: Iterable[Observation], sink) -> None:
    sink.write(OBSERVATION_HEADER + "\n")
    for r in rows:
        sink.write(
            f"{r.session}\t{r.prints}\t{r.levels}\t{r.tpo_total}\t"
            f"{r.poc_tick}\t{r.val_tick}\t{r.vah_tick}\t{r.va_width_ticks}\n"
        )


def write_profile(rows: Iterable[ProfileRow], sink) -> None:
    sink.write(PROFILE_HEADER + "\n")
    for r in rows:
        sink.write(f"{r.session}\t{r.price_tick}\t{r.tpo_count}\n")


def write_tsv(rows: Sequence, sink, emit: str = "observation") -> None:
    if emit == "observation":
        write_observations(rows, sink)
    elif emit == "profile":
        write_profile(rows, sink)
    else:
        raise ValueError(f"unknown emit {emit!r}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--feature", default=FEATURE, choices=FEATURES)
    ap.add_argument("--tape", required=True, help="frozen L0 price tape CSV")
    ap.add_argument("--emit", default="observation", choices=("observation", "profile"))
    ap.add_argument("--out", default=None, help="output path (default: stdout)")
    args = ap.parse_args(argv)

    rows = produce(args.tape, args.feature, args.emit)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            write_tsv(rows, fh, args.emit)
    else:
        write_tsv(rows, sys.stdout, args.emit)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
