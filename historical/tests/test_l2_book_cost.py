"""Book-cost ``ob_*`` features: hand-computed golden vectors, property tests and mutation tests."""

from __future__ import annotations

import json
import random
import sys
import warnings
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from historical.l2.book import (  # noqa: E402
    BookError,
    BookState,
    group_by_minute,
    minute_key,
    minute_of_session,
    read_book,
)
from historical.l2.producers.book_cost import (  # noqa: E402
    DEFAULT_LEVELS,
    FEATURES,
    depth_top10_lot,
    hidden_imb_ratio,
    hidden_lot,
    hidden_sides_lot,
    microprice_pts,
    produce,
    reduce_minutes,
    spread_pts,
    spread_tod_ratio,
)
from historical.l2.tape import ict_string_to_epoch  # noqa: E402
from historical.research_lab.feature_boundary import load_feature  # noqa: E402

FIXTURE = _REPO_ROOT / "historical" / "tests" / "fixtures" / "book_cost_book_synthetic_20210304_20210305.csv"
MANIFEST = FIXTURE.with_suffix(".manifest.json")
SPECS = _REPO_ROOT / "specs" / "features"

#: Specs produced by ``book_cost``; the other ``ob_*`` specs come from ``daily_micro``.
OWNED = FEATURES


# --- hand-computed golden book ---
#: Dyadic prices and integer sizes: every derived value is exact in f64, so each expectation
#: below can be checked on paper (0.1-point ticks are not exact in binary).
GOLDEN = BookState(
    epoch_s=ict_string_to_epoch("2026-08-06 09:00:00"),
    bid_prices=(1913.50, 1913.25),
    bid_volumes=(4, 10),
    offer_prices=(1914.00, 1914.25),
    offer_volumes=(12, 6),
    total_bid_qty=500,
    total_offer_qty=300,
)
GOLDEN_LEVELS = 2


def test_golden_book_is_exactly_representable():
    """Every golden price is a multiple of 1/4, the premise of the hand computations."""
    for price in GOLDEN.bid_prices + GOLDEN.offer_prices:
        assert (price * 4).is_integer(), f"{price} is not a multiple of 1/4"


# --- golden vectors ---
def test_spread_matches_hand_computed_golden_vector():
    """offer_prices[0] - bid_prices[0] = 1914.00 - 1913.50 = 0.5."""
    assert spread_pts(GOLDEN) == 0.5


def test_microprice_matches_hand_computed_golden_vector():
    """Stoikov micro-price: (12 * 1913.50 + 4 * 1914.00) / (12 + 4) = 30618 / 16 = 1913.625.

    The ask queue is three times the bid queue, so the price sits nearer the bid.
    """
    assert microprice_pts(GOLDEN) == 1913.625


def test_depth_top10_matches_hand_computed_golden_vector():
    """0.5 * ((4 + 10) + (12 + 6)) = 16.0 over the golden book's two levels."""
    assert depth_top10_lot(GOLDEN, GOLDEN_LEVELS) == 16.0


def test_hidden_lot_matches_hand_computed_golden_vector():
    """Hidden bid 500 - 14 = 486, hidden offer 300 - 18 = 282; 0.5 * (486 + 282) = 384.0."""
    assert hidden_sides_lot(GOLDEN, GOLDEN_LEVELS) == (486, 282)
    assert hidden_lot(GOLDEN, GOLDEN_LEVELS) == 384.0


def test_hidden_imb_ratio_matches_hand_computed_golden_vector():
    """(486 - 282) / (486 + 282) = 204 / 768 = 17/64 = 0.265625.

    The sign is asserted: a mutant with swapped sides has the same magnitude.
    """
    assert hidden_imb_ratio(GOLDEN, GOLDEN_LEVELS) == 0.265625


# --- the time-of-day baseline, hand-computed over four synthetic sessions ---
def _flat_book(day: str, hhmm: str, spread: float) -> BookState:
    """One-level book quoted 1900.0 / 1900.0 + ``spread``."""
    return BookState(
        epoch_s=ict_string_to_epoch(f"{day} {hhmm}:00"),
        bid_prices=(1900.0,),
        bid_volumes=(10,),
        offer_prices=(1900.0 + spread,),
        offer_volumes=(10,),
        total_bid_qty=1000,
        total_offer_qty=1000,
    )


#: Four sessions x two minutes of session (09:00, 13:00); every spread is a multiple of 1/4.
TOD_DAYS = ("2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06")
TOD_AM = (0.5, 1.0, 1.5, 0.5)
TOD_PM = (4.0, 4.0, 4.0, 4.0)
TOD_BOOK = [
    book
    for day, am, pm in zip(TOD_DAYS, TOD_AM, TOD_PM)
    for book in (_flat_book(day, "09:00", am), _flat_book(day, "13:00", pm))
]


def test_spread_tod_ratio_matches_hand_computed_golden_vector():
    """Baseline: median spread at the same minute of session over earlier sessions only.

    With ``baseline_min_sessions=2`` days 1-2 emit nothing; day 3 gives 1.5 / 0.75 = 2.0 and
    4.0 / 4.0 = 1.0; day 4 gives 0.5 / 1.0 = 0.5 and 1.0. A baseline pooled across minutes
    would give 1.5 / 2.5 = 0.6 on day 3, so 2.0 also shows that the minutes do not pool.
    """
    rows = spread_tod_ratio(TOD_BOOK, baseline_sessions=20, baseline_min_sessions=2)
    assert [r.value for r in rows] == [2.0, 1.0, 0.5, 1.0]
    # days 1 and 2 emit no row rather than a placeholder 1.0
    assert [minute_of_session(r.epoch_s) for r in rows] == [540, 780, 540, 780]


def test_spread_tod_ratio_is_silent_during_warmup_rather_than_neutral():
    """A placeholder 1.0 would claim a normal spread where no baseline exists yet."""
    one_session = [b for b in TOD_BOOK if b.epoch_s < ict_string_to_epoch("2026-08-04 00:00:00")]
    assert spread_tod_ratio(one_session, baseline_min_sessions=1) == []


# --- properties over the fixture ---
@pytest.fixture(scope="module")
def states() -> list[BookState]:
    """Every frozen book state of the fixture."""
    assert FIXTURE.exists(), f"missing fixture {FIXTURE}; run samples/generate-fixtures.py"
    return read_book(FIXTURE)


def test_fixture_matches_its_manifest(states):
    """The fixture's LF-normalised SHA-256 and row count match its manifest."""
    import hashlib

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    digest = hashlib.sha256(FIXTURE.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    assert digest == manifest["sha256_lf"]
    assert len(states) == manifest["rows"]


def test_book_carries_ten_levels_on_every_row(states):
    """Every fixture row carries ten non-empty levels per side, as the specs declare."""
    assert all(s.levels == 10 for s in states)
    assert not any(v == 0 for s in states for v in s.bid_volumes + s.offer_volumes)


def test_spread_is_non_negative_on_every_real_state(states):
    """The fixture has no crossed book; a crossed book still gives a negative spread.

    Matches ``dom_snapshot_spread_can_be_negative_for_crossed_book`` in ``market-dom-engine``.
    """
    assert all(spread_pts(s) >= 0.0 for s in states)
    crossed = BookState(
        epoch_s=0,
        bid_prices=(1900.5,),
        bid_volumes=(1,),
        offer_prices=(1900.0,),
        offer_volumes=(1,),
        total_bid_qty=1,
        total_offer_qty=1,
    )
    assert spread_pts(crossed) == -0.5


def test_microprice_always_lies_between_the_quotes(states):
    """A convex combination of the quotes; checked on the fixture and on 2,000 random books."""
    for s in states:
        mp = microprice_pts(s)
        assert s.bid_prices[0] <= mp <= s.offer_prices[0]

    rng = random.Random(20260906)
    for _ in range(2000):
        bid = round(rng.uniform(1500.0, 2000.0), 1)
        ask = bid + round(rng.choice([0.1, 0.2, 0.3, 0.4, 1.0]), 1)
        s = BookState(
            epoch_s=0,
            bid_prices=(bid,),
            bid_volumes=(rng.randint(1, 5000),),
            offer_prices=(ask,),
            offer_volumes=(rng.randint(1, 5000),),
            total_bid_qty=10_000,
            total_offer_qty=10_000,
        )
        assert bid <= microprice_pts(s) <= ask


def test_microprice_falls_back_to_the_arithmetic_mid_on_an_empty_book():
    empty = BookState(
        epoch_s=0,
        bid_prices=(1900.0,),
        bid_volumes=(0,),
        offer_prices=(1900.5,),
        offer_volumes=(0,),
        total_bid_qty=0,
        total_offer_qty=0,
    )
    assert microprice_pts(empty) == 1900.25


def test_hidden_depth_is_non_negative_on_every_real_state(states):
    """The formula does not clamp, so this checks the data: totals always cover the top ten."""
    sides = [hidden_sides_lot(s, 10) for s in states]
    assert all(hb >= 0 and ho >= 0 for hb, ho in sides)
    assert all(hidden_lot(s, 10) >= 0.0 for s in states)


def test_hidden_depth_dominates_the_visible_book(states):
    """Mean hidden bid exceeds ten times the mean visible top-10 bid (order of magnitude only)."""
    n = len(states)
    visible_bid = sum(sum(s.bid_volumes) for s in states) / n
    hidden_bid = sum(hidden_sides_lot(s, 10)[0] for s in states) / n
    assert hidden_bid > 10 * visible_bid


def test_hidden_imbalance_is_bounded_and_follows_the_larger_side(states):
    for s in states:
        hb, ho = hidden_sides_lot(s, 10)
        r = hidden_imb_ratio(s, 10)
        assert -1.0 <= r <= 1.0
        if hb > ho:
            assert r > 0
        elif ho > hb:
            assert r < 0
        else:
            assert r == 0.0


def test_hidden_imbalance_is_zero_when_there_is_no_hidden_size():
    """Zero, not NaN: a NaN would drop the row out of any downstream mean."""
    flat = BookState(
        epoch_s=0,
        bid_prices=(1900.0,),
        bid_volumes=(7,),
        offer_prices=(1900.5,),
        offer_volumes=(9,),
        total_bid_qty=7,
        total_offer_qty=9,
    )
    assert hidden_sides_lot(flat, 1) == (0, 0)
    assert hidden_imb_ratio(flat, 1) == 0.0


def test_depth_is_half_the_two_side_sums_on_every_real_state(states):
    for s in states:
        assert depth_top10_lot(s, 10) == 0.5 * (sum(s.bid_volumes) + sum(s.offer_volumes))


def test_reading_fewer_levels_than_declared_is_refused():
    with pytest.raises(ValueError, match="carries 2"):
        depth_top10_lot(GOLDEN, 10)


# --- mutation tests: a formula implemented wrong must disagree ---
def test_mutant_microprice_with_swapped_weights_is_a_different_number():
    """Swapped weights give (4 * 1913.50 + 12 * 1914.00) / 16 = 1913.875.

    The mutant still lies between the quotes, so only the golden vector catches it.
    """
    q_bid, q_ask = GOLDEN.bid_volumes[0], GOLDEN.offer_volumes[0]
    p_bid, p_ask = GOLDEN.bid_prices[0], GOLDEN.offer_prices[0]
    mutant = (q_bid * p_bid + q_ask * p_ask) / (q_bid + q_ask)
    assert mutant == 1913.875
    assert microprice_pts(GOLDEN) == 1913.625
    assert p_bid < mutant < p_ask  # the mutant passes the range property
    assert mutant != microprice_pts(GOLDEN)


def test_mutant_hidden_depth_from_top10_instead_of_totals_collapses_to_zero(states):
    """Using the visible sum as the "total" gives 0.0 on every row; the real value is positive."""

    def mutant(s: BookState) -> float:
        bid, off = sum(s.bid_volumes), sum(s.offer_volumes)
        return 0.5 * ((bid - bid) + (off - off))

    assert mutant(GOLDEN) == 0.0
    assert hidden_lot(GOLDEN, GOLDEN_LEVELS) == 384.0
    assert all(mutant(s) == 0.0 for s in states)
    assert all(hidden_lot(s, 10) > 0.0 for s in states)


def test_mutant_depth_without_the_half_doubles_every_value(states):
    """Without the 0.5 the value is the two-sided total, a different quantity."""
    for s in states[:200]:
        mutant = float(sum(s.bid_volumes) + sum(s.offer_volumes))
        assert mutant == 2.0 * depth_top10_lot(s, 10)
        assert mutant != depth_top10_lot(s, 10)


def test_mutant_hidden_imbalance_with_swapped_sides_flips_every_sign(states):
    """Same magnitude, opposite sign: the reason the golden vector asserts the sign."""
    for s in states[:500]:
        hb, ho = hidden_sides_lot(s, 10)
        if hb + ho == 0 or hb == ho:
            continue
        mutant = (ho - hb) / (hb + ho)
        assert mutant == -hidden_imb_ratio(s, 10)


def test_mutant_spread_with_swapped_sides_is_negative_on_every_real_state(states):
    for s in states[:500]:
        mutant = s.bid_prices[0] - s.offer_prices[0]
        assert mutant <= 0.0
        if spread_pts(s) != 0.0:
            assert mutant != spread_pts(s)


# --- minute reduction ---
def test_one_row_per_minute_that_carried_an_update(states):
    """Six fixture minutes x two sessions = twelve rows for every feature."""
    minutes = {minute_key(s.epoch_s) for s in states}
    assert len(minutes) == 12
    for feature in OWNED:
        if feature == "ob_spread_tod_ratio_v1":
            continue  # has its own warmup; covered above
        rows = reduce_minutes(states, feature)
        assert len(rows) == 12
        assert [r.minute for r in rows] == sorted(minutes)


def test_no_row_is_emitted_for_a_minute_the_venue_did_not_update(states):
    """No rows are filled in across the 11:30-13:00 lunch break."""
    rows = reduce_minutes(states, "ob_spread_pts_v1")
    mos = [minute_of_session(r.epoch_s) for r in rows]
    assert mos == [540, 541, 689, 780, 884, 885] * 2
    assert not any(690 <= m < 780 for m in mos)
    # 11:29 -> 13:00 is one step in the output and 91 minutes on the clock.
    idx = mos.index(689)
    assert rows[idx + 1].minute - rows[idx].minute == 91


def test_no_bucket_spans_the_overnight_gap(states):
    """Each minute bucket holds states from a single minute, hence a single session."""
    for key, bucket in group_by_minute(states):
        assert {minute_key(s.epoch_s) for s in bucket} == {key}


def test_last_and_first_in_minute_are_different_reductions(states):
    """``agg`` changes the output on the fixture, so the declared reduction is observable."""
    last = reduce_minutes(states, "ob_depth_resid_lot_v1", agg="last")
    first = reduce_minutes(states, "ob_depth_resid_lot_v1", agg="first")
    assert any(a.value != b.value for a, b in zip(last, first))


def test_median_reduction_is_refused_with_the_reason_stated():
    """``median`` is outside the registry's ``agg`` vocabulary, so the producer rejects it too."""
    with pytest.raises(ValueError, match="median"):
        reduce_minutes([GOLDEN], "ob_spread_pts_v1", levels=1, agg="median")


def test_a_backwards_book_tape_is_refused():
    """File order defines the ``last``-in-minute pick, so a timestamp going backwards raises."""
    backwards = [
        _flat_book("2026-08-06", "09:01", 0.5),
        _flat_book("2026-08-06", "09:00", 0.5),
    ]
    with pytest.raises(BookError, match="goes backwards"):
        group_by_minute(backwards)


def test_cli_entry_point_produces_the_same_rows_as_the_library(states):
    """``produce``, the L2 write-path entry point, returns the same rows as the library."""
    assert produce(FIXTURE, "ob_depth_resid_lot_v1") == reduce_minutes(states, "ob_depth_resid_lot_v1")


# --- registry specs ---
@pytest.mark.parametrize("feature", OWNED)
def test_every_spec_passes_the_boundary_gate_in_strict_mode(feature):
    """Each spec loads with ``strict=True`` and with warnings raised as errors."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        entry = load_feature(SPECS / f"{feature}.yaml", strict=True)
    assert entry.feature == feature
    assert entry.family == "ob"
    assert entry.cadence == "m1"
    assert entry.parity.tolerance == 0


@pytest.mark.parametrize("feature", OWNED)
def test_declared_levels_are_not_a_second_definition(feature):
    """``DEFAULT_LEVELS`` equals ``params.default.levels`` in each spec, the source of truth."""
    raw = yaml.safe_load((SPECS / f"{feature}.yaml").read_text(encoding="utf-8"))
    assert raw["params"]["default"]["levels"] == DEFAULT_LEVELS[feature]
    assert raw["params"]["default"]["agg"] == "last"
    assert raw["params"]["default"]["stale_ms"] == 0


@pytest.mark.parametrize("feature", OWNED)
def test_a_waiver_is_only_claimed_where_no_rust_implementation_exists(feature):
    """A ``parity_waiver`` requires a Python-only implementation and a reason of 40+ characters."""
    entry = load_feature(SPECS / f"{feature}.yaml")
    waiver = entry.parity.parity_waiver
    if waiver:
        assert entry.impl.rust is None, "a waiver with a Rust implementation is refused"
        assert entry.impl.authority == "python"
        assert len(waiver.strip()) >= 40
        assert entry.status == "PARITY"
    else:
        # no waiver: a writeable status (PARITY or later) requires a Rust implementation
        assert entry.status not in ("PARITY", "BACKFILLED", "LIVE") or entry.impl.rust


def test_the_spread_feature_declares_its_rust_twin_and_does_not_waive_it():
    """``market-dom-engine`` computes the same spread, so the spec names it and takes no waiver."""
    entry = load_feature(SPECS / "ob_spread_pts_v1.yaml")
    assert entry.impl.rust == (
        "native_platform/crates/market-dom-engine/src/lib.rs::spread_from_best_levels"
    )
    assert entry.impl.authority == "rust"
    assert not entry.parity.parity_waiver
    assert entry.status == "IMPLEMENTED"  # awaits a Rust parity dump; see the spec's next_action


def test_no_owned_spec_uses_a_forbidden_cadence():
    """Every spec and param set declares ``cadence: m1``; the gate refuses ``per_update``."""
    for feature in OWNED:
        raw = yaml.safe_load((SPECS / f"{feature}.yaml").read_text(encoding="utf-8"))
        assert raw["cadence"] == "m1"
        for pset in raw["params"].values():
            assert pset["cadence"] == "m1"
