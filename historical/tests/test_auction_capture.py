"""Tests for the session-phase classifier that labels archived depth (ADR 0031)."""

from __future__ import annotations

import importlib.util
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLASSIFIER_PATH = REPO_ROOT / "data_provider" / "session_phase.py"


def _load_classifier():
    """Load the dependency-free classifier by file path, bypassing the ``data_provider`` package."""
    spec = importlib.util.spec_from_file_location("dp_session_phase", CLASSIFIER_PATH)
    assert spec and spec.loader, f"cannot load {CLASSIFIER_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sp = _load_classifier()

ICT = timezone(timedelta(hours=7))


def ict(hour: int, minute: int, second: int = 0, micro: int = 0) -> float:
    """Return the Unix epoch (seconds) of an ICT wall-clock time on a fixed trading day."""
    return datetime(2026, 8, 27, hour, minute, second, micro, tzinfo=ICT).timestamp()


# --- phase ladder: every boundary second, from both sides ---

BOUNDARY_CASES = [
    # (h, m, s, expected phase value)
    (0, 0, 0, "preopen"),
    (8, 44, 59, "preopen"),
    (8, 45, 0, "ato"),  # ATO opens on the boundary second
    (8, 59, 59, "ato"),
    (9, 0, 0, "continuous_am"),
    (11, 29, 59, "continuous_am"),
    (11, 30, 0, "lunch"),
    (12, 59, 59, "lunch"),
    (13, 0, 0, "continuous_pm"),
    (14, 29, 59, "continuous_pm"),
    (14, 30, 0, "atc"),  # ATC opens on the boundary second
    (14, 44, 59, "atc"),
    (14, 45, 0, "closed"),  # session close is exclusive of ATC
    (23, 59, 59, "closed"),
]


@pytest.mark.parametrize("hour,minute,second,expected", BOUNDARY_CASES)
def test_phase_at_boundary_second(hour, minute, second, expected):
    assert sp.session_phase_of(ict(hour, minute, second)).value == expected


@pytest.mark.parametrize("hour,minute,second,expected", BOUNDARY_CASES)
def test_datetime_path_agrees_with_epoch_path(hour, minute, second, expected):
    aware = datetime(2026, 8, 27, hour, minute, second, tzinfo=ICT)
    assert sp.session_phase_of_datetime(aware).value == expected


def test_phase_ladder_is_total_and_gapless():
    """Every second of the day resolves, and the phases appear in venue order."""
    seen = []
    for sod in range(86_400):
        phase = sp.session_phase_of(sod - sp.ICT_OFFSET_SECONDS).value
        if not seen or seen[-1] != phase:
            seen.append(phase)
    assert seen == [
        "preopen",
        "ato",
        "continuous_am",
        "lunch",
        "continuous_pm",
        "atc",
        "closed",
    ]


# --- late ATO at 09:00:00.249 ---
# The exchange can still report ATO just after 09:00. The clock phase is continuous_am,
# while MTL eligibility stays blocked and names the phase the exchange reports.


def test_late_ato_quirk_clock_phase_is_continuous():
    assert sp.session_phase_of(ict(9, 0, 0, 249_000)).value == "continuous_am"
    assert sp.phase_label(ict(9, 0, 0, 249_000)) == "continuous_am"


def test_late_ato_quirk_order_is_still_blocked_and_named_ato():
    assert sp.mtl_block_reason(ict(9, 0, 0, 249_000)) == "session_phase:ato"
    assert sp.in_continuous_open_guard(ict(9, 0, 0, 249_000)) is True


def test_continuous_open_guard_expiry_matches_rust():
    # 30 s open guard: 09:00:29 blocked, 09:00:30 clear
    assert sp.mtl_block_reason(ict(9, 0, 29)) == "session_phase:ato"
    assert sp.mtl_block_reason(ict(9, 0, 30)) is None
    # the afternoon guard names lunch, the phase the exchange reports then
    assert sp.mtl_block_reason(ict(13, 0, 10)) == "session_phase:lunch"
    assert sp.mtl_block_reason(ict(13, 0, 30)) is None


@pytest.mark.parametrize("hour,minute", [(8, 50), (14, 35)])
def test_call_auctions_never_accept_market_orders(hour, minute):
    ts = ict(hour, minute)
    phase = sp.session_phase_of(ts)
    assert phase.is_call_auction
    assert phase.accepts_market_orders is False
    assert sp.mtl_block_reason(ts) == f"session_phase:{phase.value}"


# --- sub-second handling ---


@pytest.mark.parametrize("micro", [0, 1, 249_000, 999_999])
def test_subsecond_never_crosses_a_boundary(micro):
    """14:29:59.999999 is still continuous_pm; 14:30:00.000000 is atc."""
    assert sp.session_phase_of(ict(14, 29, 59, micro)).value == "continuous_pm"
    assert sp.session_phase_of(ict(14, 30, 0, micro)).value == "atc"


def test_ict_sod_floors_rather_than_rounds():
    assert sp.ict_sod(ict(14, 29, 59, 999_999)) == 14 * 3600 + 29 * 60 + 59


# --- fixed ICT offset, no DST ---


def test_offset_is_fixed_seven_hours():
    assert sp.ICT_OFFSET_SECONDS == 7 * 3600


@pytest.mark.parametrize("month,day", [(1, 15), (3, 30), (6, 21), (8, 27), (11, 3), (12, 31)])
def test_no_dst_shift_across_the_year(month, day):
    """07:30 UTC maps to 14:30 ICT (ATC open) on dates spread across the year."""
    utc_instant = datetime(2026, month, day, 7, 30, 0, tzinfo=timezone.utc)
    assert sp.session_phase_of(utc_instant.timestamp()).value == "atc"
    assert sp.session_phase_of_datetime(utc_instant).value == "atc"


def test_naive_datetime_is_read_as_ict_wall_clock():
    """A naive datetime is read as ICT wall-clock time, not in the host's zone."""
    naive = datetime(2026, 8, 27, 14, 35, 0)
    assert sp.session_phase_of_datetime(naive).value == "atc"
    assert sp.phase_label(naive) == "atc"
    # the aware equivalent agrees
    assert sp.phase_label(naive.replace(tzinfo=ICT)) == "atc"


def test_utc_aware_datetime_is_converted_not_truncated():
    utc_aware = datetime(2026, 8, 27, 7, 35, 0, tzinfo=timezone.utc)  # 14:35 ICT
    assert sp.phase_label(utc_aware) == "atc"


# --- "not recorded" is not a phase ---


def test_missing_timestamp_yields_not_recorded_never_a_guess():
    assert sp.phase_label(None) == sp.PHASE_NOT_RECORDED
    assert sp.PHASE_NOT_RECORDED == ""
    assert sp.PHASE_NOT_RECORDED not in {p.value for p in sp.SessionPhase}


# --- collection window covers the auctions ---


def test_collection_window_covers_both_call_auctions():
    """Collection opens by the 08:45 ATO and closes at or after the 14:45 session close."""
    spec = importlib.util.spec_from_file_location(
        "dp_market_calendar",
        REPO_ROOT / "data_provider" / "api" / "market_calendar.py",
    )
    assert spec and spec.loader
    cal_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cal_mod)
    cal = cal_mod.VN30MarketCalendar()

    open_sod = cal_mod.COLLECTION_OPEN.hour * 3600 + cal_mod.COLLECTION_OPEN.minute * 60
    close_sod = cal_mod.COLLECTION_CLOSE.hour * 3600 + cal_mod.COLLECTION_CLOSE.minute * 60
    assert open_sod <= sp.ATO_OPEN_SOD
    assert close_sod >= sp.SESSION_CLOSE_SOD

    thursday = datetime(2026, 8, 27, tzinfo=ICT)  # a trading day
    for hour, minute in [(8, 45), (8, 59), (9, 0), (14, 30), (14, 44)]:
        moment = thursday.replace(hour=hour, minute=minute)
        assert cal.is_collection_hours(moment), f"{hour:02d}:{minute:02d} not collected"

    # wider than the alerting window, which starts at 09:00 so a quiet auction raises no alert
    assert cal.is_collection_hours(thursday.replace(hour=8, minute=50)) is True
    assert cal.is_market_hours(thursday.replace(hour=8, minute=50)) is False

    # weekends and holidays stay excluded
    saturday = datetime(2026, 8, 29, 8, 50, tzinfo=ICT)
    assert cal.is_collection_hours(saturday) is False
    national_day = datetime(2026, 9, 2, 8, 50, tzinfo=ICT)
    assert cal.is_collection_hours(national_day) is False


def test_time_until_collection_open_targets_0845():
    spec = importlib.util.spec_from_file_location(
        "dp_market_calendar2",
        REPO_ROOT / "data_provider" / "api" / "market_calendar.py",
    )
    assert spec and spec.loader
    cal_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cal_mod)
    cal = cal_mod.VN30MarketCalendar()

    # 08:00 Thursday: 45 minutes until the ATO window opens
    before_open = datetime(2026, 8, 27, 8, 0, tzinfo=ICT)
    assert cal.time_until_collection_open(before_open) == 45 * 60
    # inside the window: 0
    assert cal.time_until_collection_open(datetime(2026, 8, 27, 8, 50, tzinfo=ICT)) == 0.0
    # at lunch the next open is the afternoon session, not tomorrow
    assert cal.time_until_collection_open(datetime(2026, 8, 27, 12, 0, tzinfo=ICT)) == 60 * 60
