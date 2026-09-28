"""Unified VN30 market calendar with sessions, holidays, and time-until-open."""

from datetime import date, datetime
from datetime import time as dt_time
from datetime import timedelta, timezone
from typing import Tuple

ICT = timezone(timedelta(hours=7))


# Vietnamese fixed holidays: (day, month)
FIXED_HOLIDAYS = [
    (1, 1),  # New Year's Day
    (30, 4),  # Reunification Day
    (1, 5),  # Labor Day
    (2, 9),  # National Day
]

# Movable public holidays by year as Gregorian (month, day); FIXED_HOLIDAYS is (day, month).
# Generated from historical/research_lab/calendar_vn.py (sessions observed in the tape);
# historical/tests/test_calendar_parity.py keeps the two in agreement. A missing year yields
# no movable holidays, so every year in use must be declared; 2027 is provisional.
MOVABLE_HOLIDAYS = {
    2024: [(2, 8), (2, 9), (2, 12), (2, 13), (2, 14), (4, 18), (4, 29), (9, 3)],
    2025: [(1, 27), (1, 28), (1, 29), (1, 30), (1, 31), (4, 7), (5, 2), (9, 1)],
    2026: [
        (1, 2),
        (2, 16),
        (2, 17),
        (2, 18),
        (2, 19),
        (2, 20),
        (4, 27),
        # extended National Day break: no sessions from Aug 31 to Sep 2
        (8, 31),
        (9, 1),
    ],
    2027: [(2, 4), (2, 5), (2, 8), (2, 9), (2, 10), (4, 16), (5, 3), (9, 3)],  # provisional
}

TRADING_DAYS = {0, 1, 2, 3, 4}  # Mon-Fri

# Sessions in UTC+7
MORNING_OPEN = dt_time(9, 0)
MORNING_CLOSE = dt_time(11, 30)
AFTERNOON_OPEN = dt_time(13, 0)
AFTERNOON_CLOSE = dt_time(14, 45)

# Market-hours window used for alerting and service scheduling.
MARKET_OPEN = dt_time(9, 0)
MARKET_CLOSE = dt_time(15, 0)

# Ingestion window: opens at the ATO auction (08:45, `ATO_OPEN_SOD` in
# data_provider/session_phase.py) rather than MARKET_OPEN, so the opening-auction book is
# captured (ADR 0031).
COLLECTION_OPEN = dt_time(8, 45)
COLLECTION_CLOSE = MARKET_CLOSE


class VN30MarketCalendar:
    """Unified market calendar for VN30 derivatives."""

    def is_trading_day(self, check_date: date) -> bool:
        is_holiday, _ = self.is_market_holiday(check_date)
        return not is_holiday

    def is_trading_hours(self, check_time: dt_time) -> bool:
        """Check if a time falls within a trading session."""
        in_morning = MORNING_OPEN <= check_time < MORNING_CLOSE
        in_afternoon = AFTERNOON_OPEN <= check_time < AFTERNOON_CLOSE
        return in_morning or in_afternoon

    def is_market_hours(self, dt: datetime = None) -> bool:
        """Check if a datetime falls in the 09:00-15:00 market-hours window of a trading day."""
        if dt is None:
            dt = datetime.now(ICT)
        if dt.weekday() not in TRADING_DAYS:
            return False
        is_holiday, _ = self.is_market_holiday(dt.date())
        if is_holiday:
            return False
        return MARKET_OPEN <= dt.time() <= MARKET_CLOSE

    def is_collection_hours(self, dt: datetime = None) -> bool:
        """Check if a datetime falls in the 08:45-15:00 ingestion window of a trading day.

        Kept separate from ``is_market_hours``, which the monitor uses for alert thresholds.
        """
        if dt is None:
            dt = datetime.now(ICT)
        if dt.weekday() not in TRADING_DAYS:
            return False
        is_holiday, _ = self.is_market_holiday(dt.date())
        if is_holiday:
            return False
        return COLLECTION_OPEN <= dt.time() <= COLLECTION_CLOSE

    def time_until_collection_open(self, from_dt: datetime = None) -> float:
        """Seconds until the ingestion window next opens (08:45, not 09:00)."""
        if from_dt is None:
            from_dt = datetime.now(ICT)

        # the lunch break lies inside the broad window, so handle it before the short-circuit
        if (
            from_dt.weekday() in TRADING_DAYS
            and self.is_trading_day(from_dt.date())
            and MORNING_CLOSE <= from_dt.time() < AFTERNOON_OPEN
        ):
            afternoon_open = from_dt.replace(
                hour=AFTERNOON_OPEN.hour, minute=AFTERNOON_OPEN.minute, second=0, microsecond=0
            )
            return (afternoon_open - from_dt).total_seconds()

        if self.is_collection_hours(from_dt):
            return 0.0

        days_ahead = 0
        while days_ahead <= 7:
            candidate = from_dt + timedelta(days=days_ahead)
            candidate_open = candidate.replace(
                hour=COLLECTION_OPEN.hour, minute=COLLECTION_OPEN.minute, second=0, microsecond=0
            )
            if candidate_open > from_dt and self.is_trading_day(candidate.date()):
                return (candidate_open - from_dt).total_seconds()
            days_ahead += 1

        return 86400.0  # Fallback: 24 hours

    def is_market_holiday(self, check_date: date) -> Tuple[bool, str]:
        """Return ``(is_holiday, reason)``; weekends count as holidays."""
        if check_date.weekday() >= 5:
            return True, f"Weekend ({check_date.strftime('%A')})"

        if (check_date.day, check_date.month) in FIXED_HOLIDAYS:
            names = {
                (1, 1): "New Year's Day",
                (30, 4): "Reunification Day",
                (1, 5): "Labor Day",
                (2, 9): "National Day",
            }
            name = names.get((check_date.day, check_date.month), "Holiday")
            return True, f"Holiday ({name})"

        movable = MOVABLE_HOLIDAYS.get(check_date.year, [])
        if (check_date.month, check_date.day) in movable:
            return True, "Holiday (movable)"

        return False, ""

    def time_until_market_open(self, from_dt: datetime = None) -> float:
        """Seconds until the next market open from given datetime."""
        if from_dt is None:
            from_dt = datetime.now(ICT)

        # the lunch break lies inside the broad window, so handle it before the short-circuit
        if (
            from_dt.weekday() in TRADING_DAYS
            and self.is_trading_day(from_dt.date())
            and MORNING_CLOSE <= from_dt.time() < AFTERNOON_OPEN
        ):
            afternoon_open = from_dt.replace(
                hour=AFTERNOON_OPEN.hour, minute=AFTERNOON_OPEN.minute, second=0, microsecond=0
            )
            return (afternoon_open - from_dt).total_seconds()

        if self.is_market_hours(from_dt):
            return 0.0

        days_ahead = 0
        while days_ahead <= 7:
            candidate = from_dt + timedelta(days=days_ahead)
            candidate_open = candidate.replace(
                hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute, second=0, microsecond=0
            )
            if candidate_open > from_dt and self.is_trading_day(candidate.date()):
                return (candidate_open - from_dt).total_seconds()
            days_ahead += 1

        return 86400.0  # Fallback: 24 hours
