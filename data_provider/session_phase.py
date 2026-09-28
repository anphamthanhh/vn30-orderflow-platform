"""VN30F1M trading-session phase classifier (pure: no I/O, no wall clock).

Maps an exchange timestamp to a session phase by ICT second-of-day; the ingest path stores
the label with each book row. Boundaries and phase names mirror the Rust order gateway, which
is authoritative (ADR 0031). ICT is a fixed UTC+7 offset with no daylight saving. The phase is
clock-derived: where it disagrees with the venue's own session state, the venue is right.
"""

from __future__ import annotations

import math
from datetime import datetime
from enum import Enum
from typing import Optional, Union

# --- Boundaries mirrored from the Rust gateway (ICT seconds-of-day) ---

#: ICT offset in seconds (UTC+7; Vietnam has no daylight saving).
ICT_OFFSET_SECONDS: int = 7 * 3600

#: 08:45 - opening call auction (ATO) begins.
ATO_OPEN_SOD: int = 8 * 3600 + 45 * 60
#: 09:00 - continuous morning session.
CONTINUOUS_AM_OPEN_SOD: int = 9 * 3600
#: 11:30 - morning close (lunch break follows).
CONTINUOUS_AM_CLOSE_SOD: int = 11 * 3600 + 30 * 60
#: 13:00 - continuous afternoon session.
CONTINUOUS_PM_OPEN_SOD: int = 13 * 3600
#: 14:30 - closing call auction (ATC) begins.
ATC_OPEN_SOD: int = 14 * 3600 + 30 * 60
#: 14:45 - session close.
SESSION_CLOSE_SOD: int = 14 * 3600 + 45 * 60
#: Seconds after a continuous open during which market orders stay blocked: the venue's
#: phase can lag the clock (ATO has been reported at 09:00:00.249).
CONTINUOUS_OPEN_GUARD_SECONDS: int = 30

# --- end of mirrored block ---

#: Stored `session_phase` for rows written before the column existed; not a phase (not "closed").
PHASE_NOT_RECORDED: str = ""


class SessionPhase(str, Enum):
    """Session phase; the value is the label stored with each book row."""

    PRE_OPEN = "preopen"
    ATO = "ato"
    CONTINUOUS_AM = "continuous_am"
    LUNCH = "lunch"
    CONTINUOUS_PM = "continuous_pm"
    ATC = "atc"
    CLOSED = "closed"

    @property
    def is_call_auction(self) -> bool:
        """True for the call-auction (batch-matched) phases, ATO and ATC."""
        return self in (SessionPhase.ATO, SessionPhase.ATC)

    @property
    def accepts_market_orders(self) -> bool:
        """True for the continuous phases, the only ones that accept market orders."""
        return self in (SessionPhase.CONTINUOUS_AM, SessionPhase.CONTINUOUS_PM)


#: Phases in which a stored book row is call-auction depth rather than continuous depth.
CALL_AUCTION_PHASES = (SessionPhase.ATO, SessionPhase.ATC)


def ict_sod(ts: Union[int, float]) -> int:
    """Return the ICT second-of-day of a unix timestamp in seconds (fractions floored)."""
    return int(math.floor(ts + ICT_OFFSET_SECONDS)) % 86_400


def session_phase_of(ts: Union[int, float]) -> SessionPhase:
    """Return the session phase at a unix timestamp in seconds.

    A boundary second belongs to the phase it opens.
    """
    sod = ict_sod(ts)
    if sod < ATO_OPEN_SOD:
        return SessionPhase.PRE_OPEN
    if sod < CONTINUOUS_AM_OPEN_SOD:
        return SessionPhase.ATO
    if sod < CONTINUOUS_AM_CLOSE_SOD:
        return SessionPhase.CONTINUOUS_AM
    if sod < CONTINUOUS_PM_OPEN_SOD:
        return SessionPhase.LUNCH
    if sod < ATC_OPEN_SOD:
        return SessionPhase.CONTINUOUS_PM
    if sod < SESSION_CLOSE_SOD:
        return SessionPhase.ATC
    return SessionPhase.CLOSED


def sod_of_datetime(dt: datetime) -> int:
    """Return the ICT second-of-day of a datetime.

    An aware datetime goes through its epoch value. A naive datetime is read as ICT
    wall-clock time, which is how the pipeline builds it; ``.timestamp()`` would
    interpret it in the host's local zone instead.
    """
    if dt.tzinfo is None:
        return dt.hour * 3600 + dt.minute * 60 + dt.second
    return ict_sod(dt.timestamp())


def session_phase_of_datetime(dt: datetime) -> SessionPhase:
    """Return the session phase of a datetime (aware, or naive in ICT)."""
    sod = sod_of_datetime(dt)
    # reuse the single ladder: map sod back to an epoch whose ICT second-of-day is sod
    return session_phase_of(sod - ICT_OFFSET_SECONDS)


def phase_label(value: Union[int, float, datetime, None]) -> str:
    """Return the stored phase label for a timestamp; ``PHASE_NOT_RECORDED`` for ``None``."""
    if value is None:
        return PHASE_NOT_RECORDED
    if isinstance(value, datetime):
        return session_phase_of_datetime(value).value
    return session_phase_of(value).value


def in_continuous_open_guard(ts: Union[int, float]) -> bool:
    """True during the first ``CONTINUOUS_OPEN_GUARD_SECONDS`` of a continuous session."""
    phase = session_phase_of(ts)
    if not phase.accepts_market_orders:
        return False
    sod = ict_sod(ts)
    open_sod = (
        CONTINUOUS_AM_OPEN_SOD if phase is SessionPhase.CONTINUOUS_AM else CONTINUOUS_PM_OPEN_SOD
    )
    return sod < open_sod + CONTINUOUS_OPEN_GUARD_SECONDS


def mtl_block_reason(ts: Union[int, float]) -> Optional[str]:
    """Return ``None`` if a market order may be sent at ``ts``, else ``session_phase:<phase>``.

    During the open guard the reason names the phase the venue still reports (``ato`` or
    ``lunch``), so it matches the broker's error message.
    """
    phase = session_phase_of(ts)
    if not phase.accepts_market_orders:
        return f"session_phase:{phase.value}"
    if in_continuous_open_guard(ts):
        naming = SessionPhase.ATO if phase is SessionPhase.CONTINUOUS_AM else SessionPhase.LUNCH
        return f"session_phase:{naming.value}"
    return None
