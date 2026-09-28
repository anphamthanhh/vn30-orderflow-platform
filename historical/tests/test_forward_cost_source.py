"""The forward scorer prices trades from ``costs.py`` and passes ``hold_days`` to the fee."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from historical.backtest_engine.costs import MULTIPLIER, POSITION_FEE_VND_PER_DAY, CostScenario
from historical.research_lab import forward_registry as fr

FORWARD_DIR = Path(fr.__file__).resolve().parent / "forward"
POSITION_FEE_PTS_PER_DAY = POSITION_FEE_VND_PER_DAY / MULTIPLIER  # 0.0255


# --- costs.py is the cost source ---
def test_cost_source_is_the_authority_not_a_local_copy():
    assert fr._COSTS_SOURCE == "historical.backtest_engine.costs", (
        "the forward scorer is not using costs.py. This is the exact failure that "
        "went unnoticed from the module's creation until 2026-09-02: a soft-import "
        "that never succeeded."
    )


def test_scenarios_are_cost_scenario_members():
    assert fr.WORSE_SIDE is CostScenario.WORSE_SIDE
    assert fr.REALISTIC is CostScenario.REALISTIC
    assert fr.STATUTORY is CostScenario.STATUTORY


def test_scenario_adapter_reads_the_enum_half_spread():
    # same names and half-spreads as the rows already in the ledgers
    assert fr._scn_name(fr.WORSE_SIDE) == "WORSE_SIDE"
    assert fr._half_spread(fr.WORSE_SIDE) == pytest.approx(0.5)
    assert fr._half_spread(fr.REALISTIC) == pytest.approx(0.1)
    assert fr._half_spread(fr.STATUTORY) == pytest.approx(0.0)


def test_no_silent_fallback_scenario_class_remains():
    assert not hasattr(fr, "_LocalCostScenario"), (
        "the local cost-scenario fallback is back. If costs.py cannot be imported "
        "the forward lane must STOP, not price trades from a copy."
    )


# --- hold_days reaches the fee ---
@pytest.mark.parametrize("price", [800.0, 1242.4, 1900.0])
def test_hold_days_charges_the_position_fee_per_calendar_day(price):
    base = fr._round_trip_fee_points(price, 0)
    for days in (1, 3, 10):
        got = fr._round_trip_fee_points(price, days)
        assert got - base == pytest.approx(days * POSITION_FEE_PTS_PER_DAY, abs=1e-12)


def test_intraday_default_is_unchanged():
    # a price-only call means intraday
    assert fr._round_trip_fee_points(1242.4) == fr._round_trip_fee_points(1242.4, 0)


def _plan(hold_days: int) -> fr.TradePlan:
    return fr.TradePlan(
        card_id="TEST",
        date_D="2026-08-14",
        date_D1="2026-08-17",
        side=1,
        entry_mid=1900.0,
        exit_mid=1905.0,
        hold_days=hold_days,
    )


def test_score_plan_charges_the_plans_hold_days():
    intraday = fr.score_plan(_plan(0), fr.WORSE_SIDE)
    weekend = fr.score_plan(_plan(3), fr.WORSE_SIDE)
    assert weekend["fee_pts"] > intraday["fee_pts"]
    assert weekend["gross_pts"] == intraday["gross_pts"]  # only the fee moves
    assert intraday["net_pts"] - weekend["net_pts"] == pytest.approx(
        3 * POSITION_FEE_PTS_PER_DAY, abs=2e-3  # rows are rounded to 3dp
    )


def test_make_row_records_the_fee_basis():
    row = fr.make_row(_plan(3))
    assert row["hold_days"] == 3, "a ledger row must carry the convention it was priced under"


def test_n1_is_declared_as_an_overnight_card():
    # N1 is the registry's only overnight card; checked from source, without the tape
    import inspect

    src = inspect.getsource(fr.n1_plans)
    assert "hold_days=" in src, "n1_plans no longer declares hold_days; its fee is understated"


# --- rows written before hold_days existed ---
def test_rows_predating_hold_days_do_not_conflict_when_intraday():
    old = fr.make_row(_plan(0))
    del old["hold_days"]  # a row written before the field existed
    new = fr.make_row(_plan(0))
    assert fr._same_content(old, new), (
        "a schema addition must not read as a data change for rows that were "
        "already priced under the implied convention"
    )


def test_rows_predating_hold_days_DO_conflict_when_overnight():
    old = fr.make_row(_plan(0))
    del old["hold_days"]
    new = fr.make_row(_plan(3))
    assert not fr._same_content(old, new), (
        "a stored row now recomputing as an overnight hold was priced with the "
        "wrong fee; that must be loud, not waved through as a schema addition"
    )


@pytest.mark.parametrize("ledger", ["c7_reversal_ledger.jsonl", "x1_basis_ledger.jsonl"])
def test_ledgers_on_disk_still_rescore_identically(ledger):
    """Stored intraday rows (C7, X1: D+1 open to D+1 close) rescore to the same values."""
    path = FORWARD_DIR / ledger
    if not path.exists():
        pytest.skip(f"{ledger} not present")
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    if not rows:
        pytest.skip(f"{ledger} is empty")
    for r in rows:
        if r.get("exit_mid") is None:  # v1-migrated row: only the fee is checkable
            ws = r["scores"][fr._scn_name(fr.WORSE_SIDE)]
            assert round(fr._round_trip_fee_points(ws["entry"], 0), 3) == ws["fee_pts"]
            continue
        plan = fr.TradePlan(
            card_id=r["card"],
            date_D=r["date_D"],
            date_D1=r["date_D1"],
            side=r["side"],
            entry_mid=r["entry_mid"],
            exit_mid=r["exit_mid"],
            stop_dist=r.get("stop_dist"),
            d1_high=r.get("d1_high"),
            d1_low=r.get("d1_low"),
            hold_days=int(r.get("hold_days", 0)),
            meta=r.get("signal") or {},
        )
        fresh = fr.make_row(plan)
        for scenario, stored in r["scores"].items():
            got = fresh["scores"].get(scenario)
            if got is None:
                continue
            for key in ("entry", "exit", "gross_pts", "fee_pts", "net_pts"):
                assert float(got[key]) == pytest.approx(
                    float(stored[key]), abs=1e-9
                ), f"{ledger} {r['date_D']} {scenario}: {key} moved under the P0 fix"
        assert fr._same_content(r, fresh)
