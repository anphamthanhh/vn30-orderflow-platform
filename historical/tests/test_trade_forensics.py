"""Tests for ``trade_forensics``, aimed at silent bugs: sign flips, look-ahead and selection."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from historical.backtest_engine import trade_forensics as tfx
from historical.backtest_engine.costs import CostScenario

T0 = 1_600_000_000  # arbitrary base epoch in seconds; the wall-clock time does not matter


def _bars(rows) -> pd.DataFrame:
    """M1 bars from ``(open, high, low, close)`` rows, one minute apart starting at ``T0``."""
    return pd.DataFrame(
        {
            "ts_open": [T0 + 60 * i for i in range(len(rows))],
            "open": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
        }
    )


def _trip(side="long", entry_px=100.0, exit_px=102.0, i_exit=3, i_entry=0) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "entry_ts": T0 + 60 * i_entry,
                "exit_ts": T0 + 60 * i_exit,
                "side": side,
                "entry_price": entry_px,
                "exit_price": exit_px,
            }
        ]
    )


# Long from 100: bar0 trades both sides of entry, bar1 sets MAE -2.0 (t=1 min),
# bar2 sets MFE +3.0 (t=2 min), bar3 exits at 102.0 (+2.0, giving back 1.0).
FIXTURE = [
    (100.0, 101.0, 99.5, 100.5),
    (100.5, 100.8, 98.0, 98.5),
    (98.5, 103.0, 98.4, 102.5),
    (102.5, 102.8, 101.0, 102.0),
]


# --- conventions ---
def test_ict_day_parity_with_lab():
    """``ict_day`` and ``hold_days_between`` match ``lab``'s copies, which mirror ``FillSim``."""
    from historical.research_lab import lab

    for ts in (T0, T0 + 3600, T0 + 86_400, 1_510_020_000, 1_700_000_123):
        assert tfx.ict_day(ts) == lab.ict_day(ts)
        assert tfx.hold_days_between(ts, ts + 86_400) == lab.hold_days_between(ts, ts + 86_400)


# --- excursions ---
def test_excursions_exact_values_long():
    """Exact values catch swapped MAE/MFE, clamping, close-based extremes and window errors."""
    x = tfx.attach_excursions(_trip(), _bars(FIXTURE)).iloc[0]
    assert x["mfe_pts"] == pytest.approx(3.0)
    assert x["mae_pts"] == pytest.approx(-2.0)
    assert x["mae_abs"] == pytest.approx(2.0)
    assert x["t_mfe_min"] == pytest.approx(2.0)
    assert x["t_mae_min"] == pytest.approx(1.0)
    assert x["realized_gross_pts"] == pytest.approx(2.0)
    assert x["etd_pts"] == pytest.approx(1.0)  # gave back 1.0 of the best 3.0
    assert x["capture"] == pytest.approx(2.0 / 3.0)
    assert x["path_range"] == pytest.approx(5.0)  # 103.0 - 98.0
    assert x["total_efficiency"] == pytest.approx(0.4)
    assert bool(x["mfe_first"]) is False  # the adverse extreme came first
    assert int(x["path_bars"]) == 4


def test_short_is_the_exact_mirror_of_long():
    """A short on the mirrored tape gets the long's excursions; catches the short-side sign bug."""
    long_x = tfx.attach_excursions(_trip(), _bars(FIXTURE)).iloc[0]
    mirrored = [(200.0 - o, 200.0 - l, 200.0 - h, 200.0 - c) for (o, h, l, c) in FIXTURE]
    short_x = tfx.attach_excursions(
        _trip(side="short", entry_px=100.0, exit_px=98.0), _bars(mirrored)
    ).iloc[0]
    for col in ("mfe_pts", "mae_pts", "realized_gross_pts", "etd_pts", "capture", "path_range"):
        assert short_x[col] == pytest.approx(long_x[col]), col


def test_capture_is_bounded_by_one_and_mae_by_zero():
    """Invariants over a random walk; realized above MFE would mean a wrong window or side."""
    rng = np.random.default_rng(7)
    px = 100 + np.cumsum(rng.normal(0, 0.3, 400))
    rows = [(p, p + abs(rng.normal(0, 0.2)), p - abs(rng.normal(0, 0.2)), p) for p in px]
    bars = _bars(rows)
    trips = pd.DataFrame(
        [
            {
                "entry_ts": T0 + 60 * i,
                "exit_ts": T0 + 60 * (i + 20),
                "side": "long" if k % 2 else "short",
                "entry_price": float(bars["close"][i]),
                "exit_price": float(bars["close"][i + 20]),
            }
            for k, i in enumerate(range(0, 300, 13))
        ]
    )
    x = tfx.attach_excursions(trips, bars)
    assert (x["mae_pts"] <= 1e-12).all()  # entry bar's own low is at or below entry
    assert (x["realized_gross_pts"] <= x["mfe_pts"] + 1e-9).all()
    assert (x["capture"].dropna() <= 1.0 + 1e-9).all()
    assert (x["etd_pts"] >= -1e-9).all()


def test_ambiguous_bars_counts_only_two_sided_extensions():
    """The intrabar-order flag fires only on a bar that trades both sides of the entry."""
    x = tfx.attach_excursions(_trip(), _bars(FIXTURE)).iloc[0]
    assert int(x["ambiguous_bars"]) == 1  # bar0 alone traded both sides of entry
    one_sided = _bars([(100.0, 100.0, 99.0, 99.5), (99.5, 99.6, 98.0, 98.2)])
    y = tfx.attach_excursions(_trip(exit_px=98.2, i_exit=1), one_sided).iloc[0]
    assert int(y["ambiguous_bars"]) == 0


def test_mfe_is_not_clamped_at_zero():
    """A long that never trades above its entry has a negative MFE; catches ``max(fav, 0)``."""
    bars = _bars([(99.8, 99.8, 99.0, 99.2), (99.2, 99.5, 98.0, 98.5)])
    x = tfx.attach_excursions(_trip(exit_px=98.5, i_exit=1), bars).iloc[0]
    assert x["mfe_pts"] == pytest.approx(-0.2)  # the best price seen was below entry
    assert np.isnan(x["capture"])  # capture is undefined when nothing favourable happened


def test_reference_price_removes_the_fill_slippage_from_excursions():
    """Excursions are measured from the market price at entry, not from the slipped fill."""
    raw = tfx.attach_excursions(_trip(), _bars(FIXTURE)).iloc[0]
    adj = tfx.attach_excursions(tfx.attach_reference_price(_trip(), 0.5), _bars(FIXTURE)).iloc[0]
    assert adj["mfe_pts"] == pytest.approx(raw["mfe_pts"] + 0.5)
    assert adj["mae_abs"] == pytest.approx(raw["mae_abs"] - 0.5)
    # realized P&L still uses the fill price
    assert adj["realized_gross_pts"] == pytest.approx(raw["realized_gross_pts"])


def test_twin_e_ratio_is_not_handed_the_slippage_by_flipping():
    """Without ``ref_price``, flipping the side turns the 0.5-point slip into a twin bonus."""
    rng = np.random.default_rng(19)
    px = 100 + np.cumsum(rng.normal(0, 0.3, 400))
    bars = _bars([(p, p + 0.4, p - 0.4, p) for p in px])
    trips = pd.DataFrame(
        [
            {
                "entry_ts": T0 + 60 * i,
                "exit_ts": T0 + 60 * (i + 20),
                "side": "long",
                "entry_price": float(bars["close"][i]) + 0.5,  # worse-side fill
                "exit_price": float(bars["close"][i + 20]),
            }
            for i in range(0, 300, 7)
        ]
    )
    naive = tfx.edge_ratio(trips, bars, horizons_min=(30,))
    fixed = tfx.edge_ratio(tfx.attach_reference_price(trips, 0.5), bars, horizons_min=(30,))
    naive_gap = naive["twin_e_ratio"].iloc[0] - naive["e_ratio"].iloc[0]
    fixed_gap = fixed["twin_e_ratio"].iloc[0] - fixed["e_ratio"].iloc[0]
    assert naive_gap > fixed_gap  # the slip inflated the twin
    assert abs(fixed_gap) < abs(naive_gap)


def test_empty_input_returns_typed_empty_frame():
    empty = pd.DataFrame(columns=["entry_ts", "exit_ts", "side", "entry_price", "exit_price"])
    out = tfx.attach_excursions(empty, _bars(FIXTURE))
    assert len(out) == 0
    assert "mfe_pts" in out.columns and "mae_abs" in out.columns


def test_profile_separates_winner_and_loser_heat():
    """Winner and loser MAE are reported separately; a pooled MAE cannot inform a stop."""
    bars = _bars(FIXTURE)
    trips = pd.concat([_trip(), _trip(exit_px=98.0)], ignore_index=True)
    prof = tfx.excursion_profile(tfx.attach_excursions(trips, bars))
    assert prof["winners"]["n"] == 1 and prof["losers"]["n"] == 1
    assert prof["winners"]["mae_abs"]["p50"] == pytest.approx(2.0)
    assert 0.0 <= prof["frac_mfe_over_cost"] <= 1.0


def test_stop_survival_table_accounts_for_both_sides():
    x = tfx.attach_excursions(
        pd.concat([_trip(), _trip(exit_px=98.0)], ignore_index=True), _bars(FIXTURE)
    )
    tbl = tfx.stop_survival_table(x, [0.5, 2.5])
    assert int(tbl.loc[tbl.stop_pts == 0.5, "n_hit"].iloc[0]) == 2  # both took 2.0 of heat
    assert int(tbl.loc[tbl.stop_pts == 2.5, "n_hit"].iloc[0]) == 0  # neither took 2.5
    assert int(tbl.loc[tbl.stop_pts == 0.5, "winners_killed"].iloc[0]) == 1


# --- barrier replay ---
def test_no_barriers_reproduces_the_original_book_exactly():
    """With no barrier the replay is the identity, the null cell of every grid."""
    trips = _trip()
    out = tfx.replay_barriers(trips, _bars(FIXTURE))
    assert (out["exit_kind"] == "original").all()
    assert out["new_exit_price"].iloc[0] == pytest.approx(102.0)
    assert out["gross_pts"].iloc[0] == pytest.approx(2.0)
    assert out["net_pts"].iloc[0] < out["gross_pts"].iloc[0]  # a cost was charged


def test_stop_wins_over_target_inside_the_same_bar():
    """Conservative tie-break: when one bar touches both barriers, the stop fills first."""
    bars = _bars([(100.0, 100.1, 99.9, 100.0), (100.0, 105.0, 95.0, 104.0)])
    trips = _trip(exit_px=104.0, i_exit=1)
    out = tfx.replay_barriers(trips, bars, stop_pts=2.0, target_pts=3.0)
    assert out["exit_kind"].iloc[0] == "stop"
    assert out["new_exit_price"].iloc[0] == pytest.approx(98.0)
    opt = tfx.replay_barriers(
        trips, bars, stop_pts=2.0, target_pts=3.0, cfg=tfx.PathConfig(adverse_first=False)
    )
    assert opt["exit_kind"].iloc[0] in {"stop", "target"}  # no tie-break rule applied


def test_gap_through_a_stop_fills_at_the_open_not_the_level():
    bars = _bars([(100.0, 100.2, 99.8, 100.0), (95.0, 95.5, 94.0, 94.5)])
    out = tfx.replay_barriers(_trip(exit_px=94.5, i_exit=1), bars, stop_pts=2.0)
    assert out["exit_kind"].iloc[0] == "stop"
    assert out["new_exit_price"].iloc[0] == pytest.approx(95.0)  # not 98.0


def test_trailing_stop_ratchets_on_prior_bars_only():
    """The trail uses highs of earlier bars only: 102.0 - 1.0 = 101.0, not 102.2 - 1.0 = 101.2.

    Catches an unshifted ``peak = maximum.accumulate(fav)``.
    """
    bars = _bars(
        [
            (100.0, 100.5, 99.9, 100.4),
            (100.4, 102.0, 100.2, 101.9),
            (101.9, 102.2, 100.9, 101.0),
            (101.0, 101.2, 100.8, 101.0),
        ]
    )
    out = tfx.replay_barriers(_trip(exit_px=101.0), bars, trail_pts=1.0)
    assert out["exit_kind"].iloc[0] == "trail"
    assert out["new_exit_price"].iloc[0] == pytest.approx(101.0)
    assert out["new_exit_price"].iloc[0] != pytest.approx(101.2)


def test_atr_scaled_barriers_are_per_trade_and_refuse_mixed_units():
    """An ATR-multiple stop resolves per trade from ``atr_entry``, not once for the whole book."""
    bars = _bars(FIXTURE)
    trips = tfx.attach_context(_trip(), bars, atr_period=2)
    trips.loc[:, "atr_entry"] = 1.0
    tight = tfx.replay_barriers(trips, bars, stop_atr=1.0)
    trips.loc[:, "atr_entry"] = 100.0
    loose = tfx.replay_barriers(trips, bars, stop_atr=1.0)
    assert tight["exit_kind"].iloc[0] == "stop"
    assert loose["exit_kind"].iloc[0] == "original"

    with pytest.raises(ValueError, match="not both"):
        tfx.replay_barriers(trips, bars, stop_pts=2.0, stop_atr=1.0)
    with pytest.raises(ValueError, match="attach_context"):
        tfx.replay_barriers(_trip(), bars, stop_atr=1.0)


def test_time_stop_exits_at_the_first_bar_at_or_after_the_clock():
    out = tfx.replay_barriers(_trip(), _bars(FIXTURE), time_stop_min=2)
    assert out["exit_kind"].iloc[0] == "time"
    assert out["new_exit_price"].iloc[0] == pytest.approx(102.5)  # close of bar2


# --- families, twins and grid hygiene ---
def _grid_book():
    rng = np.random.default_rng(11)
    px = 100 + np.cumsum(rng.normal(0, 0.4, 600))
    rows = [(p, p + abs(rng.normal(0, 0.3)), p - abs(rng.normal(0, 0.3)), p) for p in px]
    bars = _bars(rows)
    trips = pd.DataFrame(
        [
            {
                "entry_ts": T0 + 60 * i,
                "exit_ts": T0 + 60 * (i + 30),
                "side": "long" if k % 2 else "short",
                "entry_price": float(bars["close"][i]),
                "exit_price": float(bars["close"][i + 30]),
            }
            for k, i in enumerate(range(0, 500, 10))
        ]
    )
    return trips, bars


def test_barrier_grid_preserves_declared_order_and_exposes_no_argmax():
    """Pre-registered Phase-1 hygiene: no sorting and no ``best``/``rank`` column in grid output."""
    trips, bars = _grid_book()
    grid = [
        {"stop_pts": 2.0},
        {"stop_pts": 4.0},
        {"trail_pts": 3.0},
        {"time_stop_min": 60},
    ]
    out = tfx.barrier_grid(trips, bars, grid)
    assert out["stop_pts"].iloc[0] == 2.0 and out["stop_pts"].iloc[1] == 4.0
    assert out["stop_pts"].iloc[2:].isna().all()  # declared order, unsorted
    assert not any(c.lower().startswith("best") or c == "rank" for c in out.columns)
    assert (out["stamp"] == tfx.EXPLORATORY).all()
    fam = tfx.family_stats(out)
    assert fam["n_cells"] == 4
    assert 0.0 <= fam["F_frac_pos"] <= 1.0
    assert "F_mean_edge_over_twin" in fam


def test_twin_is_deterministic_and_actually_flips_sides():
    """Same seed, same twin; a twin equal to the input would make ``edge_over_twin`` zero."""
    trips, _ = _grid_book()
    a = tfx._twin_trips(trips, 12345)
    b = tfx._twin_trips(trips, 12345)
    assert (a["side"] == b["side"]).all()
    flipped = (a["side"].to_numpy() != trips["side"].to_numpy()).mean()
    assert 0.3 < flipped < 0.7
    assert (tfx._twin_trips(trips, 999)["side"] != a["side"]).any()


def test_a_symmetric_tape_gives_a_stop_no_edge_over_its_twin():
    """On a driftless random walk, stop and trail cells stay within 1 point of their twin."""
    trips, bars = _grid_book()
    out = tfx.barrier_grid(trips, bars, [{"stop_pts": 3.0}, {"trail_pts": 3.0}])
    assert (out["edge_over_twin"].abs() < 1.0).all()


# --- entry-side curves ---
def test_zero_delay_matches_the_original_entry():
    trips, bars = _grid_book()
    curve = tfx.entry_delay_curve(trips, bars, delays_min=(0, 5))
    base = tfx.replay_barriers(trips, bars)["net_pts"].mean()
    assert curve.loc[curve.delay_min == 0, "net_pts_per_trade"].iloc[0] == pytest.approx(base)
    assert int(curve.loc[curve.delay_min == 0, "n_dropped"].iloc[0]) == 0


def test_pullback_fill_rate_falls_as_the_demanded_retrace_grows():
    """Deeper pullbacks fill less often; an inverted level comparison would reverse this."""
    trips, bars = _grid_book()
    out = tfx.pullback_entry_curve(trips, bars, pullback_pts=(0.5, 2.0, 8.0), window_min=15)
    assert list(out["fill_rate"]) == sorted(out["fill_rate"], reverse=True)
    assert out["n_filled"].iloc[-1] <= out["n_filled"].iloc[0]


def test_edge_ratio_sees_drift_and_the_twin_does_not():
    """On a rising tape long entries score E > 1 and the coin-flip twin scores lower."""
    rows = [
        (100 + 0.1 * i, 100 + 0.1 * i + 0.3, 100 + 0.1 * i - 0.05, 100 + 0.1 * i)
        for i in range(300)
    ]
    bars = _bars(rows)
    trips = pd.DataFrame(
        [
            {
                "entry_ts": T0 + 60 * i,
                "exit_ts": T0 + 60 * (i + 30),
                "side": "long",
                "entry_price": float(bars["close"][i]),
                "exit_price": float(bars["close"][i + 30]),
            }
            for i in range(0, 200, 5)
        ]
    )
    out = tfx.edge_ratio(trips, bars, horizons_min=(30,))
    assert out["e_ratio"].iloc[0] > 1.0
    assert out["e_ratio"].iloc[0] > out["twin_e_ratio"].iloc[0]


def test_holding_time_curve_reports_cost_alongside_gross():
    trips, bars = _grid_book()
    out = tfx.holding_time_curve(trips, bars, horizons_min=(5, 30))
    assert {"gross_pts_per_trade", "cost_pts_per_trade", "net_pts_per_trade"} <= set(out.columns)
    assert (out["cost_pts_per_trade"] > 0).all()
    assert np.allclose(
        out["net_pts_per_trade"], out["gross_pts_per_trade"] - out["cost_pts_per_trade"]
    )


# --- conditioning ---
def test_context_atr_is_strictly_causal():
    """The ATR attached to a trade excludes the entry bar, so it carries no look-ahead."""
    rows = [(100.0, 100.5, 99.5, 100.0)] * 50 + [(100.0, 130.0, 70.0, 100.0)] * 5
    bars = _bars(rows)
    trips = pd.DataFrame(
        [
            {
                "entry_ts": T0 + 60 * 50,
                "exit_ts": T0 + 60 * 54,
                "side": "long",
                "entry_price": 100.0,
                "exit_price": 100.0,
            }
        ]
    )
    ctx = tfx.attach_context(trips, bars, atr_period=20)
    assert ctx["atr_entry"].iloc[0] < 5.0  # only the calm bars are visible at entry
    first = pd.DataFrame(
        [
            {
                "entry_ts": T0,
                "exit_ts": T0 + 60,
                "side": "long",
                "entry_price": 100.0,
                "exit_price": 100.0,
            }
        ]
    )
    assert np.isnan(tfx.attach_context(first, bars)["atr_entry"].iloc[0])


def test_weekday_is_named_and_correct_against_a_known_ict_date():
    """7 Nov 2017 09:00 ICT is a Tuesday morning bar; catches an off-by-one weekday mapping."""
    ts = int(pd.Timestamp("2017-11-07 09:00:00").value // 10**9) - 7 * 3600
    bars = pd.DataFrame(
        {
            "ts_open": [ts - 60, ts, ts + 60],
            "open": [100.0] * 3,
            "high": [101.0] * 3,
            "low": [99.0] * 3,
            "close": [100.0] * 3,
        }
    )
    trips = pd.DataFrame(
        [
            {
                "entry_ts": ts,
                "exit_ts": ts + 60,
                "side": "long",
                "entry_price": 100.0,
                "exit_price": 100.0,
            }
        ]
    )
    ctx = tfx.attach_context(trips, bars)
    assert ctx["weekday"].iloc[0] == "Tue"
    assert int(ctx["hour"].iloc[0]) == 9
    assert ctx["segment"].iloc[0] == "morning"


def test_conditional_table_reports_both_thresholds_and_sidak_is_wider():
    """Each cell reports a per-cell CI and a wider Sidak-corrected CI for multiple comparisons."""
    trips, bars = _grid_book()
    scored = tfx.replay_barriers(trips, bars)
    ctx = tfx.attach_context(scored, bars)
    tbl = tfx.conditional_table(ctx, "atr_bucket", value="net_pts", n_boot=200)
    assert len(tbl) == 4
    assert tbl.attrs["alpha_sidak"] < tbl.attrs["alpha"]
    assert (tbl["ci_lo_sidak"] <= tbl["ci_lo"] + 1e-9).all()
    assert (tbl["ci_hi_sidak"] >= tbl["ci_hi"] - 1e-9).all()
    assert (tbl["n"] > 0).all() and (tbl["n_blocks"] > 0).all()


def test_contrast_separates_profitable_from_different():
    """A cell can exclude zero and still not differ from the rest; the table reports both."""
    rng = np.random.default_rng(5)
    n = 400
    df = pd.DataFrame(
        {
            "cell": np.repeat([0, 1], n // 2),
            "ict_day": np.arange(n) // 4,
            "net_pts": rng.normal(5.0, 1.0, n),
        }
    )
    t = tfx.conditional_table(df, "cell", value="net_pts", n_boot=300)
    assert t["excludes_zero"].all()  # both cells are profitable
    assert not t["differs_from_rest"].any()  # neither is different

    df.loc[df["cell"] == 1, "net_pts"] -= 10.0  # now cell 1 is worse
    t2 = tfx.conditional_table(df, "cell", value="net_pts", n_boot=300)
    assert t2["differs_from_rest"].all()
    assert t2.loc[t2["cell"] == 1, "vs_rest"].iloc[0] < 0


def test_block_bootstrap_is_wider_than_the_iid_bootstrap_on_clustered_data():
    """With identical values inside each block, the block CI is far wider than the i.i.d. CI."""
    rng = np.random.default_rng(3)
    block_means = rng.normal(0, 1.0, 12)
    values = np.repeat(block_means, 20)
    blocks = np.repeat(np.arange(12), 20)
    lo_b, hi_b = tfx.block_bootstrap_mean_ci(values, blocks, n_boot=500, seed=1)
    lo_i, hi_i = tfx.block_bootstrap_mean_ci(values, np.arange(len(values)), n_boot=500, seed=1)
    assert (hi_b - lo_b) > 2.0 * (hi_i - lo_i)


def test_embedded_slippage_is_not_charged_twice():
    """Scored fill prices already include slippage, so an unchanged exit pays fees only.

    A replaced exit is a raw bar level and pays one slipped fill; ``embedded_slip_fills=0``
    charges slippage on both fills.
    """
    from historical.backtest_engine.costs import round_trip_fee_points

    trips = _trip()
    bars = _bars(FIXTURE)
    unchanged = tfx.replay_barriers(trips, bars, embedded_slip_fills=2)
    assert unchanged["exit_kind"].iloc[0] == "original"
    assert unchanged["cost_pts"].iloc[0] == pytest.approx(round_trip_fee_points(100.0))

    stopped = tfx.replay_barriers(trips, bars, stop_pts=1.0, embedded_slip_fills=2)
    assert stopped["exit_kind"].iloc[0] == "stop"
    one_slip = CostScenario.WORSE_SIDE.slip_per_fill_points
    assert stopped["cost_pts"].iloc[0] == pytest.approx(round_trip_fee_points(100.0) + one_slip)

    raw = tfx.replay_barriers(trips, bars, embedded_slip_fills=0)
    assert raw["cost_pts"].iloc[0] == pytest.approx(round_trip_fee_points(100.0) + 2 * one_slip)


def test_cost_scenario_is_honoured_end_to_end():
    """``WORSE_SIDE`` costs more than ``REALISTIC`` on the same trades: no hard-coded scenario."""
    trips, bars = _grid_book()
    real = tfx.replay_barriers(trips, bars, scenario=CostScenario.REALISTIC)["net_pts"].mean()
    worse = tfx.replay_barriers(trips, bars, scenario=CostScenario.WORSE_SIDE)["net_pts"].mean()
    assert worse < real
