"""Tests for the Stage-0 fee hurdle (``historical/backtest_engine/fee_hurdle.py``)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from historical.backtest_engine import fee_hurdle as fh


def test_fee_matches_fillsim_schedule_at_two_price_levels():
    # fill.rs FillConfig::default, qty 1, no slippage: 2 * (3700 + PIT) + 2550 VND with
    # PIT = 0.001 * price * 100000 * 0.17 / 2; in points 0.0995 + 0.00017 * price
    assert abs(fh.round_trip_fee_points(1250.0) - 0.3120) < 1e-4
    assert abs(fh.round_trip_fee_points(1915.0) - 0.42505) < 1e-4
    # the tax scales with price, so the fee does too
    assert fh.round_trip_fee_points(1915.0) > fh.round_trip_fee_points(1250.0)


def test_cost_adds_two_slippage_fills():
    price = 1500.0
    fee = fh.round_trip_fee_points(price)
    cost = fh.round_trip_cost_points(price)
    assert abs((cost - fee) - 2 * fh.slippage_points()) < 1e-9
    assert abs(cost - fee - 0.2) < 1e-9


def test_gross_favorable_excursion_and_hurdle():
    # 1-minute bars; a long entry at 1500 runs to a high of 1503
    bars = pd.DataFrame(
        {
            "ts_open": [0, 60, 120, 180],
            "high": [1500.0, 1503.0, 1501.0, 1500.0],
            "low": [1499.0, 1500.0, 1499.5, 1499.0],
        }
    )
    entries = pd.DataFrame({"entry_ts": [0], "dir": ["long"], "entry_price": [1500.0]})
    fe = fh.gross_favorable_excursion(bars, entries, horizon_min=45)
    assert abs(fe.loc[0, "mfe_pts"] - 3.0) < 1e-9  # 1503 - 1500
    # fee at 1500 is about 0.3545 points, cost about 0.5545; a 3-point move clears both
    assert bool(fe.loc[0, "clears_fee"])
    assert bool(fe.loc[0, "clears_cost"])
    summ = fh.fee_hurdle(fe)
    assert summ["n_signals"] == 1
    assert summ["frac_clearing_fee"] == 1.0
    assert summ["verdict"] == "PASS_STAGE0"


def test_sub_fee_idea_is_declared_dead():
    # a 0.2-point MFE never clears the fee of about 0.3545 points
    bars = pd.DataFrame({"ts_open": [0, 60], "high": [1500.2, 1500.2], "low": [1499.5, 1499.8]})
    entries = pd.DataFrame({"entry_ts": [0], "dir": ["long"], "entry_price": [1500.0]})
    fe = fh.gross_favorable_excursion(bars, entries, horizon_min=45)
    assert not bool(fe.loc[0, "clears_fee"])
    assert fh.fee_hurdle(fe)["verdict"] == "DEAD_SUB_FEE"


def test_multi_day_hold_charges_the_position_fee_per_extra_ict_day():
    """Mirrors fill.rs: ``position_fee(qty) * extra_days`` is added at exit."""
    price = 1900.0
    intraday = fh.round_trip_fee_points(price)  # hold_days defaults to 0
    assert abs(intraday - 0.4225) < 1e-9
    for days, expected in ((1, 0.4480), (5, 0.5500), (10, 0.6775)):
        assert abs(fh.round_trip_fee_points(price, hold_days=days) - expected) < 1e-9
        assert (
            abs(fh.round_trip_fee_points(price, hold_days=days) - intraday - 0.0255 * days) < 1e-9
        )
    # negative days clamp to zero, as Rust's extra_days.max(0)
    assert fh.round_trip_fee_points(price, hold_days=-3) == intraday


def test_cost_scenarios_are_named_not_magic_numbers():
    """Statutory, realistic and worse-side costs add 0, 0.2 and 1.0 points of slippage."""
    price = 1900.0
    fee = fh.round_trip_fee_points(price)
    statutory = fh.round_trip_cost_points(price, scenario=fh.CostScenario.STATUTORY)
    realistic = fh.round_trip_cost_points(price, scenario=fh.CostScenario.REALISTIC)
    worse = fh.round_trip_cost_points(price, scenario=fh.CostScenario.WORSE_SIDE)
    assert abs(statutory - fee) < 1e-12
    assert abs(realistic - fee - 0.2) < 1e-12
    assert abs(worse - fee - 1.0) < 1e-12
    # the default scenario is realistic
    assert fh.round_trip_cost_points(price) == realistic
    # hold_days flows through the scenario cost
    assert (
        abs(
            fh.round_trip_cost_points(price, hold_days=10, scenario=fh.CostScenario.WORSE_SIDE)
            - (0.4225 + 0.255 + 1.0)
        )
        < 1e-12
    )


def test_fee_hurdle_declares_no_constant_of_its_own():
    """fee_hurdle re-exports the constants of costs.py."""
    from historical.backtest_engine import costs

    assert fh.TICK_SIZE == costs.TICK == 0.1
    assert fh.MULTIPLIER == costs.MULTIPLIER
    assert fh.POSITION_FEE_VND_PER_DAY == costs.POSITION_FEE_VND_PER_DAY
