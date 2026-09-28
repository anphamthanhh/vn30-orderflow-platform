"""Tests for the forward-accrual registry: cards, driver, dual-cost scoring and power."""

from __future__ import annotations

import pandas as pd
import pytest

from historical.research_lab import forward_registry as fr

CUTOFF = "2026-08-14"


# --- synthetic daily frames ---
def _daily(open_, high, low, close, start="2026-08-10"):
    d = pd.bdate_range(start, periods=len(close))
    df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": 1})
    df["ret"] = df["close"].diff()
    df["dt"] = d
    return df


def _c7_frame(d1_high=113.5, d1_low=107, d1_close=108, d1_open=113.0):
    # ret = [_, +1, +1, +1, +10, ...]: only index 4 fires (extreme up), so fade short on D+1
    return _daily(
        open_=[100, 100, 101, 102, 103, d1_open],
        high=[101, 102, 103, 104, 114, d1_high],
        low=[99, 99, 100, 101, 103, d1_low],
        close=[100, 101, 102, 103, 113, d1_close],
    )


C7_KW = dict(lookback=3, pct=0.99, atr_period=2, stop_mult=1.0)


# --- cards are data; one driver scores every card in one pass ---
def test_registry_declares_the_three_cards_in_flight():
    assert {c.card_id for c in fr.CARDS} == {"C7", "N1", "X1"}
    c7 = fr.CARDS_BY_ID["C7"]
    assert c7.source == "m1_daily" and c7.plan_fn is fr.c7_plans
    assert c7.ledger_name == "c7_reversal_ledger.jsonl"
    assert c7.cutoff_ict_date == CUTOFF


def test_one_driver_pass_scores_every_card_into_its_own_ledger(tmp_path):
    # 120 sessions cover N1's 99-session warmup; C7 fires on the spikes
    n = 120
    close = [100.0 + (8.0 if i % 7 == 0 else 0.3) * (1 if i % 2 else -1) for i in range(n)]
    daily = _daily(
        open_=[c - 0.2 for c in close],
        high=[c + 1.5 for c in close],
        low=[c - 1.5 for c in close],
        close=close,
        start="2026-01-01",
    )
    out = fr.accrue({"m1_daily": daily, "basis_daily": None}, tmp_path)
    cards = out["cards"]
    # C7 and N1 share one tape pass; X1's source is absent, so it is blocked, not "no signal"
    assert cards["C7"]["status"] in ("ACCRUED", "NO_NEW_SIGNAL")
    assert cards["N1"]["status"] in ("ACCRUED", "NO_NEW_SIGNAL")
    assert cards["X1"]["status"] == "BLOCKED"
    assert "BlockedSource" in cards["X1"]["error"]
    assert (tmp_path / "c7_reversal_ledger.jsonl").exists()


def test_blocked_source_is_not_no_signal():
    with pytest.raises(fr.BlockedSource) as e:
        fr.x1_plans(pd.DataFrame(), CUTOFF)
    assert "BLOCKED" in str(e.value) and "VN30INDEX" in str(e.value)


def test_register_adds_a_card_without_touching_the_driver(tmp_path):
    spec = fr.CardSpec(
        card_id="ZZ_TEST",
        family="test",
        source="m1_daily",
        ledger_name="zz_test_ledger.jsonl",
        plan_fn=lambda daily, cutoff: fr.c7_plans(daily, cutoff, **C7_KW),
        required_warmup=4,
        effect_pts=1.0,
        sigma_pts=5.0,
        signals_per_year=48.2,
    )
    out = fr.accrue({"m1_daily": _c7_frame()}, tmp_path, cards=[spec])
    assert out["cards"]["ZZ_TEST"]["n"] == 1
    assert (tmp_path / "zz_test_ledger.jsonl").exists()


# --- dual-cost scoring: both cost bars stored, neither picked ---
def test_every_row_is_scored_at_both_frozen_bars():
    plan = fr.c7_plans(_c7_frame(), CUTOFF, **C7_KW)[0]
    row = fr.make_row(plan)
    assert set(row["scores"]) == {"WORSE_SIDE", "REALISTIC"}
    ws, rl = row["scores"]["WORSE_SIDE"], row["scores"]["REALISTIC"]
    assert ws["half_spread_pts"] == 0.5 and rl["half_spread_pts"] == 0.1
    # realistic slips 0.4 points less per fill: 0.8 points per round trip
    assert rl["gross_pts"] > ws["gross_pts"]
    assert abs((rl["gross_pts"] - ws["gross_pts"]) - 0.8) < 1e-6


def test_worse_side_scoring_matches_the_frozen_recorder_math():
    # short fade at D+1 open 113.0: worse-side entry 112.5, close 108 -> exit 108.5
    plan = fr.c7_plans(_c7_frame(), CUTOFF, **C7_KW)[0]
    s = fr.score_plan(plan, fr.WORSE_SIDE)
    assert plan.side == -1
    assert s["entry"] == 112.5 and s["exit"] == 108.5
    assert s["exit_reason"] == "close"
    assert abs(s["gross_pts"] - 4.0) < 1e-6
    assert s["net_pts"] < s["gross_pts"]  # fee subtracted


# --- power arithmetic ---
@pytest.mark.parametrize(
    "card_id,n_req,tol",
    # C7: delta +0.1385 / sigma 15.99 points at the promotion bar. N1 is absent: its edge
    # is negative, so N_required is undefined (see the next test).
    [("C7", 82373, 100), ("X1", 184, 2)],
)
def test_n_required_matches_the_preregistered_arithmetic(card_id, n_req, tol):
    """N_required = 6.18 * sigma^2 / delta^2 (80% power, one-sided alpha=0.05)."""
    c = fr.CARDS_BY_ID[card_id]
    assert c.power_is_defined(), (card_id, "delta<=0 -> N_required is UNDEFINED")
    assert abs(c.n_required() - n_req) <= tol, (card_id, c.n_required())


def test_n_required_is_undefined_when_the_promotion_bar_edge_is_negative():
    """A negative edge makes N_required undefined instead of a finite sample size.

    The formula squares delta, so a negative delta would otherwise yield a plausible number.
    """
    n1 = fr.CARDS_BY_ID["N1"]
    assert n1.effect_pts < 0, "A13 measured N1's promotion-bar delta as negative"
    assert not n1.power_is_defined()
    assert n1.n_required() == fr.UNDEFINED_N
    assert n1.years_to_verdict() == float("inf")

    # the naive formula gives a finite, plausible N (about 18,760) that must not be returned
    naive = int(round(fr.POWER_CONST * n1.sigma_pts**2 / n1.effect_pts**2))
    assert 1_000 < naive < 100_000, naive
    assert n1.n_required() != naive

    # the summary says UNDEFINED instead of printing the sentinel value
    s = n1.power_summary()
    assert s["power_defined"] is False
    assert "UNDEFINED" in str(s["n_required_80pct"])
    assert "UNDEFINED" in str(s["years_to_verdict"])
    assert "UNDERPOWERED-OBSERVATIONAL" in str(s["status_if_undefined"])


def test_power_at_the_preregistered_gate_is_about_alpha_for_the_thin_cards():
    # C7 at n=60: delta 0.1385 vs sigma 15.99 points gives power of about 5.7 %, near alpha
    assert 0.05 < fr.CARDS_BY_ID["C7"].power_at(60) < 0.07
    assert fr.CARDS_BY_ID["C7"].power_at(60) < 0.10
    assert fr.CARDS_BY_ID["N1"].power_at(60) < 0.15
    # X1 (N_required 184 vs a gate of 60) is under-powered, but within an order of magnitude
    assert 0.35 < fr.CARDS_BY_ID["X1"].power_at(60) < 0.80
    assert fr.CARDS_BY_ID["X1"].power_at(184) > 0.79  # 80 % power at its own N_required


def test_years_to_verdict_is_surfaced_and_absurd_where_it_should_be():
    c7, n1, x1 = (fr.CARDS_BY_ID[k] for k in ("C7", "N1", "X1"))
    assert c7.years_to_verdict() > 500  # about 1,498 years at 55 signals a year
    assert n1.years_to_verdict() > 40
    assert 3.0 < x1.years_to_verdict() < 5.0  # about 3.8 years
    st = fr.card_status(c7, {"n": 1, "verdict": "ACCRUING"})
    assert st["n_required_80pct"] == c7.n_required()
    assert st["underpowered_at_gate"] is True
    assert "N_required" in fr.format_status(st)
    assert "CANNOT distinguish" in fr.format_status(st)


def test_power_table_lists_every_card():
    t = fr.power_table()
    assert len(t) == len(fr.CARDS)
    assert {"n_required_80pct", "years_to_verdict", "power_at_min_signals"} <= set(t.columns)


# --- N1 warmup ---
def test_n1_required_warmup_is_99_sessions():
    """rv rolling(20) + z rolling(20) + rolling(60).quantile().shift(1) = 99."""
    assert fr.n1_required_warmup(20, 60) == 99
    assert fr.CARDS_BY_ID["N1"].required_warmup == 99


def test_n1_on_a_too_short_tape_fails_loud_instead_of_emitting_zero_rows(tmp_path):
    # 79 sessions, 20 short of the warmup
    short = _daily(
        open_=[100] * 79, high=[101] * 79, low=[99] * 79, close=[100] * 79, start="2026-05-01"
    )
    out = fr.accrue({"m1_daily": short, "basis_daily": None}, tmp_path)
    st = out["cards"]["N1"]
    assert st["status"] == "BLOCKED"
    assert "InsufficientWarmup" in st["error"]
    assert "99" in st["error"] and "79" in st["error"]


# --- format_status ---
def test_format_status_does_not_crash_on_a_card_whose_power_is_undefined():
    """``format_status`` handles the string sentinels ``power_summary`` returns when delta <= 0."""
    n1 = fr.CARDS_BY_ID["N1"]
    st = {"card": "N1", "family": "overnight", "n": 0, "verdict": "BLOCKED", **n1.power_summary()}

    out = fr.format_status(st)  # must not raise

    assert "UNDEFINED" in out
    assert "no sample size answers this question" in out
    # no sample size is invented
    naive = int(round(fr.POWER_CONST * n1.sigma_pts**2 / n1.effect_pts**2))
    assert f"{naive:,}" not in out


def test_format_status_still_prints_the_number_when_power_is_defined():
    """The undefined-power branch keys on ``power_defined`` and leaves other cards unchanged."""
    c7 = fr.CARDS_BY_ID["C7"]
    st = {"card": "C7", "family": "mean-reversion", "n": 0, "verdict": "ACCRUING", **c7.power_summary()}

    out = fr.format_status(st)

    assert f"{c7.n_required():,}" in out
    assert "years at" in out
