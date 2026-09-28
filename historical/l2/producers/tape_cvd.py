"""Cumulative volume delta from the trade tape (features tape_cvd_lot_v1, tape_cvd_norst_lot_v1).

``tape_cvd_lot_v1`` resets at the ICT day boundary and rolls over at 13:00 (level kept, history
cleared), as ``signal-engine`` does; ``tape_cvd_norst_lot_v1`` never resets. An unclassified
aggressor (``'U'``) contributes zero, as in ``DeltaCalculator::on_tick``.
``historical/tests/test_l2_parity_cvd.py`` compares the output with ``l2_dump`` at zero tolerance.

CLI (same output as the Rust ``l2_dump``)::

    python -m historical.l2.producers.tape_cvd \\
        --feature tape_cvd_lot_v1 --tape <tape.csv> [--out <path>]
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Iterable, Iterator, NamedTuple

if __package__ in (None, ""):  # pragma: no cover - `python path/to/tape_cvd.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from historical.l2.tape import Print, ict_day_key, ict_second_of_day, read_tape

#: Start of the afternoon session, 13:00 ICT, in seconds after midnight.
LUNCH_RETURN_SECONDS = 13 * 3600

#: Aggressor spellings recognised by ``DeltaCalculator::on_tick``; any other value, ``'U'``
#: included, contributes zero.
_BUY_SIDES = frozenset({"B", "BUY", "Buy", "buy"})
_SELL_SIDES = frozenset({"S", "SELL", "Sell", "sell"})

FEATURES = ("tape_cvd_lot_v1", "tape_cvd_norst_lot_v1")


class Step(NamedTuple):
    """The CVD after applying the print at ``idx``."""

    idx: int
    epoch_s: int
    cvd: int


def tick_delta(volume: float, aggressor: str) -> float:
    """Signed volume of one print, as ``DeltaCalculator::on_tick`` computes it.

    Negative or non-finite volume counts as zero; an unclassified aggressor gives zero delta.
    """
    v = volume if (math.isfinite(volume) and volume >= 0.0) else 0.0
    if aggressor in _BUY_SIDES:
        return v
    if aggressor in _SELL_SIDES:
        return -v
    return 0.0


def _exact_integer(value: float, idx: int) -> int:
    """Return the CVD as an ``int``; a non-integral value is an input error and is not rounded."""
    if not math.isfinite(value) or value != int(value):
        raise ValueError(f"row {idx}: CVD {value!r} is not an exact integer")
    return int(value)


def cvd_steps(prints: Iterable[Print], feature: str = "tape_cvd_lot_v1") -> Iterator[Step]:
    """Per-print CVD under the policy of ``feature``; session watermarks as in ``l2_dump``.

    The first day of a run does not reset (the accumulator starts at zero), and a day change and
    a 13:00 crossing never fire on the same print. The 13:00 rollover keeps the level, so unlike
    a reset to zero it leaves the values unchanged.
    """
    if feature not in FEATURES:
        raise ValueError(f"unknown feature {feature!r}; known: {', '.join(FEATURES)}")
    session_policy = feature == "tape_cvd_lot_v1"

    cumulative = 0.0
    initial = 0.0
    last_day_key: int | None = None
    last_sec_in_day = 0

    for idx, p in enumerate(prints):
        if session_policy:
            day_key = ict_day_key(p.epoch_s)
            sec_in_day = ict_second_of_day(p.epoch_s)
            if last_day_key != day_key:
                if last_day_key is not None:
                    # CvdTracker::reset()
                    cumulative = 0.0
                    initial = 0.0
                last_day_key = day_key
            elif last_sec_in_day < LUNCH_RETURN_SECONDS <= sec_in_day:
                # CvdTracker::rollover(): keep the level, drop the history
                initial += cumulative
                cumulative = 0.0
            last_sec_in_day = sec_in_day

        delta = tick_delta(p.volume, p.aggressor)
        # CvdTracker::update(): a non-finite delta leaves the accumulator unchanged
        if math.isfinite(delta):
            cumulative += delta
        yield Step(idx, p.epoch_s, _exact_integer(initial + cumulative, idx))


def produce(tape: str | Path, feature: str = "tape_cvd_lot_v1") -> list[Step]:
    """Compute the feature from a frozen trade tape CSV (the L2 write path's entry point)."""
    return list(cvd_steps(read_tape(tape), feature))


def write_tsv(steps: Iterable[Step], sink) -> None:
    """Write ``idx<TAB>epoch_s<TAB>cvd`` rows as integers, byte-identical to ``l2_dump``."""
    sink.write("idx\tepoch_s\tcvd\n")
    for s in steps:
        sink.write(f"{s.idx}\t{s.epoch_s}\t{s.cvd}\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--feature", default="tape_cvd_lot_v1", choices=FEATURES)
    ap.add_argument("--tape", required=True, help="frozen L0 trade tape CSV")
    ap.add_argument("--out", default=None, help="output path (default: stdout)")
    args = ap.parse_args(argv)

    steps = produce(args.tape, args.feature)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            write_tsv(steps, fh)
    else:
        write_tsv(steps, sys.stdout)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
