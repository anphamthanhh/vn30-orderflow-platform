"""Tests for holdout_lock.json schema v3: explicit dev and reserved windows per data source."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import numpy as np
import pytest

from historical.research_lab import lab

EPOCH = dt.date(1970, 1, 1)
LOCK_PATH = Path(lab.__file__).resolve().parent / "holdout_lock.json"


def _day(iso: str) -> int:
    return (dt.date.fromisoformat(iso) - EPOCH).days


@pytest.fixture(scope="module")
def lock() -> dict:
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


# --- the lock file ---


def test_schema_is_v3(lock):
    assert lock["schema"] == "research_lab.holdout_lock/3"


def test_v2_keys_survive_v3(lock):
    """v3 only adds keys; the keys the v2 guards read are unchanged."""
    for key in ("cutoff_ict_date", "reserved_ranges", "rule", "why", "enforced_by", "spent"):
        assert key in lock, key
    assert lock["cutoff_ict_date"] == "2026-08-12"
    assert {(r["from_ict_date"], r["to_ict_date"]) for r in lock["reserved_ranges"]} == {
        ("2024-01-01", "2026-03-04")
    }


def test_the_three_burns_are_still_recorded(lock):
    """The spend records, the only evidence that a range was consumed, survive the migration."""
    assert len(lock["spent"]) == 3
    assert sum("BURNED-WITHOUT-AUTHORISATION" in s["verdict"] for s in lock["spent"]) == 2


def test_every_source_declares_a_boundary_and_a_guard(lock):
    for s in lock["sources"]:
        assert s["dev_ranges"], s["source"]
        assert s["reserved_ranges"], s["source"]
        assert "guarded_by" in s, s["source"]
        assert "spent" in s, s["source"]


def test_unguarded_sources_are_named_not_hidden(lock):
    """A source without an executable guard is listed and marked as not readable."""
    unguarded = [s["source"] for s in lock["sources"] if not s["guarded_by"]]
    assert unguarded == ["dom_snapshots"]
    row = next(s for s in lock["sources"] if s["source"] == "dom_snapshots")
    assert "NOT READABLE" in row["spent"]


# --- lab API ---


def test_dev_and_holdout_do_not_overlap():
    """No dev day is reserved, checked day by day across the bar era."""
    reserved = lab.holdout_ranges()
    for f, t in lab.dev_ranges():
        for d in range(f, t + 1):
            assert not any(
                rf <= d and (rt is None or d <= rt) for rf, rt in reserved
            ), f"dev day {EPOCH + dt.timedelta(days=d)} is also reserved"


def test_dev_ranges_are_the_two_documented_windows():
    assert lab.dev_ranges() == [
        (_day("2017-11-06"), _day("2023-12-31")),
        (_day("2026-03-05"), _day("2026-08-12")),
    ]


def test_no_dev_day_survives_the_bar_guard_being_wrong():
    """Boundary pair: the last dev day and the first reserved day of the bar lock."""
    assert not lab.is_holdout((_day("2023-12-31") * 86_400) - lab.ICT_OFFSET + 3600)
    assert lab.is_holdout((_day("2024-01-01") * 86_400) - lab.ICT_OFFSET + 3600)


def test_unknown_source_raises_rather_than_defaulting_open():
    with pytest.raises(KeyError, match="register it in holdout_lock.json"):
        lab.source_lock("some_new_feed")


def test_basis_window_is_narrower_than_the_bar_window():
    """The basis source has its own, narrower dev window; the bar-era range does not apply."""
    basis = lab.source_dev_ranges("basis_m1")
    assert basis == [(_day("2026-03-05"), _day("2026-08-12"))]
    assert _day("2017-11-06") < basis[0][0]


def test_basis_guard_rejects_the_range_x1_burned():
    burned = np.array([_day("2025-09-01"), _day("2026-01-15")], dtype="int64")
    with pytest.raises(AssertionError, match="OUTSIDE the source's DEV window"):
        lab.assert_dev_only_source("basis_m1", burned)


def test_basis_guard_rejects_post_cutoff_days():
    with pytest.raises(AssertionError):
        lab.assert_dev_only_source("basis_m1", [_day("2026-08-13")])


def test_basis_guard_accepts_its_dev_window_and_its_edges():
    lab.assert_dev_only_source("basis_m1", [_day("2026-03-05"), _day("2026-08-12")])


def test_empty_input_is_not_an_error():
    lab.assert_dev_only_source("basis_m1", [])


def test_one_bad_day_in_a_good_batch_still_raises():
    """A batch that starts inside the reserved range raises even if most days are valid."""
    days = [_day("2026-03-06"), _day("2026-03-07"), _day("2026-02-28")]
    with pytest.raises(AssertionError, match="2026-02-28"):
        lab.assert_dev_only_source("basis_m1", days)


def test_bar_source_row_matches_the_global_lock():
    """The bars_m1 row agrees with the global dev ranges that the bar guards enforce."""
    assert lab.source_dev_ranges("bars_m1") == lab.dev_ranges()


def test_late_starting_sources_do_not_inherit_the_bar_era():
    for src in ("orderbook", "trades", "profile_tpo", "foreign_flow"):
        first = lab.source_dev_ranges(src)[0][0]
        assert first > _day("2026-01-01"), src


def test_orderbook_depth_is_recorded_as_ten_levels(lock):
    """The order book is recorded at 10 levels, which bounds any queue model's depth."""
    row = next(s for s in lock["sources"] if s["source"] == "orderbook")
    assert "10" in row["grain"]
