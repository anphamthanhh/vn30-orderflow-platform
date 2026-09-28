"""Order-book cost features: quoted spread, time-of-day spread ratio, microprice and depth.

Each value is a function of one book state, since the source stamps whole seconds and updates
within a second have no order. For each ICT minute with an update, the spec's ``agg`` param
(``last`` or ``first``; ``median`` is not in ``feature_boundary``'s ``agg`` set) picks the
state; minutes without an update produce no row. The features measure entry cost, not direction.

CLI::

    python -m historical.l2.producers.book_cost \\
        --feature ob_depth_resid_lot_v1 --book <book.csv> [--out <path>]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable, Iterable, NamedTuple, Sequence

if __package__ in (None, ""):  # pragma: no cover - `python path/to/book_cost.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from historical.l2.book import BookState, group_by_minute, minute_of_session, read_book
from historical.l2.tape import ict_day_key

#: Features produced here; each has its own spec in ``specs/features/``.
FEATURES = (
    "ob_spread_pts_v1",
    "ob_spread_tod_ratio_v1",
    "ob_microprice_pts_v1",
    "ob_depth_top10_lot_v1",
    "ob_depth_resid_lot_v1",
    "ob_depth_resid_imb_ratio_v1",
)

#: Implemented intra-minute reductions, a subset of ``feature_boundary``'s ``agg`` values.
AGGREGATORS: dict[str, Callable[[Sequence[BookState]], BookState]] = {
    "last": lambda states: states[-1],
    "first": lambda states: states[0],
}


class Row(NamedTuple):
    """One L2 observation: the value of a feature for one ICT minute."""

    minute: int  #: ICT minute index (``minute_key``), the row's identity
    epoch_s: int  #: event time of the book state the value was taken from
    value: float


# --- per-state formulas: pure functions of one book state ---
def _sides(state: BookState, levels: int) -> tuple[int, int]:
    """Sum bid and offer volumes over the first ``levels`` levels (exact integer sums)."""
    if levels < 1 or levels > state.levels:
        raise ValueError(
            f"levels={levels} but this book row carries {state.levels}; a feature that "
            f"silently reads fewer levels than it declares is not the feature it names"
        )
    return sum(state.bid_volumes[:levels]), sum(state.offer_volumes[:levels])


def spread_pts(state: BookState) -> float:
    """Best offer minus best bid in points; negative for a crossed book (not clamped).

    Same sign convention as ``spread_from_best_levels`` in ``market-dom-engine``.
    """
    return state.offer_prices[0] - state.bid_prices[0]


def microprice_pts(state: BookState) -> float:
    """Stoikov (2018) microprice: ``(q_ask*P_bid + q_bid*P_ask) / (q_ask + q_bid)``.

    The weights are crossed: a larger ask queue pulls the value toward the bid. Falls back to the
    arithmetic mid when both top-of-book queues are empty.
    """
    p_bid, p_ask = state.bid_prices[0], state.offer_prices[0]
    q_bid, q_ask = state.bid_volumes[0], state.offer_volumes[0]
    denom = q_ask + q_bid
    if denom == 0:
        return (p_bid + p_ask) / 2.0
    return (q_ask * p_bid + q_bid * p_ask) / denom


def depth_top10_lot(state: BookState, levels: int = 10) -> float:
    """Per-side depth: ``0.5 * (sum(bid_volumes) + sum(offer_volumes))`` over ``levels`` levels."""
    bid, off = _sides(state, levels)
    return 0.5 * (bid + off)


def hidden_sides_lot(state: BookState, levels: int = 10) -> tuple[int, int]:
    """Size beyond the first ``levels`` levels per side: ``total_*_qty - sum(*_volumes)``.

    Exact integers, not clamped: a negative value means the feed's whole-book total contradicts
    its visible levels.
    """
    bid, off = _sides(state, levels)
    return state.total_bid_qty - bid, state.total_offer_qty - off


def hidden_lot(state: BookState, levels: int = 10) -> float:
    """``0.5 * (hidden_bid + hidden_ask)``: per-side size resting beyond the visible levels."""
    hb, ha = hidden_sides_lot(state, levels)
    return 0.5 * (hb + ha)


def hidden_imb_ratio(state: BookState, levels: int = 10) -> float:
    """``(hidden_bid - hidden_ask) / (hidden_bid + hidden_ask)`` in ``[-1, 1]``.

    Uses only size beyond the visible levels, unlike whole-book imbalance measures. Returns 0.0
    when there is no hidden size on either side.
    """
    hb, ha = hidden_sides_lot(state, levels)
    denom = hb + ha
    if denom == 0:
        return 0.0
    return (hb - ha) / denom


#: Per-state formula of each feature, called as ``formula(state, levels)``.
_PER_STATE: dict[str, Callable[[BookState, int], float]] = {
    "ob_spread_pts_v1": lambda st, levels: spread_pts(st),
    "ob_microprice_pts_v1": lambda st, levels: microprice_pts(st),
    "ob_depth_top10_lot_v1": depth_top10_lot,
    "ob_depth_resid_lot_v1": hidden_lot,
    "ob_depth_resid_imb_ratio_v1": hidden_imb_ratio,
}

#: ``params.default.levels`` of each spec, mirrored so the CLI needs no YAML parser;
#: ``test_l2_book_cost.py`` checks the table against the specs.
DEFAULT_LEVELS: dict[str, int] = {
    "ob_spread_pts_v1": 1,
    "ob_spread_tod_ratio_v1": 1,
    "ob_microprice_pts_v1": 1,
    "ob_depth_top10_lot_v1": 10,
    "ob_depth_resid_lot_v1": 10,
    "ob_depth_resid_imb_ratio_v1": 10,
}


# --- minute reduction ---
def reduce_minutes(
    states: Sequence[BookState],
    feature: str,
    *,
    levels: int | None = None,
    agg: str = "last",
) -> list[Row]:
    """Evaluate ``feature`` once per ICT minute with a book update, on the state ``agg`` picks.

    Picking one state (rather than averaging per-state values) keeps each value tied to a book
    that existed.
    """
    if feature not in _PER_STATE:
        raise ValueError(f"{feature!r} has no per-state formula; known: {sorted(_PER_STATE)}")
    if agg not in AGGREGATORS:
        raise ValueError(
            f"agg={agg!r} is not supported by this producer (it implements "
            f"{sorted(AGGREGATORS)}); 'median' is not in feature_boundary's closed "
            f"`agg` vocabulary either -- see the module docstring"
        )
    lv = DEFAULT_LEVELS[feature] if levels is None else levels
    pick = AGGREGATORS[agg]
    formula = _PER_STATE[feature]
    out: list[Row] = []
    for key, bucket in group_by_minute(states):
        state = pick(bucket)
        out.append(Row(minute=key, epoch_s=state.epoch_s, value=float(formula(state, lv))))
    return out


def _median(values: Sequence[float]) -> float:
    """Median; for an even count, the mean of the two middle values."""
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        raise ValueError("median of an empty sequence")
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def spread_tod_ratio(
    states: Sequence[BookState],
    *,
    agg: str = "last",
    baseline_sessions: int = 20,
    baseline_min_sessions: int = 5,
) -> list[Row]:
    """Quoted spread divided by its baseline for the same minute of session.

    The baseline is the median ``ob_spread_pts_v1`` at that minute over the most recent
    ``baseline_sessions`` earlier sessions that have it: causal, and it absorbs the regular
    intraday pattern such as the widening into the ATC. No row is emitted with fewer than
    ``baseline_min_sessions`` prior values or a baseline <= 0.
    """
    per_minute = reduce_minutes(states, "ob_spread_pts_v1", agg=agg)

    history: dict[int, list[float]] = {}
    last_day: int | None = None
    pending: list[tuple[int, float]] = []
    out: list[Row] = []

    def _flush(day_rows: list[tuple[int, float]]) -> None:
        for mos, value in day_rows:
            history.setdefault(mos, []).append(value)
            if len(history[mos]) > baseline_sessions:
                del history[mos][0]

    for row in per_minute:
        day = ict_day_key(row.epoch_s)
        if last_day is not None and day != last_day:
            _flush(pending)
            pending = []
        last_day = day
        mos = minute_of_session(row.epoch_s)
        prior = history.get(mos, [])
        if len(prior) >= baseline_min_sessions:
            base = _median(prior)
            if base > 0:
                out.append(Row(minute=row.minute, epoch_s=row.epoch_s, value=row.value / base))
        pending.append((mos, row.value))
    return out


def produce(
    book: str | Path,
    feature: str = "ob_spread_pts_v1",
    *,
    levels: int | None = None,
    agg: str = "last",
    baseline_sessions: int = 20,
    baseline_min_sessions: int = 5,
) -> list[Row]:
    """Compute one feature from a frozen order-book CSV (the L2 write path's entry point)."""
    if feature not in FEATURES:
        raise ValueError(f"unknown feature {feature!r}; known: {', '.join(FEATURES)}")
    states = read_book(book)
    if feature == "ob_spread_tod_ratio_v1":
        return spread_tod_ratio(
            states,
            agg=agg,
            baseline_sessions=baseline_sessions,
            baseline_min_sessions=baseline_min_sessions,
        )
    return reduce_minutes(states, feature, levels=levels, agg=agg)


def write_tsv(rows: Iterable[Row], sink) -> None:
    """Write ``minute<TAB>epoch_s<TAB>value`` rows; ``repr`` floats round-trip exactly."""
    sink.write("minute\tepoch_s\tvalue\n")
    for r in rows:
        sink.write(f"{r.minute}\t{r.epoch_s}\t{r.value!r}\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--feature", default="ob_spread_pts_v1", choices=FEATURES)
    ap.add_argument("--book", required=True, help="frozen L0 order-book CSV")
    ap.add_argument("--agg", default="last", choices=sorted(AGGREGATORS))
    ap.add_argument("--levels", type=int, default=None)
    ap.add_argument("--baseline-sessions", type=int, default=20)
    ap.add_argument("--baseline-min-sessions", type=int, default=5)
    ap.add_argument("--out", default=None, help="output path (default: stdout)")
    args = ap.parse_args(argv)

    rows = produce(
        args.book,
        args.feature,
        levels=args.levels,
        agg=args.agg,
        baseline_sessions=args.baseline_sessions,
        baseline_min_sessions=args.baseline_min_sessions,
    )
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            write_tsv(rows, fh)
    else:
        write_tsv(rows, sys.stdout)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
