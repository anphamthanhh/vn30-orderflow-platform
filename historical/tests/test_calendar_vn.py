"""Tests for ``historical.research_lab.calendar_vn``, the tape-derived trading calendar."""

from __future__ import annotations

import datetime as dt

import pytest

from historical.research_lab import calendar_vn as cal


# --- non-empty calendar ---
def test_trading_holidays_is_not_empty():
    """An empty holiday set would make a Tet break look like a single overnight gap."""
    assert len(cal.TRADING_HOLIDAYS) > 0
    assert len(cal.TRADING_HOLIDAYS) == 99, "derived set changed — re-run the derivation"


def test_self_check_rejects_an_empty_set():
    """An empty calendar fails the import-time self-check."""
    with pytest.raises(RuntimeError, match="empty"):
        cal._self_check(frozenset())


def test_every_year_of_the_verified_span_has_holidays():
    """Pin the per-year holiday counts of the verified span."""
    per_year: dict[int, int] = {}
    for d in cal.TRADING_HOLIDAYS:
        per_year[d.year] = per_year.get(d.year, 0) + 1
    # 2017 has none: the tape starts in November and there is no November/December holiday
    assert sorted(per_year) == [2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026]
    assert per_year == {
        2018: 11,
        2019: 11,
        2020: 10,  # the July tape gaps are not closures
        2021: 11,
        2022: 11,
        2023: 11,
        2024: 12,
        2025: 12,
        2026: 10,  # up to VERIFIED_THROUGH only
    }


# --- Tet clusters ---
@pytest.mark.parametrize(
    "cluster",
    [
        ("2018-02-14", "2018-02-15", "2018-02-16", "2018-02-19", "2018-02-20"),
        ("2020-01-23", "2020-01-24", "2020-01-27", "2020-01-28", "2020-01-29"),
        ("2023-01-20", "2023-01-23", "2023-01-24", "2023-01-25", "2023-01-26"),
        ("2025-01-27", "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31"),
        ("2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20"),
    ],
)
def test_tet_clusters_are_registered_and_labelled(cluster):
    for iso in cluster:
        assert not cal.is_trading_day(iso), f"{iso} should be a Tet closure"
        assert cal.holiday_kind(iso) == "tet"
        assert cal.holiday_confidence(iso) == "derived"


def test_tet_costs_exactly_five_sessions_every_verified_year():
    """Tet closes five sessions in each verified year, the basis of the 2027 projection."""
    per_year: dict[int, int] = {}
    for d in cal.TET_HOLIDAYS:
        if d in cal.TRADING_HOLIDAYS:
            per_year[d.year] = per_year.get(d.year, 0) + 1
    assert set(per_year.values()) == {5}
    assert len(per_year) == 9


# --- session_adjacency ---
def test_plain_overnight_is_one_night():
    assert cal.session_adjacency("2026-08-25", "2026-08-26") == 1
    assert cal.gap_kind("2026-08-25", "2026-08-26") == "overnight"


def test_friday_to_monday_is_three_nights():
    """A naive ``shift(1)`` would count this gap as one night."""
    friday = dt.date(2026, 8, 21)
    monday = dt.date(2026, 8, 24)
    assert friday.weekday() == 4 and monday.weekday() == 0
    assert cal.session_adjacency(friday, monday) == 3
    assert cal.gap_kind(friday, monday) == "weekend"


def test_tet_adjacency_is_ten_nights_not_one():
    assert cal.previous_trading_day("2026-02-16") == dt.date(2026, 2, 13)
    assert cal.next_trading_day("2026-02-20") == dt.date(2026, 2, 23)
    assert cal.session_adjacency("2026-02-13", "2026-02-23") == 10
    assert cal.gap_kind("2026-02-13", "2026-02-23") == "tet"


def test_tet_2025_adjacency():
    """Tet 2025 also spans ten nights, Friday to Monday."""
    assert cal.session_adjacency("2025-01-24", "2025-02-03") == 10
    assert cal.gap_kind("2025-01-24", "2025-02-03") == "tet"


def test_non_tet_holiday_gap_is_labelled_holiday_not_tet():
    """Reunification Day and Labour Day 2025 (Wednesday to Friday) form a holiday gap."""
    assert cal.session_adjacency("2025-04-29", "2025-05-05") == 6
    assert cal.gap_kind("2025-04-29", "2025-05-05") == "holiday"


def test_single_holiday_midweek_is_a_holiday_gap():
    """Hung Kings Day 2024 fell on a Thursday: Wednesday to Friday is two nights."""
    assert cal.session_adjacency("2024-04-17", "2024-04-19") == 2
    assert cal.gap_kind("2024-04-17", "2024-04-19") == "holiday"
    assert cal.holiday_kind("2024-04-18") == "hung_kings"


def test_session_adjacency_rejects_a_non_forward_pair():
    with pytest.raises(ValueError):
        cal.session_adjacency("2026-08-26", "2026-08-26")
    with pytest.raises(ValueError):
        cal.session_adjacency("2026-08-26", "2026-08-25")


def test_gap_kind_never_returns_an_undocumented_label():
    allowed = {"overnight", "weekend", "holiday", "tet", "data_gap"}
    day = dt.date(2018, 1, 1)
    while day < dt.date(2026, 8, 28):
        nxt = cal.next_trading_day(day)
        if cal.is_trading_day(day):
            assert cal.gap_kind(day, nxt) in allowed
        day = nxt


# --- is_trading_day ---
def test_saturday_and_sunday_are_never_trading_days():
    sat = dt.date(2026, 8, 22)
    sun = dt.date(2026, 8, 23)
    assert sat.weekday() == 5 and sun.weekday() == 6
    assert cal.is_weekend(sat) and cal.is_weekend(sun)
    assert not cal.is_trading_day(sat)
    assert not cal.is_trading_day(sun)
    # a weekend is not a holiday and is absent from the holiday sets
    assert not cal.is_holiday(sat)
    assert sat not in cal.TRADING_HOLIDAYS


def test_ordinary_weekday_is_a_trading_day():
    assert cal.is_trading_day("2026-08-26")
    assert cal.holiday_kind("2026-08-26") is None


def test_accepts_date_datetime_and_iso_string():
    for value in (dt.date(2026, 2, 17), dt.datetime(2026, 2, 17, 9, 30), "2026-02-17"):
        assert not cal.is_trading_day(value)
    with pytest.raises(TypeError):
        cal.is_trading_day(20260217)


# --- verified vs provisional holidays ---
def test_verified_and_provisional_are_disjoint():
    assert not (cal.TRADING_HOLIDAYS & cal.PROVISIONAL_HOLIDAYS)


def test_verified_set_stops_at_the_end_of_the_tape():
    assert max(cal.TRADING_HOLIDAYS) <= cal.VERIFIED_THROUGH
    assert min(cal.TRADING_HOLIDAYS) >= cal.VERIFIED_FROM


def test_provisional_set_starts_after_the_tape():
    assert min(cal.PROVISIONAL_HOLIDAYS) > cal.VERIFIED_THROUGH
    assert max(cal.PROVISIONAL_HOLIDAYS) <= cal.PROVISIONAL_THROUGH


def test_provisional_dates_are_flagged_as_predictions():
    assert cal.holiday_confidence("2027-02-08") == "statutory"
    assert cal.holiday_confidence("2026-09-02") == "announced"
    assert cal.holiday_confidence("2025-01-29") == "derived"


def test_a_caller_can_refuse_predictions():
    """With ``include_provisional=False`` the projected 2027 holidays read as trading days."""
    assert not cal.is_trading_day("2027-02-08")
    assert cal.is_trading_day("2027-02-08", include_provisional=False)
    assert not cal.is_holiday("2027-02-08", include_provisional=False)
    # verified history is unaffected by the switch
    assert not cal.is_trading_day("2025-01-29", include_provisional=False)


# --- tape gaps are not holidays ---
def test_tape_gaps_are_trading_days():
    """The market traded on these days; only the recorded data is missing."""
    for iso in ("2020-07-16", "2020-07-17", "2020-07-20", "2026-02-27"):
        assert cal.is_trading_day(iso), f"{iso} is a tape gap, not a closure"
        assert cal.holiday_kind(iso) is None
        assert dt.date.fromisoformat(iso) not in cal.TRADING_HOLIDAYS


def test_a_jump_across_a_tape_gap_is_flagged_data_gap():
    """Days adjacent on the tape but not consecutive sessions are labelled ``data_gap``."""
    assert cal.session_adjacency("2020-07-15", "2020-07-21") == 6
    assert cal.gap_kind("2020-07-15", "2020-07-21") == "data_gap"
    assert cal.skipped_trading_days("2020-07-15", "2020-07-21") == [
        dt.date(2020, 7, 16),
        dt.date(2020, 7, 17),
        dt.date(2020, 7, 20),
    ]


def test_consecutive_sessions_skip_nothing():
    assert cal.skipped_trading_days("2026-02-13", "2026-02-23") == []
    assert cal.skipped_trading_days("2026-08-21", "2026-08-24") == []


# --- navigation ---
def test_previous_and_next_are_strict():
    """Both move off ``d`` even when ``d`` is a trading day, so a session walk advances."""
    assert cal.next_trading_day("2026-08-26") == dt.date(2026, 8, 27)
    assert cal.previous_trading_day("2026-08-26") == dt.date(2026, 8, 25)


def test_navigation_round_trips_across_tet():
    assert cal.previous_trading_day(cal.next_trading_day("2026-02-13")) == dt.date(2026, 2, 13)


def test_trading_days_range_excludes_the_whole_tet_break():
    got = list(cal.trading_days("2026-02-13", "2026-02-23"))
    assert got == [dt.date(2026, 2, 13), dt.date(2026, 2, 23)]


# --- NON_TRADING_DAYS alias ---
def test_non_trading_days_alias_matches_the_type_it_replaces():
    """``NON_TRADING_DAYS`` is a ``frozenset`` of ISO date strings, as forward_registry expects."""
    assert isinstance(cal.NON_TRADING_DAYS, frozenset)
    assert cal.NON_TRADING_DAYS
    assert all(isinstance(s, str) and len(s) == 10 for s in cal.NON_TRADING_DAYS)
    assert "2025-01-29" in cal.NON_TRADING_DAYS  # Tet 2025, verified
    assert "2026-09-02" in cal.NON_TRADING_DAYS  # National Day 2026, announced


def test_non_trading_days_excludes_tape_gaps():
    """Missing data on a real session is not hidden as a holiday."""
    for iso in ("2020-07-16", "2026-02-27"):
        assert iso not in cal.NON_TRADING_DAYS


def test_non_trading_days_is_what_the_existing_probe_would_find():
    """Coercing each entry with ``str(v)[:10]`` yields the same set."""
    value = getattr(cal, "NON_TRADING_DAYS", None)
    assert value
    assert frozenset(str(v)[:10] for v in value) == cal.NON_TRADING_DAYS


# --- provenance ---
def test_docstring_states_the_derivation():
    doc = cal.__doc__ or ""
    assert "ohlcv_m1" in doc
    assert "VN30F1M" in doc
    assert "DERIVED FROM THE TAPE" in doc
    assert "VN30F1M" in cal.DERIVATION_QUERY
    assert "ohlcv_m1" in cal.DERIVATION_QUERY
