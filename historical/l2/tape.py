"""Reader for a frozen L0 trade tape: ``default.vn30f_trades`` exported to CSV. Stdlib only.

``event_timestamp`` is ICT wall-clock time and is converted to UTC epoch seconds, the unit of
``market_types::EventTimestamp``; the Rust counterpart is ``ict_string_to_epoch`` in
``native_platform/crates/orderflow-engine/src/bin/l2_dump.rs``. Format::

    event_timestamp,price,volume,aggressor
    "2021-03-04 09:00:00",1250.0,596,"U"
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Iterator

#: ICT offset from UTC in seconds (fixed UTC+7, no DST, so no timezone database is needed).
ICT_OFFSET_SECONDS = 7 * 3600

#: ``date(1970, 1, 1).toordinal()``.
_EPOCH_ORDINAL = 719163


@dataclass(frozen=True)
class Print:
    """One trade print in engine units (UTC epoch seconds)."""

    epoch_s: int
    price: float
    volume: float
    aggressor: str


def ict_string_to_epoch(text: str) -> int:
    """Parse ``YYYY-MM-DD HH:MM:SS[.fff]`` as ICT wall-clock time into UTC epoch seconds.

    Sub-second digits are truncated, matching the engine's one-second resolution.
    """
    text = text.strip()
    sep = " " if " " in text else "T"
    try:
        day_part, time_part = text.split(sep, 1)
        y, m, d = (int(v) for v in day_part.split("-"))
        hh, mm, ss = (int(v) for v in time_part.split(".", 1)[0].split(":"))
    except ValueError as exc:  # pragma: no cover - message is the point
        raise ValueError(f"{text!r} is not `YYYY-MM-DD HH:MM:SS`") from exc
    days = date(y, m, d).toordinal() - _EPOCH_ORDINAL
    return days * 86_400 + hh * 3600 + mm * 60 + ss - ICT_OFFSET_SECONDS


def ict_day_key(epoch_s: int) -> int:
    """ICT calendar day as an integer: days since 1970-01-01 in ICT."""
    return (epoch_s + ICT_OFFSET_SECONDS) // 86_400


def ict_second_of_day(epoch_s: int) -> int:
    """Seconds since ICT midnight."""
    return (epoch_s + ICT_OFFSET_SECONDS) % 86_400


def parse_tape(lines: Iterable[str]) -> Iterator[Print]:
    """Parse tape rows from any iterable of CSV lines; ``price`` is optional (NaN if absent)."""
    reader = csv.DictReader(lines)
    if reader.fieldnames is None:
        raise ValueError("tape has no header row")
    columns = {name.strip().strip('"').lower(): name for name in reader.fieldnames}
    for required in ("event_timestamp", "volume", "aggressor"):
        if required not in columns:
            raise ValueError(f"tape has no {required!r} column; got {sorted(columns)}")
    ts_col, vol_col, side_col = (columns[c] for c in ("event_timestamp", "volume", "aggressor"))
    price_col = columns.get("price")

    for row in reader:
        yield Print(
            epoch_s=ict_string_to_epoch(row[ts_col]),
            price=float(row[price_col]) if price_col else float("nan"),
            volume=float(row[vol_col]),
            aggressor=row[side_col].strip(),
        )


def read_tape(path: str | Path) -> list[Print]:
    """Read a frozen tape CSV in file order.

    Prints within a second have no ordering key, so file order is the order both stacks share.
    """
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return list(parse_tape(fh))
