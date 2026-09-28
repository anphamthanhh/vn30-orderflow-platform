"""Reader for a frozen L0 price tape (``event_timestamp, price``), the input of profile features.

Kept separate from :mod:`historical.l2.tape` so each reader requires its own columns: this one
fails without ``price``, ``read_tape`` without ``aggressor``. Timestamps are converted by
:func:`historical.l2.tape.ict_string_to_epoch`. Stdlib only.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Iterator

from historical.l2.tape import ICT_OFFSET_SECONDS, ict_day_key, ict_string_to_epoch

_EPOCH_ORDINAL = 719163  # date(1970, 1, 1).toordinal()

__all__ = [
    "PricePrint",
    "ICT_OFFSET_SECONDS",
    "ict_day_key",
    "ict_string_to_epoch",
    "ict_date_string",
    "parse_price_tape",
    "read_price_tape",
]


@dataclass(frozen=True)
class PricePrint:
    """One price print in engine units (UTC epoch seconds)."""

    epoch_s: int
    price: float


def ict_date_string(day_key: int) -> str:
    """Format an ``ict_day_key`` as ``YYYY-MM-DD``, the session label ``l2_dump`` also prints."""
    return date.fromordinal(day_key + _EPOCH_ORDINAL).isoformat()


def parse_price_tape(lines: Iterable[str]) -> Iterator[PricePrint]:
    """Parse price-tape rows from any iterable of CSV lines."""
    reader = csv.DictReader(lines)
    if reader.fieldnames is None:
        raise ValueError("tape has no header row")
    columns = {name.strip().strip('"').lower(): name for name in reader.fieldnames}
    for required in ("event_timestamp", "price"):
        if required not in columns:
            raise ValueError(f"tape has no {required!r} column; got {sorted(columns)}")
    ts_col, price_col = columns["event_timestamp"], columns["price"]
    for row in reader:
        yield PricePrint(
            epoch_s=ict_string_to_epoch(row[ts_col]),
            price=float(row[price_col]),
        )


def read_price_tape(path: str | Path) -> list[PricePrint]:
    """Read a frozen price tape CSV in file order.

    A TPO profile counts distinct (period, price) cells, so unlike CVD it does not depend on
    the order; file order is kept for reproducibility.
    """
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return list(parse_price_tape(fh))
