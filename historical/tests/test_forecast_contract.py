"""Tests for the ``Forecast`` contract: mandatory horizon, +/-20 cap, scaling and combination."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from shared.models.forecast import (
    DEFAULT_CAP,
    DEFAULT_TARGET_ABS,
    Forecast,
    combine,
    scale_and_cap,
    to_position_target,
)

SEED = 20260902


def _index(n: int = 300, start: str = "2024-01-01") -> pd.DatetimeIndex:
    return pd.date_range(start, periods=n, freq="B")


def _raw(n: int = 300, seed: int = SEED, scale: float = 7.0) -> pd.Series:
    rng = np.random.default_rng(seed)
    return pd.Series(rng.normal(0.0, scale, n), index=_index(n))


def _fc(values: pd.Series, **kw) -> Forecast:
    kw.setdefault("horizon", pd.Timedelta(days=1))
    kw.setdefault("spec_id", "TEST")
    return Forecast(values=values, **kw)


# --- mandatory horizon ---
def test_horizon_is_mandatory_positionally():
    vals = pd.Series(np.zeros(10) + 1.0, index=_index(10))
    with pytest.raises(TypeError):
        Forecast(values=vals, spec_id="NOHORIZON")  # type: ignore[call-arg]


def test_horizon_none_and_zero_rejected():
    vals = pd.Series(np.ones(10), index=_index(10))
    with pytest.raises(ValueError, match="mandatory"):
        _fc(vals, horizon=None)
    with pytest.raises(ValueError, match="strictly positive"):
        _fc(vals, horizon=pd.Timedelta(0))
    with pytest.raises(ValueError, match="strictly positive"):
        _fc(vals, horizon="-1D")


def test_horizon_accepts_strings_and_exposes_calendar_days():
    vals = pd.Series(np.ones(10), index=_index(10))
    fc = _fc(vals, horizon="1D")
    assert fc.horizon == pd.Timedelta(days=1)
    assert fc.horizon_days == pytest.approx(1.0)
    assert fc.hold_days == 1

    intraday = _fc(vals, horizon="30min")
    assert intraday.hold_days == 0  # matches costs.round_trip_fee_points semantics
    assert _fc(vals, horizon="5D").hold_days == 5


# --- cap enforcement ---
def test_values_beyond_cap_are_rejected():
    vals = pd.Series(np.linspace(-25.0, 25.0, 50), index=_index(50))
    with pytest.raises(ValueError, match="breaches the cap"):
        _fc(vals)


def test_capped_constructor_clips_to_contract():
    vals = pd.Series(np.linspace(-25.0, 25.0, 50), index=_index(50))
    fc = Forecast.capped(vals, horizon="1D", spec_id="CLIP")
    assert fc.values.abs().max() == pytest.approx(DEFAULT_CAP)
    assert fc.values.min() == pytest.approx(-DEFAULT_CAP)


def test_exactly_at_cap_is_legal():
    vals = pd.Series([DEFAULT_CAP, -DEFAULT_CAP] * 5, index=_index(10))
    assert _fc(vals).values.abs().max() == pytest.approx(DEFAULT_CAP)


def test_custom_cap_is_honoured():
    vals = pd.Series(np.full(10, 6.0), index=_index(10))
    with pytest.raises(ValueError, match="breaches the cap"):
        _fc(vals, cap=5.0)
    assert _fc(vals, cap=10.0).cap == 10.0
    with pytest.raises(ValueError, match="cap must be finite"):
        _fc(pd.Series(np.zeros(10), index=_index(10)), cap=0.0)


# --- index and value validation ---
def test_non_datetime_index_rejected():
    vals = pd.Series(np.ones(10), index=range(10))
    with pytest.raises(TypeError, match="DatetimeIndex"):
        _fc(vals)


def test_non_monotonic_index_rejected():
    idx = _index(10)
    vals = pd.Series(np.ones(10), index=idx[::-1])
    with pytest.raises(ValueError, match="monotonically increasing"):
        _fc(vals)


def test_duplicate_timestamps_rejected():
    idx = pd.DatetimeIndex(["2024-01-01", "2024-01-01", "2024-01-02"])
    with pytest.raises(ValueError, match="duplicate"):
        _fc(pd.Series([1.0, 2.0, 3.0], index=idx))


def test_all_nan_series_rejected():
    vals = pd.Series(np.full(10, np.nan), index=_index(10))
    with pytest.raises(ValueError, match="all-NaN"):
        _fc(vals)


def test_warmup_nan_prefix_is_allowed():
    arr = np.concatenate([np.full(5, np.nan), np.full(5, 3.0)])
    fc = _fc(pd.Series(arr, index=_index(10)))
    assert len(fc) == 10
    assert int(fc.active().sum()) == 5


def test_empty_and_bad_spec_id_rejected():
    with pytest.raises(ValueError, match="empty"):
        _fc(pd.Series([], index=pd.DatetimeIndex([]), dtype="float64"))
    with pytest.raises(ValueError, match="spec_id"):
        _fc(pd.Series([1.0], index=_index(1)), spec_id="")


def test_forecast_is_frozen():
    fc = _fc(pd.Series(np.ones(10), index=_index(10)))
    with pytest.raises(Exception):
        fc.cap = 30.0  # type: ignore[misc]


# --- scale_and_cap ---
def test_scale_and_cap_hits_the_target_average():
    scaled = scale_and_cap(_raw(2000, scale=7.0), min_periods=50)
    warm = scaled.dropna()
    # the target is a long-run average; the expanding scalar drifts early on
    assert warm.abs().mean() == pytest.approx(DEFAULT_TARGET_ABS, rel=0.15)
    assert warm.abs().max() <= DEFAULT_CAP + 1e-9


def test_scale_and_cap_is_scale_invariant_in_rank():
    raw = _raw(500)
    a = scale_and_cap(raw, min_periods=50)
    b = scale_and_cap(raw * 137.0, min_periods=50)
    # rescaling the input rescales the scalar identically, so the forecast is unchanged
    pd.testing.assert_series_equal(a, b, rtol=1e-12, atol=1e-12)


def test_scale_and_cap_warmup_is_nan_not_zero():
    scaled = scale_and_cap(_raw(100), min_periods=30)
    assert scaled.iloc[:29].isna().all()
    assert np.isfinite(scaled.iloc[29])


def test_scale_and_cap_causal_scalar_differs_from_whole_sample():
    """The expanding scalar differs from a whole-sample scalar, which would leak lookahead."""
    raw = _raw(400)
    causal = scale_and_cap(raw, min_periods=30)
    whole = (raw * (DEFAULT_TARGET_ABS / raw.abs().mean())).clip(-DEFAULT_CAP, DEFAULT_CAP)
    mid = 200
    assert abs(float(causal.iloc[mid]) - float(whole.iloc[mid])) > 1e-6


def test_scale_and_cap_ignore_zeros_for_sparse_signals():
    """A sparse signal pins to the cap unless the scalar ignores its zeros."""
    rng = np.random.default_rng(SEED)
    n = 600
    fires = rng.random(n) < 0.2
    raw = pd.Series(np.where(fires, rng.normal(0, 1, n), 0.0), index=_index(n))

    unconditional = scale_and_cap(raw, min_periods=50).dropna()
    conditional = scale_and_cap(raw, min_periods=50, ignore_zeros=True).dropna()

    fired_uncond = unconditional[unconditional != 0].abs()
    fired_cond = conditional[conditional != 0].abs()
    # unconditional scaling pushes most fires onto the cap; conditional does not
    assert (fired_uncond >= DEFAULT_CAP - 1e-9).mean() > 0.5
    assert (fired_cond >= DEFAULT_CAP - 1e-9).mean() < 0.2
    assert fired_cond.mean() == pytest.approx(DEFAULT_TARGET_ABS, rel=0.25)


def test_scale_and_cap_rolling_mode_and_bad_args():
    roll = scale_and_cap(_raw(400), method="rolling", window=100, min_periods=50)
    assert roll.dropna().abs().max() <= DEFAULT_CAP + 1e-9
    with pytest.raises(ValueError, match="window"):
        scale_and_cap(_raw(100), method="rolling", min_periods=50)
    with pytest.raises(ValueError, match="unknown method"):
        scale_and_cap(_raw(100), method="whole_sample")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="target_abs"):
        scale_and_cap(_raw(100), target_abs=0.0)


def test_scale_and_cap_all_zero_input_yields_nan_not_inf():
    zeros = pd.Series(np.zeros(100), index=_index(100))
    out = scale_and_cap(zeros, min_periods=10, ignore_zeros=True)
    assert not np.isfinite(out.to_numpy()).any()


def test_from_raw_builds_a_compliant_forecast():
    fc = Forecast.from_raw(_raw(500), horizon="1D", spec_id="RAW", min_periods=50)
    assert fc.values.dropna().abs().max() <= DEFAULT_CAP + 1e-9
    assert fc.spec_id == "RAW"
    assert fc.values.name == "RAW"


# --- combine ---
def test_combine_recaps():
    idx = _index(50)
    a = _fc(pd.Series(np.full(50, DEFAULT_CAP), index=idx), spec_id="A")
    b = _fc(pd.Series(np.full(50, DEFAULT_CAP), index=idx), spec_id="B")
    # two capped sleeves times a diversification multiplier exceed the cap until re-capped
    out = combine([a, b], div_multiplier=1.6)
    assert out.values.max() == pytest.approx(DEFAULT_CAP)
    assert out.values.abs().max() <= DEFAULT_CAP + 1e-9
    assert out.spec_id == "combo(A+B)"


def test_combine_is_a_weighted_average():
    idx = _index(50)
    a = _fc(pd.Series(np.full(50, 10.0), index=idx), spec_id="A")
    b = _fc(pd.Series(np.full(50, -2.0), index=idx), spec_id="B")
    assert combine([a, b]).values.iloc[0] == pytest.approx(4.0)
    assert combine([a, b], weights=[0.75, 0.25]).values.iloc[0] == pytest.approx(7.0)
    assert combine([a, b], weights=[3.0, 1.0]).values.iloc[0] == pytest.approx(7.0)  # normalised


def test_combine_redistributes_weight_over_missing_sleeves():
    idx = _index(10)
    a = _fc(pd.Series(np.full(10, 8.0), index=idx), spec_id="A")
    warm = np.full(10, 4.0)
    warm[:5] = np.nan
    b = _fc(pd.Series(warm, index=idx), spec_id="B")
    out = combine([a, b])
    assert out.values.iloc[0] == pytest.approx(8.0)  # B not warm -> A carries it
    assert out.values.iloc[-1] == pytest.approx(6.0)


def test_combine_all_nan_bar_stays_nan():
    idx = _index(6)
    nan_head = np.full(6, 5.0)
    nan_head[0] = np.nan
    a = _fc(pd.Series(nan_head, index=idx), spec_id="A")
    b = _fc(pd.Series(nan_head, index=idx), spec_id="B")
    assert np.isnan(combine([a, b]).values.iloc[0])


def test_combine_rejects_mixed_horizons_and_caps():
    idx = _index(20)
    a = _fc(pd.Series(np.ones(20), index=idx), spec_id="A", horizon="1D")
    b = _fc(pd.Series(np.ones(20), index=idx), spec_id="B", horizon="5D")
    with pytest.raises(ValueError, match="mixed horizons"):
        combine([a, b])
    forced = combine([a, b], horizon="3D")
    assert forced.horizon == pd.Timedelta(days=3)

    c = _fc(pd.Series(np.ones(20), index=idx), spec_id="C", cap=10.0)
    with pytest.raises(ValueError, match="mixed caps"):
        combine([a, c])


def test_combine_propagates_causal_claim_and_validates_weights():
    idx = _index(20)
    a = _fc(pd.Series(np.ones(20), index=idx), spec_id="A", causal=True)
    b = _fc(pd.Series(np.ones(20), index=idx), spec_id="B", causal=False)
    assert combine([a, b]).causal is False
    with pytest.raises(ValueError, match="length"):
        combine([a, b], weights=[1.0])
    with pytest.raises(ValueError, match="non-negative"):
        combine([a, b], weights=[1.0, -1.0])
    with pytest.raises(ValueError, match="at least one"):
        combine([])


# --- to_position_target ---
def test_position_target_is_linear_and_full_at_cap():
    idx = _index(4)
    fc = _fc(pd.Series([DEFAULT_CAP, -DEFAULT_CAP, 0.0, 10.0], index=idx))
    tgt = to_position_target(fc, 8)
    assert list(tgt) == pytest.approx([8.0, -8.0, 0.0, 4.0])
    assert tgt.name == "TEST_target"


def test_position_target_keeps_warmup_nan_distinct_from_flat():
    idx = _index(3)
    fc = _fc(pd.Series([np.nan, 0.0, 20.0], index=idx))
    tgt = to_position_target(fc, 5)
    assert np.isnan(tgt.iloc[0])  # warm-up means unknown, not flat
    assert tgt.iloc[1] == 0.0
    with pytest.raises(ValueError, match="capital_contracts"):
        to_position_target(fc, 0)


# --- metadata ---
def test_avg_abs_conditional_vs_unconditional():
    idx = _index(4)
    fc = _fc(pd.Series([0.0, 0.0, 10.0, -20.0], index=idx))
    assert fc.avg_abs() == pytest.approx(7.5)
    assert fc.avg_abs(conditional=True) == pytest.approx(15.0)


def test_periods_per_year_reflects_a_trading_calendar():
    fc = _fc(pd.Series(np.ones(504), index=_index(504)))
    # business days: about 252 a year, not 365
    assert 240 < fc.periods_per_year() < 270
    assert fc.horizon_in_bars() == pytest.approx(fc.periods_per_year() / 365.25, rel=1e-9)


def test_fingerprint_is_content_addressed():
    idx = _index(50)
    base = pd.Series(np.linspace(-5, 5, 50), index=idx)
    f1 = _fc(base.copy())
    f2 = _fc(base.copy())
    bumped = base.copy()
    bumped.iloc[7] += 1e-6
    f3 = _fc(bumped)
    assert f1.fingerprint()["sha1"] == f2.fingerprint()["sha1"]
    assert f1.fingerprint()["sha1"] != f3.fingerprint()["sha1"]
    assert f1.fingerprint()["horizon"] == "1 days 00:00:00"
