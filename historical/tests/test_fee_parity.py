"""Golden-value parity between the Python cost mirror and the Rust ``FillSim`` (ADR 0008)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from historical.backtest_engine import costs
from historical.backtest_engine import fee_hurdle as fh

TOL = 1e-9

# Golden values transcribed from fill.rs, not computed by the code under test.
# side_fee(price) VND = 3700 + 0.001 * price * 100_000 * 0.17 / 2 = 3700 + 8.5 * price
GOLDEN_SIDE_FEE_VND = {
    1000.0: 12_200.0,
    1250.0: 14_325.0,  # also asserted by fill.rs side_fee_matches_dnse_schedule
    1900.0: 19_850.0,
    1915.0: 19_977.5,
}

# round_trip_fee(price, hold_days) VND = 2*side_fee + 2550*(1 + extra_days)
GOLDEN_ROUND_TRIP_FEE_VND = {
    (1250.0, 0): 31_200.0,
    (1250.0, 1): 33_750.0,
    (1250.0, 5): 43_950.0,
    (1250.0, 10): 56_700.0,
    (1900.0, 0): 42_250.0,
    (1900.0, 1): 44_800.0,
    (1900.0, 5): 55_000.0,
    (1900.0, 10): 67_750.0,
    (1915.0, 0): 42_505.0,
}


# --- fee arithmetic ---


@pytest.mark.parametrize("price,expected_vnd", sorted(GOLDEN_SIDE_FEE_VND.items()))
def test_side_fee_matches_rust_fill_config(price: float, expected_vnd: float) -> None:
    assert costs.side_fee_vnd(price, 1) == pytest.approx(expected_vnd, abs=1e-6)


def test_position_fee_matches_rust() -> None:
    """2,550 VND per contract per ICT day held."""
    assert costs.position_fee_vnd(1) == pytest.approx(2_550.0, abs=1e-9)
    assert costs.position_fee_vnd(3) == pytest.approx(7_650.0, abs=1e-9)


@pytest.mark.parametrize(
    "price,hold_days,expected_vnd",
    [(p, d, v) for (p, d), v in sorted(GOLDEN_ROUND_TRIP_FEE_VND.items())],
)
def test_round_trip_fee_vnd_across_hold_days(
    price: float, hold_days: int, expected_vnd: float
) -> None:
    """Round trip = two side fees + one position fee for day 1 and for each extra ICT day."""
    assert costs.round_trip_fee_vnd(price, 1, hold_days) == pytest.approx(expected_vnd, abs=1e-6)
    assert costs.round_trip_fee_points(price, 1, hold_days) == pytest.approx(
        expected_vnd / 100_000.0, abs=1e-12
    )


@pytest.mark.parametrize("hold_days", [0, 1, 5, 10])
def test_extra_day_costs_exactly_one_position_fee_each(hold_days: int) -> None:
    """Each extra ICT day adds 2,550 VND (0.0255 points) per contract."""
    base = costs.round_trip_fee_points(1900.0, hold_days=0)
    held = costs.round_trip_fee_points(1900.0, hold_days=hold_days)
    assert held - base == pytest.approx(0.0255 * hold_days, abs=1e-12)


def test_hold_days_clamps_at_zero_like_rust_max0() -> None:
    """Negative hold days clamp to zero, as Rust's ``(ict_day(exit) - ict_day(entry)).max(0)``."""
    intraday = costs.round_trip_fee_points(1900.0, hold_days=0)
    assert costs.round_trip_fee_points(1900.0, hold_days=-1) == pytest.approx(intraday)
    assert costs.round_trip_fee_points(1900.0, hold_days=-99) == pytest.approx(intraday)


def test_default_hold_days_is_the_legacy_intraday_behaviour() -> None:
    """The default ``hold_days=0`` charges the position fee once."""
    price = 1500.0
    legacy = (2.0 * (3700.0 + 8.5 * price) + 2550.0) / 100_000.0
    assert costs.round_trip_fee_points(price) == pytest.approx(legacy, abs=1e-12)
    assert fh.round_trip_fee_points(price) == pytest.approx(legacy, abs=1e-12)


def test_fee_is_price_dependent_not_a_scalar() -> None:
    """PIT scales with price: the round-trip fee grows 0.00017 points per index point."""
    lo = costs.round_trip_fee_points(1000.0)
    hi = costs.round_trip_fee_points(1900.0)
    assert hi > lo
    assert hi - lo == pytest.approx(0.00017 * 900.0, abs=1e-12)


def test_qty_scales_every_fee_component() -> None:
    for qty in (1, 2, 5):
        assert costs.round_trip_fee_vnd(1900.0, qty, 3) == pytest.approx(
            qty * costs.round_trip_fee_vnd(1900.0, 1, 3), abs=1e-6
        )


# --- replays of the Rust unit tests ---


def test_replays_rust_multi_day_hold_test_fill_rs_404() -> None:
    """Replay fill.rs ``multi_day_hold_charges_position_fee_per_extra_day``.

    Buy fills 1250.1 on ICT day 0 and sell fills 1249.9 on day 2 (two extra days). ``side_fee``
    is linear in price and the fills straddle the 1250 mid, so the single-price mirror matches.
    """
    rust_entry_fee = costs.side_fee_vnd(1250.1, 1) + 2_550.0
    rust_exit_fee = costs.side_fee_vnd(1249.9, 1) + 2.0 * 2_550.0
    assert rust_entry_fee == pytest.approx(16_875.85, abs=1e-6)
    assert rust_exit_fee == pytest.approx(19_424.15, abs=1e-6)

    rust_round_trip = rust_entry_fee + rust_exit_fee
    assert rust_round_trip == pytest.approx(36_300.0, abs=1e-6)

    mirror = costs.round_trip_fee_vnd(1250.0, qty=1, hold_days=2)
    assert mirror == pytest.approx(rust_round_trip, abs=1e-6)


def test_replays_rust_intraday_round_trip_test() -> None:
    """Replay fill.rs ``round_trip_pnl_net_of_all_fees``: net P&L of a 2.8-point intraday move."""
    entry_fee = costs.side_fee_vnd(1250.1, 1) + 2_550.0
    exit_fee = costs.side_fee_vnd(1252.9, 1)  # intraday: no extra position fee
    assert entry_fee == pytest.approx(16_875.85, abs=1e-6)
    assert exit_fee == pytest.approx(14_349.65, abs=1e-6)

    gross_vnd = 2.8 * 100_000.0
    assert gross_vnd - entry_fee - exit_fee == pytest.approx(248_774.5, abs=1e-3)


def test_tick_and_slippage_bars_match_fill_rs() -> None:
    """Tick 0.1; ``Marketable`` slips one adverse tick; worse-side slips 0.5 points per fill."""
    assert costs.TICK == 0.1
    assert costs.TICK_SIZE == 0.1
    assert costs.SLIPPAGE_TICKS * costs.TICK == pytest.approx(0.1)
    assert costs.CostScenario.STATUTORY.slip_per_fill_points == 0.0
    assert costs.CostScenario.REALISTIC.slip_per_fill_points == pytest.approx(0.1)
    assert costs.CostScenario.WORSE_SIDE.slip_per_fill_points == pytest.approx(0.5)
    assert costs.CostScenario.WORSE_SIDE.round_trip_slippage_points == pytest.approx(1.0)


# --- cost scenario ladder ---


def test_scenario_ladder_is_ordered_and_exact() -> None:
    """Statutory < realistic < worse-side, each charging two fills of slippage."""
    price, fee = 1900.0, 0.4225
    s = costs.round_trip_cost(price, scenario=costs.CostScenario.STATUTORY)
    r = costs.round_trip_cost(price, scenario=costs.CostScenario.REALISTIC)
    w = costs.round_trip_cost(price, scenario=costs.CostScenario.WORSE_SIDE)
    assert s == pytest.approx(fee, abs=1e-12)
    assert r == pytest.approx(fee + 0.2, abs=1e-12)
    assert w == pytest.approx(fee + 1.0, abs=1e-12)
    assert s < r < w


def test_round_trip_cost_carries_hold_days_into_the_fee() -> None:
    """Extra hold days reach the all-in cost, not only the fee."""
    w0 = costs.round_trip_cost(1900.0, hold_days=0, scenario=costs.CostScenario.WORSE_SIDE)
    w10 = costs.round_trip_cost(1900.0, hold_days=10, scenario=costs.CostScenario.WORSE_SIDE)
    assert w10 - w0 == pytest.approx(0.255, abs=1e-12)
    assert w10 == pytest.approx(0.4225 + 0.255 + 1.0, abs=1e-12)


# --- E1 passive-queue fill model (ADR 0023) ---

# Golden values transcribed from fill.rs `mod e1` and `FillModel::PassiveQueue`.
GOLDEN_E1_PASSIVE_INPUTS = {
    "half_spread_pts": 0.12,
    "fill_prob": 0.90,
    "adverse_selection_pts": 0.045,
    "chase_pts": 0.12,
}
# 0.9 * (0.045 - 0.12) + 0.1 * 0.12 = -0.0675 + 0.012
GOLDEN_PASSIVE_QUEUE_E1_SLIP_PTS = -0.0555


def test_passive_queue_inputs_match_rust_mod_e1() -> None:
    assert costs.E1_PASSIVE_HALF_SPREAD_PTS == GOLDEN_E1_PASSIVE_INPUTS["half_spread_pts"]
    assert costs.E1_PASSIVE_FILL_PROB == GOLDEN_E1_PASSIVE_INPUTS["fill_prob"]
    assert (
        costs.E1_PASSIVE_ADVERSE_SELECTION_PTS == GOLDEN_E1_PASSIVE_INPUTS["adverse_selection_pts"]
    )
    assert costs.E1_PASSIVE_CHASE_PTS == GOLDEN_E1_PASSIVE_INPUTS["chase_pts"]


def test_passive_queue_slip_matches_rust_fill_model() -> None:
    """Replay fill.rs ``passive_queue_e1_matches_the_measured_calibration``."""
    assert costs.PASSIVE_QUEUE_E1_SLIP_PTS == pytest.approx(
        GOLDEN_PASSIVE_QUEUE_E1_SLIP_PTS, abs=1e-12
    )
    assert costs.CostScenario.PASSIVE_QUEUE_E1.slip_per_fill_points == pytest.approx(
        GOLDEN_PASSIVE_QUEUE_E1_SLIP_PTS, abs=1e-12
    )
    # a credit, but smaller than the E1 composite estimate of -0.077 points
    assert -0.077 < costs.PASSIVE_QUEUE_E1_SLIP_PTS < 0.0


def test_passive_queue_sits_inside_the_maker_taker_bracket() -> None:
    """Replay fill.rs ``passive_queue_sits_inside_the_maker_taker_bracket``."""
    optimistic = -costs.E1_PASSIVE_HALF_SPREAD_PTS  # uncalibrated Passive bound
    calibrated = costs.PASSIVE_QUEUE_E1_SLIP_PTS
    taker = costs.E1_TAKER_SLIP_PTS
    floor = costs.WORSE_SIDE_HALF_SPREAD_PTS
    assert optimistic < calibrated < taker < floor
    # per-fill edge of the calibrated maker model over the taker default
    assert taker - calibrated == pytest.approx(0.1555, abs=1e-12)


def test_passive_queue_endpoints_collapse_like_rust() -> None:
    """Replay fill.rs ``passive_queue_collapses_to_its_two_endpoints`` and the clamp."""
    # p = 1, no adverse selection: the optimistic maker bound
    assert costs.passive_queue_slip_points(0.5, 1.0, 0.0, 0.5) == pytest.approx(-0.5, abs=1e-12)
    # p = 0, never fills and always chases: the worse-side floor
    assert costs.passive_queue_slip_points(0.5, 0.0, 0.0, 0.5) == pytest.approx(0.5, abs=1e-12)
    # fill_prob clamps to [0, 1], as Rust's `.clamp(0.0, 1.0)`
    assert costs.passive_queue_slip_points(0.5, 1.7, 0.0, 0.5) == pytest.approx(-0.5, abs=1e-12)
    assert costs.passive_queue_slip_points(0.5, -0.4, 0.0, 0.5) == pytest.approx(0.5, abs=1e-12)


def test_passive_queue_scenario_is_a_maker_credit_not_a_looser_bar() -> None:
    """The maker scenario costs less than every cost bar, so it is not used as one."""
    price, fee = 1900.0, 0.4225
    maker = costs.round_trip_cost(price, scenario=costs.CostScenario.PASSIVE_QUEUE_E1)
    assert maker == pytest.approx(fee + 2 * GOLDEN_PASSIVE_QUEUE_E1_SLIP_PTS, abs=1e-12)
    assert maker < costs.round_trip_cost(price, scenario=costs.CostScenario.STATUTORY)
    assert maker < costs.round_trip_cost(price, scenario=costs.CostScenario.REALISTIC)
    assert maker < costs.round_trip_cost(price, scenario=costs.CostScenario.WORSE_SIDE)
    # the fee still exceeds the maker credit
    assert maker > 0.0
    assert "NOT A PROMOTION BAR" in costs.CostScenario.PASSIVE_QUEUE_E1.rationale


# --- fee_hurdle re-exports costs.py ---


def test_fee_hurdle_reexports_are_the_same_numbers() -> None:
    for name in (
        "MULTIPLIER",
        "TXN_FEE_VND_PER_SIDE",
        "POSITION_FEE_VND_PER_DAY",
        "INCOME_TAX_RATE",
        "INITIAL_MARGIN_RATIO",
        "TICK_SIZE",
        "SLIPPAGE_TICKS",
    ):
        assert getattr(fh, name) == getattr(costs, name), f"{name} drifted"


@pytest.mark.parametrize("hold_days", [0, 1, 5, 10])
def test_fee_hurdle_delegates_fee_and_cost(hold_days: int) -> None:
    price = 1900.0
    assert fh.round_trip_fee_points(price, hold_days=hold_days) == pytest.approx(
        costs.round_trip_fee_points(price, hold_days=hold_days), abs=1e-12
    )
    assert fh.round_trip_cost_points(price, hold_days=hold_days) == pytest.approx(
        costs.round_trip_cost(price, hold_days=hold_days), abs=1e-12
    )
    # cost - fee == two realistic slippage fills
    assert fh.round_trip_cost_points(price, hold_days=hold_days) - fh.round_trip_fee_points(
        price, hold_days=hold_days
    ) == pytest.approx(0.2, abs=1e-12)


def test_no_module_redeclares_a_cost_constant() -> None:
    """Only costs.py binds a numeric literal to a tick or fee constant."""
    import re

    root = Path(__file__).resolve().parents[1]
    literal = re.compile(
        r"^\s*(TICK_SIZE|FEE_PT|DEFAULT_FEE_POINTS"
        r"|POSITION_FEE_VND_PER_DAY|TXN_FEE_VND_PER_SIDE)\s*=\s*[0-9]"
    )
    offenders = []
    for path in root.rglob("*.py"):
        if path.name in {"costs.py", "test_fee_parity.py"} or "_legacy" in path.parts:
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if literal.match(line):
                offenders.append(f"{path.relative_to(root)}:{i}: {line.strip()}")
    assert not offenders, "cost constants must come from costs.py:\n" + "\n".join(offenders)


# --- assert_fee_parity ---


def _write_run(tmp_path: Path, rows: list[dict]) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "trades.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    return run_dir


def _rust_run_rows(prices: list[float], hold_days: int = 0) -> list[dict]:
    """Build trades.jsonl rows with the fees ``FillSim::execute`` records."""
    rows = []
    for p in prices:
        rows.append(
            {
                "action": "enter",
                "price": p,
                "fee_vnd": costs.side_fee_vnd(p, 1) + costs.position_fee_vnd(1),
            }
        )
        rows.append(
            {
                "action": "exit",
                "price": p,
                "fee_vnd": costs.side_fee_vnd(p, 1) + costs.position_fee_vnd(1) * max(hold_days, 0),
            }
        )
    return rows


def test_assert_fee_parity_passes_on_a_rust_shaped_run(tmp_path: Path) -> None:
    run_dir = _write_run(tmp_path, _rust_run_rows([1890.0, 1900.0, 1910.0]))
    out = fh.assert_fee_parity(run_dir)
    assert out["drift_pct"] < 1e-6
    assert out["mirror_rt_fee_pts"] == pytest.approx(0.4225, abs=1e-4)
    assert out["mean_exit_price"] == pytest.approx(1900.0)


def test_assert_fee_parity_raises_on_the_old_6x_pit_bug(tmp_path: Path) -> None:
    """A PIT of 0.05 % of full notional (about 6x the statutory amount) fails parity."""
    bad_side = 3_700.0 + 0.0005 * 1900.0 * 100_000.0
    rows = [
        {"action": "enter", "price": 1900.0, "fee_vnd": bad_side + 2_550.0},
        {"action": "exit", "price": 1900.0, "fee_vnd": bad_side},
    ]
    run_dir = _write_run(tmp_path, rows)
    with pytest.raises(AssertionError, match="fee mirror drift"):
        fh.assert_fee_parity(run_dir)


def test_assert_fee_parity_detects_a_missed_multi_day_position_fee(tmp_path: Path) -> None:
    """The extra position fees of a 10-day hold do not reconcile with the intraday mirror."""
    run_dir = _write_run(tmp_path, _rust_run_rows([1900.0], hold_days=10))
    with pytest.raises(AssertionError, match="fee mirror drift"):
        fh.assert_fee_parity(run_dir, tol_pct=2.0)
