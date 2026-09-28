"""Parity between the ingestion calendar and the tape-derived research calendar."""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DATA_PROVIDER = _REPO_ROOT / "data_provider"
for _p in (str(_REPO_ROOT), str(_DATA_PROVIDER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from api.market_calendar import VN30MarketCalendar  # noqa: E402

from historical.research_lab import calendar_vn  # noqa: E402


@pytest.fixture(scope="module")
def ingestion():
    return VN30MarketCalendar()


def _research_is_non_trading(day: date) -> bool:
    """Return the research calendar's verdict, weekends included.

    Uses ``is_trading_day``: the module's sets hold ISO strings, so testing a ``date``
    for membership would always be False.
    """
    return not calendar_vn.is_trading_day(day)


def _ingestion_is_non_trading(cal: VN30MarketCalendar, day: date) -> bool:
    is_holiday, _ = cal.is_market_holiday(day)
    return is_holiday


# --- 2026 National Day break ---

# Closed days have zero rows in vn30f_trades and in ohlcv_m1 for VN30F1M and VN30INDEX.
MEASURED_CLOSED_2026 = (date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 2))
MEASURED_OPEN_2026 = (date(2026, 8, 28), date(2026, 9, 3), date(2026, 9, 4))


@pytest.mark.parametrize("day", MEASURED_CLOSED_2026)
def test_ingestion_calendar_knows_the_measured_closures(ingestion, day):
    assert _ingestion_is_non_trading(ingestion, day), (
        f"{day} was measured closed on three independent series but the ingestion "
        f"calendar still treats it as a trading day; the coverage checker will "
        f"report it as a MISSING session"
    )


@pytest.mark.parametrize("day", MEASURED_CLOSED_2026)
def test_research_calendar_knows_the_measured_closures(day):
    assert _research_is_non_trading(
        day
    ), f"{day} was measured closed but research treats it as a session"


@pytest.mark.parametrize("day", MEASURED_OPEN_2026)
def test_measured_open_days_stay_open_in_both(ingestion, day):
    assert not _ingestion_is_non_trading(ingestion, day)
    assert not _research_is_non_trading(day)


# From `default.ohlcv_m1` (VN30F1M); a full session has 241 M1 bars.
MEASURED_SESSIONS_JAN_2026 = (date(2026, 1, 19), date(2026, 1, 20), date(2026, 1, 21))
MEASURED_TET_2026 = (
    date(2026, 2, 16),
    date(2026, 2, 17),
    date(2026, 2, 18),
    date(2026, 2, 19),
    date(2026, 2, 20),
)


@pytest.mark.parametrize("day", MEASURED_SESSIONS_JAN_2026)
def test_january_2026_sessions_are_not_marked_holiday(ingestion, day):
    """These days hold full 241-bar sessions, so ingestion must not skip them."""
    assert not _ingestion_is_non_trading(ingestion, day), (
        f"{day} holds 241 M1 bars (a full session) but the ingestion calendar "
        f"calls it a holiday; the service will not subscribe that day"
    )


@pytest.mark.parametrize("day", MEASURED_TET_2026)
def test_tet_2026_is_marked_holiday(ingestion, day):
    assert _ingestion_is_non_trading(ingestion, day), (
        f"{day} has zero rows in ohlcv_m1 (Tet) but the ingestion calendar calls "
        f"it a trading day; the coverage checker will report a MISSING session"
    )


def test_the_fix_did_not_reach_beyond_what_was_measured():
    """Every 2026 movable holiday is backed by a measurement or by the tape-derived calendar."""
    from api.market_calendar import MOVABLE_HOLIDAYS

    declared_2026 = {date(2026, m, d) for m, d in MOVABLE_HOLIDAYS[2026]}
    evidence = set(MEASURED_CLOSED_2026) | set(MEASURED_TET_2026)
    # (1, 2) and (4, 27) are backed by the tape-derived calendar (see the span test)
    from_tape = {d for d in declared_2026 if _research_is_non_trading(d)}
    unbacked = declared_2026 - evidence - from_tape
    assert not unbacked, (
        f"MOVABLE_HOLIDAYS[2026] declares {sorted(unbacked)} with neither a "
        f"measurement in this file nor backing from the tape-derived calendar. "
        f"A holiday typed from a website is what this file exists to prevent."
    )


def test_every_year_the_service_may_run_in_has_holidays():
    """A year missing from ``MOVABLE_HOLIDAYS`` would silently have no movable holidays."""
    from api.market_calendar import MOVABLE_HOLIDAYS

    for year in (2026, 2027):
        assert MOVABLE_HOLIDAYS.get(year), (
            f"MOVABLE_HOLIDAYS has no entry for {year}; every movable holiday that "
            f"year will read as a trading day"
        )


# --- day-by-day agreement ---


def _dates(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def test_the_two_calendars_agree_across_the_verified_span(ingestion):
    """The calendars agree on every day the research calendar verified against the tape."""
    start = max(calendar_vn.VERIFIED_FROM, date(2024, 1, 1))
    end = calendar_vn.VERIFIED_THROUGH

    disagreements = [
        (day, _ingestion_is_non_trading(ingestion, day), _research_is_non_trading(day))
        for day in _dates(start, end)
        if _ingestion_is_non_trading(ingestion, day) != _research_is_non_trading(day)
    ]

    assert not disagreements, (
        f"{len(disagreements)} day(s) where the ingestion and research calendars "
        f"disagree over {start}..{end}. First five: "
        + "; ".join(
            f"{d} ingestion_closed={i} research_closed={r}" for d, i, r in disagreements[:5]
        )
    )


def test_weekends_agree_trivially(ingestion):
    """Sanity check under the span test: both calendars close on a Saturday."""
    saturday = date(2026, 8, 29)
    assert saturday.weekday() == 5
    assert _ingestion_is_non_trading(ingestion, saturday)
    assert _research_is_non_trading(saturday)


def test_the_span_test_is_not_vacuous(ingestion):
    """The verified span contains at least 20 weekday holidays, not only weekends."""
    start = max(calendar_vn.VERIFIED_FROM, date(2024, 1, 1))
    end = calendar_vn.VERIFIED_THROUGH
    weekday_holidays = [
        day for day in _dates(start, end) if day.weekday() < 5 and _research_is_non_trading(day)
    ]
    assert len(weekday_holidays) >= 20, (
        f"only {len(weekday_holidays)} weekday holidays in {start}..{end}; the parity "
        f"test would be nearly vacuous"
    )


# --- holiday table ordering ---


def test_the_two_orderings_are_still_distinguishable():
    """``FIXED_HOLIDAYS`` is keyed (day, month) and ``MOVABLE_HOLIDAYS`` (month, day)."""
    from api.market_calendar import FIXED_HOLIDAYS, MOVABLE_HOLIDAYS

    assert (30, 4) in FIXED_HOLIDAYS, "FIXED_HOLIDAYS is no longer (day, month)"
    assert (2, 16) in MOVABLE_HOLIDAYS[2026], "MOVABLE_HOLIDAYS is no longer (month, day)"
