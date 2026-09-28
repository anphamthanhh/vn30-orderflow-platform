"""Signal-evaluation metrics, lookahead detector and C7 forecast on seeded synthetic data."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from historical.backtest_engine.costs import (
    CostScenario,
    round_trip_cost,
    round_trip_fee_points,
)
from historical.signal_eval import (
    LookaheadError,
    assert_causal,
    check_causal,
    cost_in_sharpe_units,
    evaluate,
    forward_returns,
    ic_decay,
    information_coefficient,
    information_ratio,
    quantile_returns,
    turnover_per_year,
)
from historical.signals.forecast_c7 import (
    C7_HORIZON,
    c7_forecast,
    c7_forward_returns,
    c7_prices,
    c7_signal_frame,
)
from shared.models.forecast import DEFAULT_CAP, Forecast, scale_and_cap

SEED = 20260902


# --- synthetic tape ---
def make_daily(n: int = 600, seed: int = SEED, vol_shock_tail: int = 0) -> pd.DataFrame:
    """Build a synthetic VN30F1M-like daily OHLC frame on a business-day index.

    ``vol_shock_tail`` raises the volatility of that many final bars, so a whole-sample
    estimator disagrees with its causal twin on earlier bars.
    """
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2019-01-02", periods=n, freq="B")
    sd = np.full(n, 8.0)
    if vol_shock_tail:
        sd[-vol_shock_tail:] = 60.0
    step = rng.normal(0.0, 1.0, n) * sd
    close = 900.0 + np.cumsum(step)
    open_ = close - step * rng.uniform(0.2, 0.8, n)
    span = np.abs(rng.normal(0.0, 1.0, n)) * sd * 0.6
    high = np.maximum(open_, close) + span
    low = np.minimum(open_, close) - span
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=idx)


def _perfect_forecast(prices: pd.Series, lag: int = 1) -> Forecast:
    """Oracle forecast: the future return rescaled inside the cap, used to calibrate the metrics."""
    fwd = forward_returns(prices, lag)
    scale = (DEFAULT_CAP * 0.9) / float(fwd.abs().max())
    return Forecast.capped(fwd * scale, horizon=f"{lag}D", spec_id="ORACLE")


# --- metric calibration ---
def test_perfect_foresight_forecast_has_ic_one():
    px = make_daily(400)["close"]
    fwd = forward_returns(px, 1)
    fc = _perfect_forecast(px)
    assert information_coefficient(fc, fwd) == pytest.approx(1.0, abs=1e-9)
    # negating the forecast flips the sign of the IC
    inverted = Forecast.capped(-fc.values, horizon="1D", spec_id="ANTI")
    assert information_coefficient(inverted, fwd) == pytest.approx(-1.0, abs=1e-9)


def test_random_forecast_has_no_ic():
    px = make_daily(600)["close"]
    rng = np.random.default_rng(SEED + 1)
    noise = pd.Series(rng.normal(0, 5, len(px)), index=px.index)
    fc = Forecast.capped(noise, horizon="1D", spec_id="NOISE")
    assert abs(information_coefficient(fc, forward_returns(px, 1))) < 0.15


def test_ic_is_invariant_to_a_positive_rescale():
    px = make_daily(400)["close"]
    fwd = forward_returns(px, 1)
    raw = pd.Series(np.gradient(px.to_numpy()), index=px.index)
    a = information_coefficient(Forecast.capped(raw * 0.01, "1D", "A"), fwd)
    b = information_coefficient(Forecast.capped(raw * 0.02, "1D", "B"), fwd)
    assert a == pytest.approx(b, abs=1e-12)


def test_ic_degenerate_cases_return_nan():
    px = make_daily(60)["close"]
    flat = Forecast.capped(pd.Series(np.full(len(px), 5.0), index=px.index), "1D", "FLAT")
    assert np.isnan(information_coefficient(flat, forward_returns(px, 1)))


def test_ic_decay_reports_every_lag_and_a_half_life():
    px = make_daily(600)["close"]
    fc = _perfect_forecast(px)  # informative at lag 1, decaying afterwards
    out = ic_decay(fc, px, lags=(1, 5, 21))
    assert set(out["ic"]) == {"1", "5", "21"}
    assert out["ic"]["1"] == pytest.approx(1.0, abs=1e-9)
    assert abs(out["ic"]["21"]) < abs(out["ic"]["1"])
    assert out["half_life_bars"] is not None and out["half_life_bars"] > 0
    with pytest.raises(ValueError, match="lags"):
        ic_decay(fc, px, lags=())


def test_information_ratio_is_finite_for_a_persistent_signal():
    px = make_daily(800)["close"]
    fwd = forward_returns(px, 1)
    rng = np.random.default_rng(SEED + 7)
    # informative but noisy, so the monthly ICs vary and the IR has a denominator
    blend = _perfect_forecast(px).values * 0.5 + rng.normal(0, 6, len(px))
    fc = Forecast.capped(blend, "1D", "NOISY_ORACLE")
    ir = information_ratio(fc, fwd)
    assert np.isfinite(ir) and ir > 0
    assert information_coefficient(fc, fwd) > 0.3


def test_information_ratio_is_nan_when_the_ic_has_no_variance():
    """The oracle's IC is 1 in every period, so the ratio is undefined."""
    px = make_daily(400)["close"]
    ir = information_ratio(_perfect_forecast(px), forward_returns(px, 1))
    assert np.isnan(ir)
    ics = information_ratio(_perfect_forecast(px), forward_returns(px, 1), min_obs=10_000)
    assert np.isnan(ics)  # too few qualifying periods


# --- turnover ---
def test_turnover_of_a_bar_by_bar_flip_and_of_a_constant():
    idx = pd.date_range("2024-01-01", periods=504, freq="B")
    flip = Forecast.capped(
        pd.Series(np.where(np.arange(504) % 2 == 0, DEFAULT_CAP, -DEFAULT_CAP), index=idx),
        "1D",
        "FLIP",
    )
    ppy = flip.periods_per_year()
    # a full-size flip every bar trades two units per bar
    assert turnover_per_year(flip) == pytest.approx(2.0 * ppy, rel=1e-9)

    hold = Forecast.capped(pd.Series(np.full(504, DEFAULT_CAP), index=idx), "1D", "HOLD")
    assert turnover_per_year(hold) == pytest.approx(0.0, abs=1e-12)


def test_turnover_ignores_the_warmup_edge():
    idx = pd.date_range("2024-01-01", periods=100, freq="B")
    arr = np.full(100, 10.0)
    arr[:10] = np.nan
    fc = Forecast.capped(pd.Series(arr, index=idx), "1D", "WARM")
    # the first valid bar does not count as a trade
    assert turnover_per_year(fc) == pytest.approx(0.0, abs=1e-12)


# --- cost in Sharpe units ---
def test_cost_in_sharpe_units_uses_the_cost_authority():
    daily = make_daily(600)
    px = daily["close"]
    idx = px.index
    rng = np.random.default_rng(SEED + 2)
    fc = Forecast.capped(pd.Series(rng.normal(0, 8, len(idx)), index=idx), "1D", "X")

    out = cost_in_sharpe_units(fc, px, scenario=CostScenario.WORSE_SIDE)
    level = float(px.median())
    assert out["price_for_cost"] == pytest.approx(level)
    assert out["hold_days"] == 1
    # both numbers come from costs.py
    assert out["fee_only_round_trip_points"] == pytest.approx(
        round_trip_fee_points(level, qty=1, hold_days=1)
    )
    assert out["cost_round_trip_points"] == pytest.approx(
        round_trip_cost(level, hold_days=1, scenario=CostScenario.WORSE_SIDE, qty=1)
    )
    # worse-side slippage puts the all-in cost above the fee
    assert out["fee_only_round_trip_points"] < out["cost_round_trip_points"]
    assert out["scenario"] == "worse_side"


def test_cost_in_sharpe_units_arithmetic_is_reproducible():
    daily = make_daily(600)
    px = daily["close"]
    rng = np.random.default_rng(SEED + 3)
    fc = Forecast.capped(pd.Series(rng.normal(0, 8, len(px)), index=px.index), "1D", "X")
    out = cost_in_sharpe_units(fc, px)

    assert out["round_trips_per_year"] == pytest.approx(out["turnover_per_year"] / 2.0)
    assert out["annual_cost_points"] == pytest.approx(
        out["round_trips_per_year"] * out["cost_round_trip_points"]
    )
    assert out["sr_cost"] == pytest.approx(out["annual_cost_points"] / out["annual_vol_points"])
    assert out["sr_net"] == pytest.approx(out["sr_gross"] - out["sr_cost"])
    assert out["sr_cost"] > 0


def test_cost_scales_with_turnover():
    daily = make_daily(600)
    px = daily["close"]
    idx = px.index
    churn = Forecast.capped(
        pd.Series(np.where(np.arange(len(idx)) % 2 == 0, 20.0, -20.0), index=idx), "1D", "CHURN"
    )
    slow = Forecast.capped(
        pd.Series(np.where((np.arange(len(idx)) // 50) % 2 == 0, 20.0, -20.0), index=idx),
        "1D",
        "SLOW",
    )
    assert (
        cost_in_sharpe_units(churn, px)["sr_cost"] > 10 * cost_in_sharpe_units(slow, px)["sr_cost"]
    )


def test_cost_accepts_an_explicit_override():
    daily = make_daily(300)
    px = daily["close"]
    fc = Forecast.capped(pd.Series(np.full(len(px), 10.0), index=px.index), "1D", "F")
    out = cost_in_sharpe_units(fc, px, cost_per_round_trip_points=1.234)
    assert out["cost_round_trip_points"] == pytest.approx(1.234)
    assert out["cost_source"] == "caller-supplied override"


def test_longer_horizon_charges_more_position_fee():
    """The forecast horizon sets ``hold_days``, which drives the per-day position fee."""
    daily = make_daily(400)
    px = daily["close"]
    vals = pd.Series(np.full(len(px), 10.0), index=px.index)
    one_day = cost_in_sharpe_units(Forecast.capped(vals, "1D", "A"), px)
    ten_day = cost_in_sharpe_units(Forecast.capped(vals, "10D", "B"), px)
    assert ten_day["hold_days"] == 10
    assert ten_day["fee_only_round_trip_points"] > one_day["fee_only_round_trip_points"]


# --- quantiles ---
def test_quantile_returns_are_monotone_for_a_perfect_forecast():
    px = make_daily(500)["close"]
    fc = _perfect_forecast(px)
    table = quantile_returns(fc, forward_returns(px, 1), n=5)
    assert len(table) == 5
    assert table["mean"].is_monotonic_increasing
    assert int(table["n"].sum()) > 400


def test_quantile_returns_tolerate_a_sparse_forecast():
    px = make_daily(400)["close"]
    idx = px.index
    rng = np.random.default_rng(SEED + 4)
    sparse = np.where(rng.random(len(idx)) < 0.2, rng.choice([-15.0, 15.0], len(idx)), 0.0)
    fc = Forecast.capped(pd.Series(sparse, index=idx), "1D", "SPARSE")
    table = quantile_returns(fc, forward_returns(px, 1), n=5)
    # mass at zero collapses interior edges: fewer buckets, no error
    assert 1 <= len(table) <= 5
    with pytest.raises(ValueError, match="n must be"):
        quantile_returns(fc, forward_returns(px, 1), n=1)


# --- lookahead detector ---
def _causal_gate(bars: pd.DataFrame, lookback: int = 60, pct: float = 0.80) -> Forecast:
    """C7-style fade gate with a trailing quantile of prior bars (causal)."""
    ret = bars["close"].diff()
    absr = ret.abs()
    thresh = absr.rolling(lookback).quantile(pct).shift(1)
    fire = (absr >= thresh).fillna(False)
    side = np.where(fire.to_numpy(), -np.sign(ret.to_numpy()), 0.0)
    return Forecast.capped(
        pd.Series(np.nan_to_num(side) * 10.0, index=bars.index), "1D", "CAUSAL_GATE"
    )


def _leaky_gate(bars: pd.DataFrame, pct: float = 0.80) -> Forecast:
    """The same gate with a whole-sample quantile threshold (lookahead)."""
    ret = bars["close"].diff()
    absr = ret.abs()
    thresh = absr.quantile(pct)  # uses the entire sample, including future bars
    fire = absr >= thresh
    side = np.where(fire.to_numpy(), -np.sign(ret.to_numpy()), 0.0)
    return Forecast.capped(
        pd.Series(np.nan_to_num(side) * 10.0, index=bars.index), "1D", "LEAKY_GATE"
    )


def _leaky_scalar(bars: pd.DataFrame) -> Forecast:
    """Scale by a full-sample mean (lookahead)."""
    raw = -bars["close"].diff().fillna(0.0)
    scaled = raw * (10.0 / raw.abs().mean())
    return Forecast.capped(scaled, "1D", "LEAKY_SCALAR")


def _causal_scalar(bars: pd.DataFrame) -> Forecast:
    raw = -bars["close"].diff().fillna(0.0)
    return Forecast.capped(scale_and_cap(raw, min_periods=30).fillna(0.0), "1D", "CAUSAL_SCALAR")


def test_whole_sample_quantile_fails_assert_causal():
    """A late volatility shock lifts the whole-sample threshold above earlier prefixes' values."""
    bars = make_daily(600, vol_shock_tail=40)
    with pytest.raises(LookaheadError) as excinfo:
        assert_causal(_leaky_gate, bars, n_probes=40)
    msg = str(excinfo.value)
    assert "LOOKAHEAD DETECTED" in msg
    assert "first disagreement at" in msg

    report = check_causal(_leaky_gate, bars, n_probes=40)
    assert report.passed is False
    assert report.first_mismatch is not None
    assert report.first_mismatch.timestamp is not None
    assert report.first_mismatch.delta > 0


def test_causal_twin_of_the_same_gate_passes():
    bars = make_daily(600, vol_shock_tail=40)
    report = assert_causal(_causal_gate, bars, n_probes=40)
    assert report.passed is True
    assert report.n_compared > 0


def test_whole_sample_scalar_fails_and_causal_scalar_passes():
    bars = make_daily(500)
    with pytest.raises(LookaheadError):
        assert_causal(_leaky_scalar, bars, n_probes=8)
    assert assert_causal(_causal_scalar, bars, n_probes=8).passed


def test_shift_minus_one_is_caught():
    def peeking(bars: pd.DataFrame) -> Forecast:
        fwd = bars["close"].shift(-1) - bars["close"]
        return Forecast.capped(np.sign(fwd).fillna(0.0) * 10.0, "1D", "PEEK")

    with pytest.raises(LookaheadError):
        assert_causal(peeking, make_daily(400), n_probes=12)


def test_centred_rolling_window_is_caught():
    def centred(bars: pd.DataFrame) -> Forecast:
        smooth = bars["close"].rolling(21, center=True, min_periods=1).mean()
        return Forecast.capped(np.sign(bars["close"] - smooth).fillna(0.0) * 10.0, "1D", "CENTRED")

    with pytest.raises(LookaheadError):
        assert_causal(centred, make_daily(400), n_probes=12)


def test_causal_report_is_json_serialisable():
    bars = make_daily(400)
    payload = json.dumps(check_causal(_causal_gate, bars, n_probes=6).to_dict(), allow_nan=False)
    assert '"passed": true' in payload


def test_check_causal_guards_against_a_vacuous_pass():
    """A forecast with no finite value raises instead of passing vacuously."""

    def never_warm(bars: pd.DataFrame) -> pd.Series:
        return pd.Series(np.full(len(bars), np.nan), index=bars.index)

    with pytest.raises(ValueError, match="compared nothing"):
        check_causal(never_warm, make_daily(200), n_probes=8)


def test_check_causal_compares_the_whole_prefix_not_only_its_last_point():
    """A lookahead confined to the early sample is still caught.

    With whole-sample max scaling, prefix and full runs agree at every point after the
    global maximum, so only a comparison of the whole prefix sees the leak.
    """
    daily = make_daily(400)
    close = daily["close"].copy()
    close.iloc[40] = float(close.max()) + 60.0  # extremum lands before the back half
    daily = daily.assign(close=close)

    def whole_sample_minmax(bars: pd.DataFrame) -> pd.Series:
        c = bars["close"]
        return (c / c.max()) * 20.0

    def causal_twin(bars: pd.DataFrame) -> pd.Series:
        c = bars["close"]
        return (c / c.expanding().max()) * 20.0

    report = check_causal(whole_sample_minmax, daily)
    assert not report.passed, "whole-sample min-max scaling is lookahead"
    assert report.first_mismatch is not None
    assert check_causal(causal_twin, daily).passed, "the expanding-max twin is causal"


def test_check_causal_catches_a_saturating_whole_sample_statistic():
    """``sign(x - mean(x))`` hides its lookahead at most points but not at all of them."""
    daily = make_daily(400)

    def sign_of_whole_sample_mean(bars: pd.DataFrame) -> pd.Series:
        c = bars["close"]
        return pd.Series(np.sign(c - c.mean()) * 10.0, index=c.index)

    report = check_causal(sign_of_whole_sample_mean, daily)
    assert not report.passed
    assert "rewritten by later data" in (
        report.first_mismatch.note if report.first_mismatch else ""
    )

    with pytest.raises(ValueError, match=">= 6 bars"):
        check_causal(_causal_gate, make_daily(4), n_probes=4)


# --- C7 forecast ---
def test_c7_signal_frame_mirrors_the_frozen_logic():
    daily = make_daily(400)
    sig = c7_signal_frame(daily)
    ret = daily["close"].diff()
    absr = ret.abs()
    thresh = absr.rolling(60).quantile(0.80).shift(1)
    pd.testing.assert_series_equal(sig["ret"], ret, check_names=False)
    pd.testing.assert_series_equal(sig["thresh"], thresh, check_names=False)
    # fade the move, only where the gate fires
    fired = sig[sig["fade_dir"] != 0]
    assert (np.sign(fired["ret"]) == -fired["fade_dir"]).all()
    assert (fired["absr"] >= fired["thresh"]).all()
    assert set(np.unique(sig["fade_dir"])) <= {-1, 0, 1}
    # about 20 % of sessions fire, by construction of the 80th percentile
    fire_rate = float(sig["fire"][sig["thresh"].notna()].mean())
    assert 0.10 < fire_rate < 0.35


def test_c7_forecast_obeys_the_contract_and_is_causal():
    daily = make_daily(600)
    fc = c7_forecast(daily)
    assert fc.horizon == C7_HORIZON == pd.Timedelta(days=1)
    assert fc.hold_days == 1
    assert fc.spec_id == "C7"
    assert fc.values.dropna().abs().max() <= DEFAULT_CAP + 1e-9
    assert int(fc.active().sum()) > 20
    # conviction averages about 10 while in a position
    assert 5.0 < fc.avg_abs(conditional=True) < 20.0

    report = assert_causal(c7_forecast, daily, n_probes=12)
    assert report.passed and report.n_compared > 0


def test_c7_binary_mode_reproduces_the_frozen_cards_own_content():
    daily = make_daily(600)
    fc = c7_forecast(daily, magnitude="binary")
    active = fc.values[fc.active()]
    # direction only: every firing day has the same conviction
    assert active.abs().nunique() == 1
    assert active.abs().iloc[0] == pytest.approx(10.0)
    sig = c7_signal_frame(daily)
    aligned = sig.loc[active.index, "fade_dir"]
    assert (np.sign(active.to_numpy()) == aligned.to_numpy()).all()
    with pytest.raises(ValueError, match="magnitude must be"):
        c7_forecast(daily, magnitude="excess")  # type: ignore[arg-type]


def test_c7_forward_returns_are_the_traded_leg_not_close_to_close():
    daily = make_daily(300)
    fwd = c7_forward_returns(daily)
    o, c = daily["open"], daily["close"]
    expected = (c.shift(-1) - o.shift(-1)).to_numpy()
    np.testing.assert_allclose(fwd.to_numpy(), expected, equal_nan=True)
    # the gap to close-to-close is the overnight leg, which C7 does not hold
    c2c = forward_returns(c7_prices(daily), 1)
    overnight = (c2c - fwd).dropna()
    np.testing.assert_allclose(
        overnight.to_numpy(), (o.shift(-1) - c).dropna().to_numpy(), atol=1e-9
    )
    assert float(overnight.abs().mean()) > 0.0


def test_c7_rejects_a_frame_it_cannot_read():
    with pytest.raises(KeyError, match="missing columns"):
        c7_signal_frame(pd.DataFrame({"close": [1.0, 2.0]}))
    bad = make_daily(80).iloc[::-1]
    with pytest.raises(ValueError, match="sorted ascending"):
        c7_signal_frame(bad)


def test_c7_accepts_a_lab_shaped_frame_with_a_dt_column():
    daily = make_daily(200)
    lab_shaped = daily.reset_index(drop=True)
    lab_shaped["dt"] = daily.index
    a = c7_forecast(daily)
    b = c7_forecast(lab_shaped)
    # check_freq=False: the reset frame loses the inferred index freq (metadata only)
    pd.testing.assert_series_equal(a.values, b.values, check_freq=False)
    assert a.fingerprint()["sha1"] == b.fingerprint()["sha1"]


# --- evaluate() ---
def test_evaluate_returns_strict_json():
    daily = make_daily(700)
    fc = c7_forecast(daily)
    out = evaluate(fc, c7_prices(daily), fwd=c7_forward_returns(daily))
    # allow_nan=False: a bare NaN is not valid JSON
    payload = json.dumps(out, allow_nan=False)
    assert json.loads(payload)["forecast"]["spec_id"] == "C7"

    assert out["forecast"]["horizon"] == "1 days 00:00:00"
    assert out["forecast"]["hold_days"] == 1
    assert out["ic"]["fwd_source"] == "caller-supplied"
    assert out["cost"]["scenario"] == "realistic"
    assert set(out["decay"]["ic"]) == {"1", "5", "21"}
    assert out["turnover"]["turnover_per_year"] > 0
    assert out["cost"]["sr_cost"] > 0


def test_evaluate_fingerprints_its_inputs():
    daily = make_daily(500)
    fc = c7_forecast(daily)
    px = c7_prices(daily)
    a = evaluate(fc, px)
    b = evaluate(fc, px)
    assert a["inputs"] == b["inputs"]

    nudged = px.copy()
    nudged.iloc[123] += 0.1
    c = evaluate(c7_forecast(daily), nudged)
    assert c["inputs"]["prices"]["sha1"] != a["inputs"]["prices"]["sha1"]
    assert a["inputs"]["forecast"]["sha1"] == c["inputs"]["forecast"]["sha1"]


def test_evaluate_sees_the_oracle_and_rejects_a_non_forecast():
    daily = make_daily(500)
    px = daily["close"]
    out = evaluate(_perfect_forecast(px), px)
    assert out["ic"]["ic"] == pytest.approx(1.0, abs=1e-9)
    assert out["ic"]["fwd_source"] == "close-to-close lag 1"
    assert out["quantiles"][0]["mean"] < out["quantiles"][-1]["mean"]
    with pytest.raises(TypeError, match="takes a Forecast"):
        evaluate(px, px)  # type: ignore[arg-type]
