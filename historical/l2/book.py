"""Reader for a frozen order-book snapshot tape: ``default.vn30f_orderbook`` exported to CSV.

Ten levels per side; timestamps are whole seconds with no intra-second order, so features use
one :class:`BookState` at a time. ``total_*_qty`` are whole-book totals, not sums of the visible
levels. Stdlib only. The fixture here is synthetic (``samples/generate-fixtures.py``)::

    event_timestamp,bid_prices,bid_volumes,offer_prices,offer_volumes,total_bid_qty,total_offer_qty
    "2021-03-04 09:00:00","[1249.9,...]","[9,...]","[1250.1,...]","[14,...]",2878,3650
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from historical.l2.tape import (  # noqa: F401
    ICT_OFFSET_SECONDS,
    ict_day_key,
    ict_second_of_day,
    ict_string_to_epoch,
)

#: Book depth delivered by the feed, per side.
MAX_LEVELS = 10

#: Source column names; the ask side keeps the table's ``offer_*`` naming.
BOOK_CSV_COLUMNS = (
    "event_timestamp",
    "bid_prices",
    "bid_volumes",
    "offer_prices",
    "offer_volumes",
    "total_bid_qty",
    "total_offer_qty",
)


class BookError(ValueError):
    """A frozen book row that must not reach a producer."""


@dataclass(frozen=True)
class BookState:
    """One book snapshot in engine units; index 0 is the best level on each side.

    ``total_bid_qty`` and ``total_offer_qty`` are whole-book totals, not sums of the arrays.
    """

    epoch_s: int
    bid_prices: tuple[float, ...]
    bid_volumes: tuple[int, ...]
    offer_prices: tuple[float, ...]
    offer_volumes: tuple[int, ...]
    total_bid_qty: int
    total_offer_qty: int

    @property
    def levels(self) -> int:
        """Depth carried by this row: the length of its shortest array."""
        return min(
            len(self.bid_prices),
            len(self.bid_volumes),
            len(self.offer_prices),
            len(self.offer_volumes),
        )


def parse_array(text: str, *, cast, field: str, row: int) -> tuple:
    """Parse a ClickHouse CSV array such as ``"[1913.8,1913.3]"`` into a tuple.

    Whole numbers may lack a decimal point (``1913``); ``float`` accepts both spellings.
    """
    body = text.strip()
    if not (body.startswith("[") and body.endswith("]")):
        raise BookError(f"row {row}: {field} is not a bracketed array: {text!r}")
    body = body[1:-1].strip()
    if not body:
        return ()
    out = []
    for piece in body.split(","):
        piece = piece.strip()
        try:
            out.append(cast(piece))
        except ValueError as exc:
            raise BookError(f"row {row}: {field} element {piece!r} is not valid") from exc
    return tuple(out)


def _int(piece: str) -> int:
    """Parse a volume as ``int``; volumes are ``UInt64`` at the source, so floats are rejected."""
    return int(piece)


def parse_book(lines: Iterable[str]) -> Iterator[BookState]:
    """Parse book rows from any iterable of CSV lines, in file order."""
    reader = csv.DictReader(lines)
    if reader.fieldnames is None:
        raise BookError("book tape has no header row")
    columns = {name.strip().strip('"').lower(): name for name in reader.fieldnames}
    missing = [c for c in BOOK_CSV_COLUMNS if c not in columns]
    if missing:
        raise BookError(f"book tape is missing column(s) {missing}; got {sorted(columns)}")
    cols = {c: columns[c] for c in BOOK_CSV_COLUMNS}

    for idx, row in enumerate(reader):
        bid_prices = parse_array(row[cols["bid_prices"]], cast=float, field="bid_prices", row=idx)
        bid_volumes = parse_array(row[cols["bid_volumes"]], cast=_int, field="bid_volumes", row=idx)
        offer_prices = parse_array(
            row[cols["offer_prices"]], cast=float, field="offer_prices", row=idx
        )
        offer_volumes = parse_array(
            row[cols["offer_volumes"]], cast=_int, field="offer_volumes", row=idx
        )
        if not bid_prices or not offer_prices:
            raise BookError(f"row {idx}: one side of the book is empty")
        if len(bid_prices) != len(bid_volumes) or len(offer_prices) != len(offer_volumes):
            raise BookError(
                f"row {idx}: price/volume arrays disagree in length "
                f"({len(bid_prices)}/{len(bid_volumes)} bid, "
                f"{len(offer_prices)}/{len(offer_volumes)} offer)"
            )
        yield BookState(
            epoch_s=ict_string_to_epoch(row[cols["event_timestamp"]]),
            bid_prices=bid_prices,
            bid_volumes=bid_volumes,
            offer_prices=offer_prices,
            offer_volumes=offer_volumes,
            total_bid_qty=_int(row[cols["total_bid_qty"]].strip()),
            total_offer_qty=_int(row[cols["total_offer_qty"]].strip()),
        )


def read_book(path: str | Path) -> list[BookState]:
    """Read a frozen book CSV in file order.

    Updates within a second have no ordering key, so file order is the order both stacks share;
    it decides only which state a ``last`` or ``first`` minute reduction picks.
    """
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return list(parse_book(fh))


def minute_key(epoch_s: int) -> int:
    """ICT minute index of a timestamp: minutes since 1970-01-01 00:00 ICT."""
    return (epoch_s + ICT_OFFSET_SECONDS) // 60


def minute_of_session(epoch_s: int) -> int:
    """Minutes since ICT midnight (540 = 09:00, 885 = 14:45); the time-of-day baseline key."""
    return ict_second_of_day(epoch_s) // 60


def group_by_minute(states: Sequence[BookState]) -> list[tuple[int, list[BookState]]]:
    """``[(minute_key, [states in file order]), ...]``; minutes without an update are absent.

    No forward fill (``stale_ms: 0`` in the ``ob_*`` specs). Raises :class:`BookError` if the
    minute key goes backwards.
    """
    out: list[tuple[int, list[BookState]]] = []
    current: int | None = None
    for idx, st in enumerate(states):
        key = minute_key(st.epoch_s)
        if current is not None and key < current:
            # an out-of-order tape would split a minute and make `last`-in-minute ill-defined
            raise BookError(
                f"row {idx}: minute key {key} goes backwards from {current}; the frozen "
                f"book tape must be in non-decreasing event_timestamp order"
            )
        if key != current:
            out.append((key, []))
            current = key
        out[-1][1].append(st)
    return out
