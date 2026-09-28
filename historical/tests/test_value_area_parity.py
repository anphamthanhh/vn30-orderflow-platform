"""Cross-stack value-area parity: the Python half of the shared golden fixture."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared.quant_core.profile import value_area

# Also read by market-profile-engine value_area_matches_the_cross_stack_golden_fixture.
GOLDEN = (
    Path(__file__).resolve().parents[2]
    / "shared"
    / "quant_core"
    / "testdata"
    / "value_area_golden.json"
)


def _cases():
    doc = json.loads(GOLDEN.read_text(encoding="utf-8"))
    return [(c["name"], c["value_area_pct"], c["rows"], c["expected"]) for c in doc["cases"]]


def test_golden_fixture_exists_and_is_populated():
    cases = _cases()
    assert len(cases) >= 5, "the fixture is the cross-stack contract; keep it meaningful"


@pytest.mark.parametrize("name,pct,rows,expected", _cases())
def test_value_area_matches_golden(name, pct, rows, expected):
    poc, val, vah = value_area(rows, pct)
    assert poc == pytest.approx(expected["poc"]), f"{name}: POC"
    assert val == pytest.approx(expected["val"]), f"{name}: VAL"
    assert vah == pytest.approx(expected["vah"]), f"{name}: VAH"


# --- properties ---
def test_value_area_is_always_contiguous_and_contains_the_poc():
    rows = [(900.0, 9), (900.1, 1), (900.2, 2), (900.3, 6), (900.4, 10), (900.5, 7)]
    poc, val, vah = value_area(rows)
    assert val <= poc <= vah
    inside = [p for p, _ in rows if val <= p <= vah]
    # contiguous over the price grid present in the profile
    present = [p for p, _ in rows]
    lo_i, hi_i = present.index(val), present.index(vah)
    assert inside == present[lo_i : hi_i + 1]


def test_greedy_top_n_would_have_reported_a_disjoint_band():
    """A greedy top-N band spans the empty middle; the walk outward from the POC does not.

    Volume sits at the top of the profile with an isolated shelf below, so a greedy
    selection pairs the two and reports a band across the gap.
    """
    rows = [
        (900.0, 5),
        (900.1, 1),
        (900.2, 1),
        (900.3, 1),
        (900.4, 5),
        (900.5, 1),
        (900.6, 20),
    ]
    # greedy top-N selection
    total = sum(v for _, v in rows)
    cum, picked = 0, []
    for price, vol in sorted(rows, key=lambda r: r[1], reverse=True):
        cum += vol
        picked.append(price)
        if cum / total >= 0.70:
            break
    greedy_band = (min(picked), max(picked))
    assert greedy_band == (900.0, 900.6), "setup: greedy spans the empty middle"
    assert set(picked) == {900.6, 900.0}, "setup: the greedy set is DISJOINT"

    poc, val, vah = value_area(rows)
    assert poc == pytest.approx(900.6)
    assert (val, vah) != greedy_band, "canonical rule must not span the empty middle"
    assert (val, vah) == (pytest.approx(900.4), pytest.approx(900.6))


def test_rejects_invalid_pct_and_zero_weight():
    with pytest.raises(ValueError):
        value_area([(900.0, 1)], 0.0)
    with pytest.raises(ValueError):
        value_area([(900.0, 1)], 1.5)
    with pytest.raises(ValueError):
        value_area([(900.0, 0), (900.1, 0)])


def test_empty_profile_is_none():
    assert value_area([]) is None
    assert value_area({}) is None


def test_accepts_mapping_and_pair_forms_identically():
    rows = [(900.0, 3), (900.1, 9), (900.2, 4)]
    assert value_area(rows) == value_area(dict(rows))


