"""Corwin-Schultz, Roll and Amihud daily estimators: golden vectors, invariants and mutants."""

from __future__ import annotations

import datetime as dt
import json
import math
import random
import sys
from decimal import Decimal, localcontext
from pathlib import Path

import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from historical.l2.producers import daily_micro as dm  # noqa: E402
from historical.research_lab import calendar_vn as cal  # noqa: E402
from historical.research_lab import feature_boundary as fb  # noqa: E402

FIXTURE = _REPO_ROOT / "historical" / "tests" / "fixtures" / "daily_micro_golden_v1.json"
M1_CSV = _REPO_ROOT / "data_provider" / "VN30F1M_Final.csv"

GOLDEN = json.loads(FIXTURE.read_text(encoding="utf-8"))


def _case(section: str, case_id: str) -> dict:
    for c in GOLDEN[section]:
        if c["id"] == case_id:
            return c
    raise KeyError(f"no golden case {section}/{case_id} in {FIXTURE}")


def _rel_close(got: float, want: float, tol: float = 1e-12) -> bool:
    """Relative comparison, absolute when ``want`` is 0; ``tol`` covers float64 noise only."""
    if want == 0.0:
        return abs(got) <= tol
    return abs(got - want) / abs(want) <= tol


# --- independent reference: 50-digit decimal, written from the papers ---
def _dec_cs(h1, l1, h2, l2) -> dict[str, Decimal]:
    """Corwin & Schultz eqs (7), (10), (18) and (14) in 50-digit decimal."""
    with localcontext() as ctx:
        ctx.prec = 50
        two = Decimal(2)
        denom = Decimal(3) - two * two.sqrt()
        h1, l1, h2, l2 = (Decimal(str(x)) for x in (h1, l1, h2, l2))
        beta = (h1 / l1).ln() ** 2 + (h2 / l2).ln() ** 2
        gamma = (max(h1, h2) / min(l1, l2)).ln() ** 2
        alpha = ((two * beta).sqrt() - beta.sqrt()) / denom - (gamma / denom).sqrt()
        e = alpha.exp()
        return {
            "beta": +beta,
            "gamma": +gamma,
            "alpha": +alpha,
            "spread_frac": +(two * (e - 1) / (1 + e)),
        }


def _dec_roll(deltas, ddof: int = 1) -> dict[str, Decimal | None]:
    """Roll (1984): lag-1 sample covariance of price changes, then 2*sqrt(-cov)."""
    with localcontext() as ctx:
        ctx.prec = 50
        d = [Decimal(str(x)) for x in deltas]
        xs, ys = d[1:], d[:-1]
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (n - ddof)
        spread = None if cov >= 0 else Decimal(2) * (-cov).sqrt()
        return {"cov": +cov, "spread_pts": (None if spread is None else +spread)}


def _dec_amihud(rets, turnovers, scale) -> Decimal:
    """Amihud (2002): mean of |r| / (turnover / scale)."""
    with localcontext() as ctx:
        ctx.prec = 50
        terms = [
            abs(Decimal(str(r))) / (Decimal(str(t)) / Decimal(str(scale)))
            for r, t in zip(rets, turnovers)
        ]
        return +(sum(terms) / len(terms))


# --- golden vectors ---
def _assert_zero_volatility_identity() -> None:
    """Zero-volatility identity: P = 1000 and S = 0.01 give H = 1005 and L = 995 on both days.

    With k = ln(1005/995): beta = 2k^2, gamma = k^2 and, since 3 - 2*sqrt(2) = (sqrt(2) - 1)^2,
    alpha = k and S_hat = 0.01. Shared with the mutation tests, which require it to fail.
    """
    case = _case("cs", "zero_volatility_identity")
    k = math.log(1005.0 / 995.0)
    got = dm.corwin_schultz(**case["input"])
    assert _rel_close(got.beta, 2.0 * k * k), f"beta {got.beta!r} != 2k^2"
    assert _rel_close(got.gamma, k * k), f"gamma {got.gamma!r} != k^2"
    assert _rel_close(got.alpha, k), f"alpha {got.alpha!r} != k = ln(1005/995)"
    assert _rel_close(got.spread_frac, 0.01), f"S_hat {got.spread_frac!r} != the true S = 0.01"
    # the frozen fixture agrees with the same algebra
    assert _rel_close(float(case["expect"]["alpha"]), k)
    assert float(case["expect"]["spread_frac"]) == 0.01


def test_cs_golden_zero_volatility_identity():
    """Named by ``specs/features/ob_spread_cs_pts_v1.yaml::parity.test``."""
    _assert_zero_volatility_identity()


def _assert_cs_general_case() -> None:
    """A volatile pair: every intermediate matches the decimal reference and the fixture."""
    case = _case("cs", "general_two_day")
    got = dm.corwin_schultz(**case["input"])
    ref = _dec_cs(**case["input"])
    for field in ("beta", "gamma", "alpha", "spread_frac"):
        want_fixture = float(case["expect"][field])
        want_decimal = float(ref[field])
        assert _rel_close(want_fixture, want_decimal, 1e-15), f"fixture drifted on {field}"
        assert _rel_close(
            getattr(got, field), want_decimal
        ), f"{field}: float64 {getattr(got, field)!r} vs decimal reference {want_decimal!r}"


def test_cs_golden_general_two_day_matches_decimal_reference():
    _assert_cs_general_case()


def test_cs_golden_negative_alpha_matches_decimal_reference():
    """Two quiet days far apart: ``gamma >> beta`` drives ``alpha < 0`` and ``S < 0``.

    The formula returns the negative estimate; ``negative_rule`` then zeroes, keeps or drops it.
    """
    case = _case("cs", "negative_alpha")
    got = dm.corwin_schultz(**case["input"])
    assert got.alpha < 0.0
    assert got.spread_frac < 0.0
    assert _rel_close(got.spread_frac, float(case["expect"]["spread_frac"]))
    assert dm.apply_negative_rule(got.spread_frac, "zero") == 0.0
    assert dm.apply_negative_rule(got.spread_frac, "keep") == got.spread_frac
    assert dm.apply_negative_rule(got.spread_frac, "drop") is None


def _assert_roll_closed_form() -> None:
    """``deltas = [+2, -2, +2, -2]``: lag-1 cov = (-96/9) / 2 = -16/3, so S = 8/sqrt(3)."""
    case = _case("roll", "alternating_closed_form")
    got = dm.roll_spread_pts(
        [case["input"]["deltas"]],
        ddof=case["input"]["ddof"],
        min_pairs=case["input"]["min_pairs"],
    )
    assert got is not None
    assert _rel_close(got, 8.0 / math.sqrt(3.0)), f"{got!r} != 8/sqrt(3)"
    assert _rel_close(float(case["expect"]["spread_pts"]), 8.0 / math.sqrt(3.0), 1e-15)
    xs = case["input"]["deltas"][1:]
    ys = case["input"]["deltas"][:-1]
    assert _rel_close(dm.paired_covariance(xs, ys, 1), -16.0 / 3.0)


def test_roll_golden_alternating_deltas_closed_form():
    """Named by ``specs/features/ob_spread_roll_pts_v1.yaml::parity.test``."""
    _assert_roll_closed_form()


def test_roll_golden_general_matches_decimal_reference():
    case = _case("roll", "general_mixed")
    got = dm.roll_spread_pts(
        [case["input"]["deltas"]],
        ddof=case["input"]["ddof"],
        min_pairs=case["input"]["min_pairs"],
    )
    ref = _dec_roll(case["input"]["deltas"], case["input"]["ddof"])
    assert got is not None
    assert _rel_close(float(case["expect"]["spread_pts"]), float(ref["spread_pts"]), 1e-15)
    assert _rel_close(got, float(ref["spread_pts"]))


def _assert_amihud_hand_computed(fn=None) -> None:
    """``|0.02|/(2e6/1e6) = 0.01`` and ``|-0.01|/(1e6/1e6) = 0.01``, so the mean is 0.01.

    Takes ``fn`` so the turnover-scale mutant, which monotonicity cannot detect, runs here.
    """
    fn = fn or dm.amihud_illiq
    case = _case("amihud", "hand_computed_window")
    inp = case["input"]
    got = fn(
        list(zip(inp["returns"], inp["turnovers"])),
        scale=inp["scale"],
        min_obs=inp["min_obs"],
    )
    assert got is not None
    assert _rel_close(got, 0.01), f"{got!r} != 0.01"
    assert float(case["expect"]["illiq"]) == 0.01


def test_amihud_golden_hand_computed_window():
    """Named by ``specs/features/ob_illiq_amihud_v1.yaml::parity.test``."""
    _assert_amihud_hand_computed()


def test_amihud_golden_general_matches_decimal_reference():
    case = _case("amihud", "general_window")
    inp = case["input"]
    got = dm.amihud_illiq(
        list(zip(inp["returns"], inp["turnovers"])),
        scale=inp["scale"],
        min_obs=inp["min_obs"],
    )
    ref = float(_dec_amihud(inp["returns"], inp["turnovers"], inp["scale"]))
    assert _rel_close(float(case["expect"]["illiq"]), ref, 1e-15)
    assert got is not None and _rel_close(got, ref)


def test_fixture_provenance_names_where_the_numbers_came_from():
    """Every golden case carries a derivation, with one closed-form anchor per estimator."""
    assert GOLDEN["schema"] == "daily_micro.golden/1"
    assert "decimal" in GOLDEN["provenance"] and "Corwin" in GOLDEN["provenance"]
    for section in ("cs", "roll", "amihud"):
        for case in GOLDEN[section]:
            assert case["derivation"].strip(), f"{section}/{case['id']} has no derivation"
    closed = [c["id"] for s in ("cs", "roll", "amihud") for c in GOLDEN[s] if c["closed_form"]]
    assert len(closed) == 3, f"expected one hand-derived anchor per estimator, got {closed}"


# --- property invariants ---
def test_cs_spread_sign_follows_alpha_sign():
    """``S = 2(e^a - 1)/(1 + e^a)`` is strictly increasing and zero at ``a = 0``."""
    rng = random.Random(20260906)
    assert dm.cs_spread_frac(0.0) == 0.0
    for _ in range(500):
        a = rng.uniform(-2.0, 2.0)
        s = dm.cs_spread_frac(a)
        if a > 0:
            assert s > 0.0, f"alpha={a} gave S={s}"
        elif a < 0:
            assert s < 0.0, f"alpha={a} gave S={s}"
        assert -2.0 < s < 2.0  # the transform is bounded by construction


def test_cs_two_day_range_contains_both_single_days():
    """``gamma`` uses ``max(H)`` and ``min(L)``, so it bounds each day's squared log range."""
    rng = random.Random(7)
    for _ in range(500):
        l1 = rng.uniform(50, 2000)
        h1 = l1 * (1 + rng.uniform(0.0005, 0.05))
        l2 = l1 * (1 + rng.uniform(-0.03, 0.03))
        h2 = l2 * (1 + rng.uniform(0.0005, 0.05))
        est = dm.corwin_schultz(h1, l1, h2, l2)
        assert est.gamma >= math.log(h1 / l1) ** 2 - 1e-15
        assert est.gamma >= math.log(h2 / l2) ** 2 - 1e-15
        assert est.beta > 0.0


def _assert_roll_undefined_when_cov_non_negative(fn) -> None:
    """Roll returns ``None``, not ``0.0``, when ``cov >= 0`` (its model has ``cov = -s^2/4``).

    Takes ``fn`` so ``test_mutation_roll_drops_negativity_guard`` can run a mutant through it.
    """
    case = _case("roll", "positive_covariance_undefined")
    got = fn([case["input"]["deltas"]], ddof=1, min_pairs=case["input"]["min_pairs"])
    assert got is None, f"expected None for cov >= 0, got {got!r}"
    assert got != 0.0
    # a constant series has cov == 0.0
    flat = fn([[1.0] * 12], ddof=1, min_pairs=3)
    assert flat is None, f"expected None for cov == 0, got {flat!r}"


def test_roll_undefined_is_none_not_zero():
    _assert_roll_undefined_when_cov_non_negative(dm.roll_spread_pts)


def test_roll_returns_none_below_min_pairs():
    assert dm.roll_spread_pts([[1.0, -1.0, 1.0]], min_pairs=10) is None
    assert dm.roll_spread_pts([], min_pairs=1) is None
    assert dm.roll_spread_pts([[1.0]], min_pairs=1) is None


def _assert_roll_respects_segment_boundaries(fn) -> None:
    """No lagged pair may straddle a break in the session chain.

    Two segments of six changes give ten pairs; flattening them adds a pair across the break.
    """
    seg_a = [2.0, -2.0, 2.0, -2.0, 2.0, -2.0]
    seg_b = [1.0, -1.0, 1.0, -1.0, 1.0, -1.0]
    split = fn([seg_a, seg_b], ddof=1, min_pairs=10)
    flat = fn([seg_a + seg_b], ddof=1, min_pairs=10)
    assert split is not None and flat is not None
    assert not _rel_close(split, flat, 1e-9), (
        "segmented and flattened inputs gave the same answer, so the segment "
        "boundary is not being honoured"
    )


def test_roll_pairs_never_straddle_a_segment_boundary():
    _assert_roll_respects_segment_boundaries(dm.roll_spread_pts)


def _assert_amihud_monotone_in_volume(fn) -> None:
    """Same ``|r|``, less turnover -> strictly larger ILLIQ. Halving doubles it."""
    base = [(0.01, 2_000_000.0)] * 10
    half = [(0.01, 1_000_000.0)] * 10
    a = fn(base, scale=dm.AMIHUD_TURNOVER_SCALE, min_obs=10)
    b = fn(half, scale=dm.AMIHUD_TURNOVER_SCALE, min_obs=10)
    assert a is not None and b is not None
    assert b > a, f"less volume must be MORE illiquid: {b!r} !> {a!r}"
    assert _rel_close(b, 2.0 * a), f"halving turnover must double ILLIQ: {b!r} vs {2 * a!r}"
    rng = random.Random(11)
    for _ in range(200):
        r = rng.uniform(-0.05, 0.05)
        t = rng.uniform(1e5, 1e7)
        hi = fn([(r, t)] * 10, min_obs=10)
        lo = fn([(r, t * 1.5)] * 10, min_obs=10)
        assert hi is not None and lo is not None
        assert hi >= lo


def test_amihud_monotone_in_volume():
    _assert_amihud_monotone_in_volume(dm.amihud_illiq)


def test_amihud_skips_zero_turnover_rather_than_dividing_by_it():
    """A zero-turnover session is unmeasured, not infinitely illiquid."""
    obs = [(0.01, 1_000_000.0)] * 10 + [(0.01, 0.0)]
    got = dm.amihud_illiq(obs, min_obs=10)
    assert got is not None and math.isfinite(got)
    assert _rel_close(got, dm.amihud_illiq([(0.01, 1_000_000.0)] * 10, min_obs=10))
    assert dm.amihud_illiq([(0.01, 0.0)] * 20, min_obs=10) is None


# --- mutation tests: each mutant must fail a named assertion ---
def test_mutation_cs_denominator_constant(monkeypatch):
    """A 1 % change to ``3 - 2*sqrt(2)`` fails the zero-volatility identity and the general case."""
    monkeypatch.setattr(dm, "CS_DENOM", dm.CS_DENOM * 1.01)
    with pytest.raises(AssertionError):
        _assert_zero_volatility_identity()
    with pytest.raises(AssertionError):
        _assert_cs_general_case()


def test_mutation_cs_beta_uses_two_day_range(monkeypatch):
    """``beta`` from the two-day range (eq. 10) instead of the two one-day ranges (eq. 7).

    Invisible at zero volatility, where the ranges coincide; the general case catches it.
    """

    def _mutant_beta(h1, l1, h2, l2):
        g = math.log(max(h1, h2) / min(l1, l2))
        return 2.0 * g * g

    monkeypatch.setattr(dm, "cs_beta", _mutant_beta)
    with pytest.raises(AssertionError):
        _assert_cs_general_case()


def test_mutation_roll_drops_negativity_guard():
    """Swapping the ``cov < 0`` guard for ``abs`` returns a number where Roll is undefined."""

    def _mutant(segments, *, ddof=1, min_pairs=10):
        xs, ys = [], []
        for seg in segments:
            for i in range(1, len(seg)):
                xs.append(seg[i])
                ys.append(seg[i - 1])
        if len(xs) < max(min_pairs, ddof + 1):
            return None
        return 2.0 * math.sqrt(abs(dm.paired_covariance(xs, ys, ddof)))

    with pytest.raises(AssertionError):
        _assert_roll_undefined_when_cov_non_negative(_mutant)
    # the real implementation still passes
    _assert_roll_undefined_when_cov_non_negative(dm.roll_spread_pts)


def test_mutation_roll_ignores_segment_boundaries():
    """Concatenating the segments creates a pair across a break in the chain."""

    def _mutant(segments, *, ddof=1, min_pairs=10):
        flat = [x for seg in segments for x in seg]
        return dm.roll_spread_pts([flat], ddof=ddof, min_pairs=min_pairs)

    with pytest.raises(AssertionError):
        _assert_roll_respects_segment_boundaries(_mutant)
    _assert_roll_respects_segment_boundaries(dm.roll_spread_pts)


def test_mutation_amihud_turnover_scale():
    """Dropping ``turnover_scale`` rescales ILLIQ by 1e6; only the golden value detects it."""

    def _mutant(observations, *, scale=dm.AMIHUD_TURNOVER_SCALE, min_obs=10):
        return dm.amihud_illiq(observations, scale=1.0, min_obs=min_obs)

    # monotonicity is scale-invariant, so the mutant passes it
    _assert_amihud_monotone_in_volume(_mutant)
    with pytest.raises(AssertionError):
        _assert_amihud_hand_computed(_mutant)
    _assert_amihud_hand_computed(dm.amihud_illiq)


# --- session boundaries, causality and the dev-window gate ---
def _frame(dates, highs, lows, closes, volumes) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        },
        index=pd.to_datetime([str(d) for d in dates]),
    )


def _synthetic_bars(start: str, n: int, *, drop: tuple[str, ...] = ()) -> pd.DataFrame:
    """``n`` trading sessions from ``start``, skipping the dates in ``drop``.

    Closes alternate, so Roll's covariance is negative and every estimator is defined.
    """
    day = dt.date.fromisoformat(start)
    dates: list[dt.date] = []
    while len(dates) < n:
        if cal.is_trading_day(day) and day.isoformat() not in drop:
            dates.append(day)
        day += dt.timedelta(days=1)
    closes, highs, lows, vols = [], [], [], []
    for i, _ in enumerate(dates):
        base = 1000.0 + (2.0 if i % 2 else -2.0)
        closes.append(base)
        highs.append(base + 6.0 + (i % 3))
        lows.append(base - 6.0 - (i % 5))
        vols.append(100_000.0 + 1_000.0 * (i % 7))
    return _frame(dates, highs, lows, closes, vols)


def test_session_links_break_on_a_tape_gap_and_on_the_contract_roll():
    """A session missing from the tape or an expiry breaks the chain; weekends and Tet do not."""
    dates = [dt.date(2020, 7, 15), dt.date(2020, 7, 21), dt.date(2020, 7, 22)]
    links = dm.session_links(dates, [False, False, False])
    assert links == ["", dm.LINK_DATA_GAP, dm.LINK_OK]

    # a weekend or the Tet holiday between two sessions is not a break
    fri, mon = dt.date(2019, 3, 1), dt.date(2019, 3, 4)
    assert dm.session_links([fri, mon], [False, False])[1] == dm.LINK_OK
    pre, post = dt.date(2019, 2, 1), dt.date(2019, 2, 11)  # Tet 2019, 10 nights
    assert cal.gap_kind(pre, post) == "tet"
    assert dm.session_links([pre, post], [False, False])[1] == dm.LINK_OK

    # an expiry on the first day of the pair breaks it; on the second it does not
    a, b = dt.date(2019, 3, 21), dt.date(2019, 3, 22)
    assert dm.session_links([a, b], [True, False])[1] == dm.LINK_ROLL
    assert dm.session_links([a, b], [False, True])[1] == dm.LINK_OK


def _assert_no_value_across_a_broken_link(links_fn) -> None:
    """No CS estimate, return or price change may cross a broken link (here the 2020-07 hole)."""
    bars = _synthetic_bars("2020-07-01", 20, drop=("2020-07-16", "2020-07-17", "2020-07-20"))
    frame = dm.daily_micro_frame(bars)
    broken = frame.index[[i for i, k in enumerate(links_fn(frame)) if k != dm.LINK_OK]]
    assert len(broken) >= 2, "the fixture must contain at least the gap and the first row"
    for ts in broken:
        row = frame.loc[ts]
        assert not math.isfinite(
            float(row["ob_spread_cs_pts_v1"])
        ), f"{ts.date()}: a CS estimate was produced across a broken link"
        assert not math.isfinite(float(row["dp"]))
        assert not math.isfinite(float(row["ret"]))


def test_no_estimate_is_produced_across_a_broken_link():
    _assert_no_value_across_a_broken_link(lambda f: list(f["link"]))


def test_mutation_links_treated_as_always_consecutive(monkeypatch):
    """Ignoring the link mask pairs sessions across the 2020-07 tape hole."""
    real = dm.session_links
    monkeypatch.setattr(
        dm, "session_links", lambda dates, expiry: [""] + [dm.LINK_OK] * (len(dates) - 1)
    )
    with pytest.raises(AssertionError):
        _assert_no_value_across_a_broken_link(
            lambda f: real(
                [ts.date() for ts in pd.DatetimeIndex(f.index)],
                list(f["is_expiry"]),
            )
        )


def test_values_are_causal_and_do_not_repaint():
    """Recomputing on a truncated frame leaves every earlier value unchanged."""
    bars = _synthetic_bars("2019-04-01", 60)
    full = dm.daily_micro_frame(bars)
    cut = 40
    truncated = dm.daily_micro_frame(bars.iloc[:cut])
    for col in dm.FEATURES:
        a = full[col].iloc[:cut].to_numpy(dtype=float)
        b = truncated[col].to_numpy(dtype=float)
        for i, (x, y) in enumerate(zip(a, b)):
            if math.isnan(x) and math.isnan(y):
                continue
            assert x == y, f"{col} row {i} repainted: {x!r} -> {y!r} when later data arrived"


def test_dev_gate_refuses_reserved_sessions():
    """``daily.assert_dev_only_daily`` raises on a frame that reaches into the reserved range."""
    from historical.research_lab.daily import assert_dev_only_daily

    reserved = _frame(
        [dt.date(2024, 6, 3), dt.date(2024, 6, 4)],
        [1010.0, 1011.0],
        [990.0, 991.0],
        [1000.0, 1001.0],
        [1.0, 1.0],
    )
    with pytest.raises(AssertionError, match="HOLDOUT"):
        assert_dev_only_daily(reserved)


@pytest.mark.skipif(not M1_CSV.exists(), reason="M1 CSV not present")
def test_real_dev_window_is_exactly_the_declared_sample():
    """1,649 dev sessions in two blocks; a change to the holdout lock or the CSV fails here."""
    bars = dm.dev_daily_bars()
    assert len(bars) == 1649
    days = [ts.date() for ts in pd.DatetimeIndex(bars.index)]
    assert days[0] == dt.date(2017, 11, 6)
    assert days[-1] == dt.date(2026, 8, 12)
    block_a = [d for d in days if d <= dt.date(2023, 12, 31)]
    block_b = [d for d in days if d >= dt.date(2026, 3, 5)]
    assert (len(block_a), len(block_b)) == (1537, 112)
    assert len(block_a) + len(block_b) == len(days), "a session fell in the reserved range"


@pytest.mark.skipif(not M1_CSV.exists(), reason="M1 CSV not present")
def test_producer_emits_rows_only_where_the_estimate_exists():
    """``produce`` writes defined values only; an undefined estimate produces no row."""
    bars = dm.dev_daily_bars()
    frame = dm.daily_micro_frame(bars)
    for feature in dm.FEATURES:
        rows = dm.produce(feature, bars)
        defined = int(frame[feature].notna().sum())
        assert len(rows) == defined
        assert all(math.isfinite(r.value) for r in rows)
        assert [r.day for r in rows] == sorted(r.day for r in rows)
    # Roll is defined on strictly fewer sessions than Amihud
    assert frame["ob_spread_roll_pts_v1"].notna().sum() < frame["ob_illiq_amihud_v1"].notna().sum()


# --- registry entries ---
FEATURE_NAMES = ("ob_spread_cs_pts_v1", "ob_spread_roll_pts_v1", "ob_illiq_amihud_v1")


@pytest.mark.parametrize("name", FEATURE_NAMES)
def test_registry_entry_is_valid_in_strict_mode(name):
    """No warnings, no missing fields, and the producer's names match the registry's."""
    feat = fb.load_feature(fb.FEATURES_DIR / f"{name}.yaml", strict=True)
    assert feat.feature == name and name in dm.FEATURES
    assert feat.family == "ob"
    assert feat.cadence == "session", "daily cadence; `per_update` and `per_trade` are banned"
    assert feat.signal.causal is True and feat.signal.lookahead_ms == 0
    assert feat.impl.authority == "python" and not feat.impl.rust
    assert feat.impl.python.startswith("historical/l2/producers/daily_micro.py")
    assert feat.parity.tolerance == 0


@pytest.mark.parametrize("name", FEATURE_NAMES)
def test_registry_entry_passes_the_writeable_gate(name):
    """Re-validated at status PARITY, each spec passes the waiver rules of a writeable status."""
    raw = dict(fb.load_feature(fb.FEATURES_DIR / f"{name}.yaml").raw)
    raw["status"] = "PARITY"
    feat = fb.validate_feature(raw, path=fb.FEATURES_DIR / f"{name}.yaml", strict=True)
    assert feat.is_writeable
    assert feat.parity.parity_waiver and len(feat.parity.parity_waiver.strip()) >= 40
    assert "no rust" in feat.parity.parity_waiver.lower()
    assert feat.parity.test.split("::")[0] == "historical/tests/test_l2_daily_micro.py"


@pytest.mark.parametrize("name", FEATURE_NAMES)
def test_producer_reads_its_parameters_from_the_registry(name):
    """Producer parameters come from the spec's ``default`` param set, not Python defaults."""
    declared = fb.load_feature(fb.FEATURES_DIR / f"{name}.yaml").param_sets["default"]
    params = dm.params_from_registry(name)
    assert params.window == declared["window"]["value"]
    assert declared["window"]["unit"] == "session"
    assert declared["gap_policy"] == "drop_data_gap"
    assert declared["roll_boundary"] == "exclude"
    assert declared["levels"] == 1 and declared["stale_ms"] == 0


def test_param_set_ids_are_distinct_across_the_three_features():
    ids = {
        n: fb.load_feature(fb.FEATURES_DIR / f"{n}.yaml").default_param_set_id()
        for n in FEATURE_NAMES
    }
    assert len(set(ids.values())) == 3, ids
