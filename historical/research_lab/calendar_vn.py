"""VN30F1M trading calendar with holidays DERIVED FROM THE TAPE rather than transcribed.

:data:`TRADING_HOLIDAYS` holds every weekday from :data:`VERIFIED_FROM` to :data:`VERIFIED_THROUGH`
with no VN30F1M bar in ClickHouse ``ohlcv_m1`` (:data:`DERIVATION_QUERY`), less the ingestion
holes in :data:`TAPE_GAPS`. :data:`PROVISIONAL_HOLIDAYS` holds predicted later closures and is
kept separate. The sets are committed constants: standard library only, nothing is queried at
import, and an empty verified set raises at import.
"""

from __future__ import annotations

import datetime as _dt
from typing import Iterable, Iterator

__all__ = [
    "TRADING_HOLIDAYS",
    "PROVISIONAL_HOLIDAYS",
    "TAPE_GAPS",
    "TET_HOLIDAYS",
    "NON_TRADING_DAYS",
    "HOLIDAY_KIND",
    "VERIFIED_FROM",
    "VERIFIED_THROUGH",
    "PROVISIONAL_THROUGH",
    "DERIVATION_QUERY",
    "is_weekend",
    "is_trading_day",
    "is_holiday",
    "holiday_kind",
    "holiday_confidence",
    "previous_trading_day",
    "next_trading_day",
    "trading_days",
    "session_adjacency",
    "gap_kind",
    "skipped_trading_days",
    "derive_from_clickhouse",
    "verify_against_clickhouse",
]

Date = _dt.date

# Query the verified holidays were derived from (see `derive_from_clickhouse`).
DERIVATION_QUERY = (
    "SELECT toDate(time_1m) AS d, count() AS n FROM default.ohlcv_m1 "
    "WHERE symbol = 'VN30F1M' GROUP BY d ORDER BY d FORMAT TabSeparated"
)

VERIFIED_FROM = _dt.date(2017, 11, 6)  # first VN30F1M M1 bar in `default.ohlcv_m1`
VERIFIED_THROUGH = _dt.date(2026, 8, 28)  # last session on the tape at derivation time
PROVISIONAL_THROUGH = _dt.date(2027, 12, 31)


def _d(iso: str) -> Date:
    return _dt.date.fromisoformat(iso)


# --- verified closures: weekdays with no session on the tape ---
_VERIFIED_ISO: tuple[str, ...] = (
    # ---- 2018 (11 sessions) ----
    # New Year
    "2018-01-01",
    # Tet
    "2018-02-14",
    "2018-02-15",
    "2018-02-16",
    "2018-02-19",
    "2018-02-20",
    # Hung Kings
    "2018-04-25",
    # Reunification 30/4 + Labour 1/5
    "2018-04-30",
    "2018-05-01",
    # National Day 2/9
    "2018-09-03",
    # New Year
    "2018-12-31",
    # ---- 2019 (11 sessions) ----
    # New Year
    "2019-01-01",
    # Tet
    "2019-02-04",
    "2019-02-05",
    "2019-02-06",
    "2019-02-07",
    "2019-02-08",
    # Hung Kings
    "2019-04-15",
    # Reunification 30/4 + Labour 1/5
    "2019-04-29",
    "2019-04-30",
    "2019-05-01",
    # National Day 2/9
    "2019-09-02",
    # ---- 2020 (10 sessions) ----
    # New Year
    "2020-01-01",
    # Tet
    "2020-01-23",
    "2020-01-24",
    "2020-01-27",
    "2020-01-28",
    "2020-01-29",
    # Hung Kings
    "2020-04-02",
    # Reunification 30/4 + Labour 1/5
    "2020-04-30",
    "2020-05-01",
    # National Day 2/9
    "2020-09-02",
    # ---- 2021 (11 sessions) ----
    # New Year
    "2021-01-01",
    # Tet
    "2021-02-10",
    "2021-02-11",
    "2021-02-12",
    "2021-02-15",
    "2021-02-16",
    # Hung Kings
    "2021-04-21",
    # Reunification 30/4 + Labour 1/5
    "2021-04-30",
    "2021-05-03",
    # National Day 2/9
    "2021-09-02",
    "2021-09-03",
    # ---- 2022 (11 sessions) ----
    # New Year
    "2022-01-03",
    # Tet
    "2022-01-31",
    "2022-02-01",
    "2022-02-02",
    "2022-02-03",
    "2022-02-04",
    # Hung Kings
    "2022-04-11",
    # Reunification 30/4 + Labour 1/5
    "2022-05-02",
    "2022-05-03",
    # National Day 2/9
    "2022-09-01",
    "2022-09-02",
    # ---- 2023 (11 sessions) ----
    # New Year
    "2023-01-02",
    # Tet
    "2023-01-20",
    "2023-01-23",
    "2023-01-24",
    "2023-01-25",
    "2023-01-26",
    # Reunification 30/4 + Labour 1/5
    "2023-05-01",
    "2023-05-02",
    "2023-05-03",
    # National Day 2/9
    "2023-09-01",
    "2023-09-04",
    # ---- 2024 (12 sessions) ----
    # New Year
    "2024-01-01",
    # Tet
    "2024-02-08",
    "2024-02-09",
    "2024-02-12",
    "2024-02-13",
    "2024-02-14",
    # Hung Kings
    "2024-04-18",
    # Reunification 30/4 + Labour 1/5
    "2024-04-29",
    "2024-04-30",
    "2024-05-01",
    # National Day 2/9
    "2024-09-02",
    "2024-09-03",
    # ---- 2025 (12 sessions) ----
    # New Year
    "2025-01-01",
    # Tet
    "2025-01-27",
    "2025-01-28",
    "2025-01-29",
    "2025-01-30",
    "2025-01-31",
    # Hung Kings
    "2025-04-07",
    # Reunification 30/4 + Labour 1/5
    "2025-04-30",
    "2025-05-01",
    "2025-05-02",
    # National Day 2/9
    "2025-09-01",
    "2025-09-02",
    # ---- 2026 (10 sessions) ----
    # New Year
    "2026-01-01",
    "2026-01-02",  # government swap day, absent from the exchange's published calendar
    # Tet
    "2026-02-16",
    "2026-02-17",
    "2026-02-18",
    "2026-02-19",
    "2026-02-20",
    # Hung Kings
    "2026-04-27",
    # Reunification 30/4 + Labour 1/5
    "2026-04-30",
    "2026-05-01",
)


# Holiday kind of each verified closure (see `holiday_kind`).
_VERIFIED_KIND_ISO: dict[str, str] = {
    "2018-01-01": "new_year",
    "2018-02-14": "tet",
    "2018-02-15": "tet",
    "2018-02-16": "tet",
    "2018-02-19": "tet",
    "2018-02-20": "tet",
    "2018-04-25": "hung_kings",
    "2018-04-30": "reunification_labour",
    "2018-05-01": "reunification_labour",
    "2018-09-03": "national_day",
    "2018-12-31": "new_year",
    "2019-01-01": "new_year",
    "2019-02-04": "tet",
    "2019-02-05": "tet",
    "2019-02-06": "tet",
    "2019-02-07": "tet",
    "2019-02-08": "tet",
    "2019-04-15": "hung_kings",
    "2019-04-29": "reunification_labour",
    "2019-04-30": "reunification_labour",
    "2019-05-01": "reunification_labour",
    "2019-09-02": "national_day",
    "2020-01-01": "new_year",
    "2020-01-23": "tet",
    "2020-01-24": "tet",
    "2020-01-27": "tet",
    "2020-01-28": "tet",
    "2020-01-29": "tet",
    "2020-04-02": "hung_kings",
    "2020-04-30": "reunification_labour",
    "2020-05-01": "reunification_labour",
    "2020-09-02": "national_day",
    "2021-01-01": "new_year",
    "2021-02-10": "tet",
    "2021-02-11": "tet",
    "2021-02-12": "tet",
    "2021-02-15": "tet",
    "2021-02-16": "tet",
    "2021-04-21": "hung_kings",
    "2021-04-30": "reunification_labour",
    "2021-05-03": "reunification_labour",
    "2021-09-02": "national_day",
    "2021-09-03": "national_day",
    "2022-01-03": "new_year",
    "2022-01-31": "tet",
    "2022-02-01": "tet",
    "2022-02-02": "tet",
    "2022-02-03": "tet",
    "2022-02-04": "tet",
    "2022-04-11": "hung_kings",
    "2022-05-02": "reunification_labour",
    "2022-05-03": "reunification_labour",
    "2022-09-01": "national_day",
    "2022-09-02": "national_day",
    "2023-01-02": "new_year",
    "2023-01-20": "tet",
    "2023-01-23": "tet",
    "2023-01-24": "tet",
    "2023-01-25": "tet",
    "2023-01-26": "tet",
    "2023-05-01": "reunification_labour",
    "2023-05-02": "reunification_labour",
    "2023-05-03": "reunification_labour",
    "2023-09-01": "national_day",
    "2023-09-04": "national_day",
    "2024-01-01": "new_year",
    "2024-02-08": "tet",
    "2024-02-09": "tet",
    "2024-02-12": "tet",
    "2024-02-13": "tet",
    "2024-02-14": "tet",
    "2024-04-18": "hung_kings",
    "2024-04-29": "reunification_labour",
    "2024-04-30": "reunification_labour",
    "2024-05-01": "reunification_labour",
    "2024-09-02": "national_day",
    "2024-09-03": "national_day",
    "2025-01-01": "new_year",
    "2025-01-27": "tet",
    "2025-01-28": "tet",
    "2025-01-29": "tet",
    "2025-01-30": "tet",
    "2025-01-31": "tet",
    "2025-04-07": "hung_kings",
    "2025-04-30": "reunification_labour",
    "2025-05-01": "reunification_labour",
    "2025-05-02": "reunification_labour",
    "2025-09-01": "national_day",
    "2025-09-02": "national_day",
    "2026-01-01": "new_year",
    "2026-01-02": "new_year",
    "2026-02-16": "tet",
    "2026-02-17": "tet",
    "2026-02-18": "tet",
    "2026-02-19": "tet",
    "2026-02-20": "tet",
    "2026-04-27": "hung_kings",
    "2026-04-30": "reunification_labour",
    "2026-05-01": "reunification_labour",
}

# --- tape gaps: weekdays missing from the tape on which the market traded ---
# They are sessions, not holidays: `is_trading_day` is True and `gap_kind` returns "data_gap".
# The partial session on 2025-04-10 (133 of ~243 bars) counts as a full trading day.
_TAPE_GAP_ISO: tuple[str, ...] = (
    "2020-07-16",  # Thu, expiry day; front-month rollover ingestion hole
    "2020-07-17",  # Fri
    "2020-07-20",  # Mon
    "2026-02-27",  # Fri, a trading day on the published exchange calendar
)

# --- provisional closures: predictions, kept apart from the verified set ---
_PROVISIONAL_ISO: dict[str, tuple[str, str]] = {
    # ---- 2026 remainder: exchange-announced ----
    "2026-08-31": ("national_day", "announced"),  # Mon, swap day (no Saturday make-up session)
    "2026-09-01": ("national_day", "announced"),  # Tue
    "2026-09-02": ("national_day", "announced"),  # Wed
    # ---- 2027: inferred from the labour code and lunar dates ----
    "2027-01-01": ("new_year", "statutory"),  # Fri
    # Tet: Thursday to Wednesday, as in earlier years whose Tet day 1 fell on a Saturday
    "2027-02-04": ("tet", "statutory"),  # Thu, day 28 of lunar month 12
    "2027-02-05": ("tet", "statutory"),  # Fri, day 29 of lunar month 12 (New Year's Eve)
    "2027-02-08": ("tet", "statutory"),  # Mon, Tet day 3
    "2027-02-09": ("tet", "statutory"),  # Tue, Tet day 4
    "2027-02-10": ("tet", "statutory"),  # Wed, Tet day 5
    "2027-04-16": ("hung_kings", "statutory"),  # Fri, day 10 of lunar month 3
    "2027-04-30": ("reunification_labour", "statutory"),  # Fri
    "2027-05-03": ("reunification_labour", "statutory"),  # Mon, observed for Sat 1 May
    "2027-09-02": ("national_day", "statutory"),  # Thu
    "2027-09-03": ("national_day", "statutory"),  # Fri, assumed bridge day
}


TRADING_HOLIDAYS: frozenset[Date] = frozenset(_d(s) for s in _VERIFIED_ISO)
PROVISIONAL_HOLIDAYS: frozenset[Date] = frozenset(_d(s) for s in _PROVISIONAL_ISO)
TAPE_GAPS: frozenset[Date] = frozenset(_d(s) for s in _TAPE_GAP_ISO)

HOLIDAY_KIND: dict[Date, str] = {_d(s): k for s, k in _VERIFIED_KIND_ISO.items()}
HOLIDAY_KIND.update({_d(s): k for s, (k, _c) in _PROVISIONAL_ISO.items()})

_CONFIDENCE: dict[Date, str] = {d: "derived" for d in TRADING_HOLIDAYS}
_CONFIDENCE.update({_d(s): c for s, (_k, c) in _PROVISIONAL_ISO.items()})

TET_HOLIDAYS: frozenset[Date] = frozenset(d for d, k in HOLIDAY_KIND.items() if k == "tet")

_ALL_HOLIDAYS: frozenset[Date] = TRADING_HOLIDAYS | PROVISIONAL_HOLIDAYS

# ISO-string holidays for `forward_registry`. Provisional dates are included so forward accrual
# does not report an announced closure as missing data; tape gaps are sessions and are excluded.
NON_TRADING_DAYS: frozenset[str] = frozenset(d.isoformat() for d in _ALL_HOLIDAYS)


def _as_date(value: object) -> Date:
    """Coerce a ``date``, ``datetime``, ISO string or pandas/numpy timestamp to a ``date``."""
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    if isinstance(value, str):
        return _dt.date.fromisoformat(value[:10])
    to_pydatetime = getattr(value, "to_pydatetime", None)  # pandas.Timestamp
    if callable(to_pydatetime):
        return to_pydatetime().date()
    date_attr = getattr(value, "date", None)
    if callable(date_attr):
        return date_attr()
    raise TypeError(f"cannot interpret {value!r} ({type(value).__name__}) as a calendar date")


# --- predicates ---
def is_weekend(d: object) -> bool:
    """Return True for Saturday or Sunday; VN30F1M has no weekend sessions."""
    return _as_date(d).weekday() >= 5


def is_holiday(d: object, include_provisional: bool = True) -> bool:
    """Return True if ``d`` is a registered holiday (weekends are not holidays).

    With ``include_provisional=False`` only verified holidays count.
    """
    day = _as_date(d)
    if include_provisional:
        return day in _ALL_HOLIDAYS
    return day in TRADING_HOLIDAYS


def is_trading_day(d: object, include_provisional: bool = True) -> bool:
    """Return True if VN30F1M trades on ``d``; dates in :data:`TAPE_GAPS` count as trading days."""
    day = _as_date(d)
    if day.weekday() >= 5:
        return False
    return not is_holiday(day, include_provisional=include_provisional)


def holiday_kind(d: object) -> str | None:
    """Return the holiday kind of ``d``, or None if it is not a registered holiday.

    Kinds: ``tet``, ``new_year``, ``hung_kings``, ``reunification_labour``, ``national_day``.
    """
    return HOLIDAY_KIND.get(_as_date(d))


def holiday_confidence(d: object) -> str | None:
    """Return how a holiday is known, or None if ``d`` is not one.

    ``derived`` (observed on the tape), ``announced`` (exchange calendar) or ``statutory``
    (inferred from the labour code and lunar dates).
    """
    return _CONFIDENCE.get(_as_date(d))


# --- navigation ---
_MAX_SEARCH_DAYS = 30  # longest observed closure is 5 sessions ~ 9 calendar days


def previous_trading_day(d: object, include_provisional: bool = True) -> Date:
    """Return the last trading day strictly before ``d``."""
    day = _as_date(d)
    for _ in range(_MAX_SEARCH_DAYS):
        day -= _dt.timedelta(days=1)
        if is_trading_day(day, include_provisional=include_provisional):
            return day
    raise ValueError(f"no trading day within {_MAX_SEARCH_DAYS} days before {_as_date(d)}")


def next_trading_day(d: object, include_provisional: bool = True) -> Date:
    """Return the first trading day strictly after ``d``."""
    day = _as_date(d)
    for _ in range(_MAX_SEARCH_DAYS):
        day += _dt.timedelta(days=1)
        if is_trading_day(day, include_provisional=include_provisional):
            return day
    raise ValueError(f"no trading day within {_MAX_SEARCH_DAYS} days after {_as_date(d)}")


def trading_days(start: object, end: object, include_provisional: bool = True) -> Iterator[Date]:
    """Yield every trading day in the inclusive range ``[start, end]``."""
    day, last = _as_date(start), _as_date(end)
    while day <= last:
        if is_trading_day(day, include_provisional=include_provisional):
            yield day
        day += _dt.timedelta(days=1)


def skipped_trading_days(d_from: object, d_to: object) -> list[Date]:
    """Return the trading days strictly between two sessions (non-empty: the tape skipped one)."""
    a, b = _as_date(d_from), _as_date(d_to)
    if b <= a:
        raise ValueError(f"d_to ({b}) must be strictly after d_from ({a})")
    return list(trading_days(a + _dt.timedelta(days=1), b - _dt.timedelta(days=1)))


# --- gaps between sessions ---
def session_adjacency(d_from: object, d_to: object) -> int:
    """Return the number of calendar nights between two sessions.

    1 for a normal overnight, 3 for Friday to Monday, typically 9 across Tet. This is a plain
    date difference; use :func:`gap_kind` to check that the two sessions are consecutive.
    """
    a, b = _as_date(d_from), _as_date(d_to)
    if b <= a:
        raise ValueError(f"d_to ({b}) must be strictly after d_from ({a})")
    return (b - a).days


def gap_kind(d_from: object, d_to: object) -> str:
    """Classify the gap between two sessions so a screen can stratify overnight returns.

    Returns ``"overnight"`` (no day between), ``"weekend"`` (only weekend days), ``"holiday"``
    (a non-Tet holiday), ``"tet"`` (a Tet closure) or ``"data_gap"`` (a trading day between: the
    tape is missing a session and the pair should be dropped).
    """
    a, b = _as_date(d_from), _as_date(d_to)
    if b <= a:
        raise ValueError(f"d_to ({b}) must be strictly after d_from ({a})")

    between = [a + _dt.timedelta(days=k) for k in range(1, (b - a).days)]
    if not between:
        return "overnight"
    if any(is_trading_day(day) for day in between):
        return "data_gap"
    if any(day in TET_HOLIDAYS for day in between):
        return "tet"
    if any(day in _ALL_HOLIDAYS for day in between):
        return "holiday"
    return "weekend"


# --- opt-in re-derivation (never called at import) ---
_DEFAULT_CH_URL = "http://localhost:8123/"


def derive_from_clickhouse(
    url: str = _DEFAULT_CH_URL,
    symbol: str = "VN30F1M",
    timeout: float = 60.0,
) -> tuple[frozenset[Date], frozenset[Date]]:
    """Re-run :data:`DERIVATION_QUERY` and return ``(session_dates, missing_weekdays)``.

    Network call over stdlib ``urllib``; raises on failure or an empty result. Module state is
    not modified.
    """
    import urllib.request

    query = (
        f"SELECT toDate(time_1m) FROM default.ohlcv_m1 WHERE symbol = '{symbol}' "
        "GROUP BY 1 ORDER BY 1 FORMAT TabSeparated"
    )
    with urllib.request.urlopen(url, data=query.encode("utf-8"), timeout=timeout) as resp:
        body = resp.read().decode("utf-8")

    sessions = frozenset(_d(line.strip()) for line in body.splitlines() if line.strip())
    if not sessions:
        raise RuntimeError(f"ClickHouse returned no sessions for symbol={symbol!r} — refusing")

    lo, hi = min(sessions), max(sessions)
    missing, day = [], lo
    while day <= hi:
        if day.weekday() < 5 and day not in sessions:
            missing.append(day)
        day += _dt.timedelta(days=1)
    return sessions, frozenset(missing)


def verify_against_clickhouse(url: str = _DEFAULT_CH_URL) -> dict[str, list[str]]:
    """Diff the committed constants against a fresh derivation.

    Returns ``{"unexpected_closure": [...], "unexpected_session": [...]}``; both empty means
    the constants still match the tape.
    """
    _sessions, missing = derive_from_clickhouse(url=url)
    known = TRADING_HOLIDAYS | TAPE_GAPS
    return {
        "unexpected_closure": sorted(d.isoformat() for d in (missing - known)),
        "unexpected_session": sorted(
            d.isoformat()
            for d in (TRADING_HOLIDAYS - missing)
            if VERIFIED_FROM <= d <= VERIFIED_THROUGH
        ),
    }


def _self_check(holidays: Iterable[Date]) -> None:
    """Raise if ``holidays`` is empty, has a weekend, or overlaps the gap or provisional sets."""
    days = frozenset(holidays)
    if not days:
        raise RuntimeError(
            "calendar_vn.TRADING_HOLIDAYS is empty. An empty VN trading calendar is the "
            "exact defect this module exists to fix (see forward_registry.NON_TRADING_DAYS): "
            "it makes every overnight gap look like one night. Re-derive with "
            "derive_from_clickhouse() rather than shipping an empty set."
        )
    if any(d.weekday() >= 5 for d in days):
        raise RuntimeError("weekend date registered as a holiday — weekends are not holidays here")
    overlap = days & TAPE_GAPS
    if overlap:
        raise RuntimeError(f"dates are both holidays and tape gaps: {sorted(overlap)}")
    if days & PROVISIONAL_HOLIDAYS:
        raise RuntimeError("verified and provisional holiday sets overlap — keep them disjoint")


_self_check(TRADING_HOLIDAYS)
