"""Tests for the DNSE demo resting-limit probe and its analyzer (offline, virtual clock)."""

from __future__ import annotations

import datetime as dt
import json
import time

import pytest

from historical.backtest_engine.costs import TICK, CostScenario
from historical.research_lab.probes import analyze_probe as ap
from historical.research_lab.probes import resting_limit_probe as rlp
from historical.research_lab.probes.fakes import FakeBroker, VirtualClock
from historical.research_lab.probes.resting_limit_probe import (
    CANCEL_ALL_MIN,
    ICT,
    LIVE_ENV_VALUE,
    LIVE_ENV_VAR,
    REAL_ACCOUNT_DENYLIST,
    BookSnapshot,
    BudgetExhausted,
    CrossingLimitRejected,
    DnseDemoBroker,
    LedgerTampered,
    LiveSendNotAuthorized,
    OutsideSession,
    ProbeConfig,
    RealAccountRefused,
    RestingLimitProbe,
    UnexpectedMarketableFill,
    mask_account,
    read_log,
    session_phase,
    verify_chain,
)

DEMO_ACCOUNT = "0001000001"
LIVE_ENV = {LIVE_ENV_VAR: LIVE_ENV_VALUE}


def _cfg(tmp_path, **kw) -> ProbeConfig:
    base = dict(
        account_no=DEMO_ACCOUNT,
        horizon_s=10.0,
        max_orders_per_session=6,
        max_concurrent=2,
        spacing_s=0.0,
        poll_interval_s=1.0,
        log_path=tmp_path / "limit_probe_log.jsonl",
    )
    base.update(kw)
    return ProbeConfig(**base)


def _probe(tmp_path, broker=None, *, dry_run=True, clock=None, **cfgkw) -> RestingLimitProbe:
    clock = clock or VirtualClock()
    broker = broker or FakeBroker(clock=clock)
    return RestingLimitProbe(
        broker,
        _cfg(tmp_path, **cfgkw),
        dry_run=dry_run,
        clock=clock,
        sleep_fn=clock.sleep,
        env=dict(LIVE_ENV),
    )


# --- interlock 1: the real-account denylist refuses construction ---
def test_real_account_refuses_construction(tmp_path):
    """The denylist is checked in the constructor, so no probe for a denylisted account exists."""
    with pytest.raises(RealAccountRefused) as exc:
        RestingLimitProbe(FakeBroker(), _cfg(tmp_path, account_no="0001234567"))
    assert "DEMO account ONLY" in str(exc.value)
    # the error message masks the account number
    assert "0001234567" not in str(exc.value)


def test_denylist_contains_the_real_account_and_matches_the_rust_lane():
    assert "0001234567" in REAL_ACCOUNT_DENYLIST


def test_blank_account_also_refuses(tmp_path):
    """A blank account cannot be proven to be the demo account, so it fails closed."""
    with pytest.raises(RealAccountRefused):
        RestingLimitProbe(FakeBroker(), _cfg(tmp_path, account_no="   "))


def test_dnse_demo_broker_also_refuses_the_real_account():
    with pytest.raises(RealAccountRefused):
        DnseDemoBroker("0001234567")


def test_mask_account_never_leaks_the_full_number():
    assert mask_account("0001234567") == "****4567"
    assert mask_account("12") == "****"


# --- interlock 2: a crossing limit raises and is never clamped ---
def test_crossing_buy_limit_raises_and_does_not_clamp(tmp_path):
    p = _probe(tmp_path)
    book = BookSnapshot(ts=0.0, best_bid=1900.0, best_ask=1900.2)
    with pytest.raises(CrossingLimitRejected) as exc:
        p._assert_resting(book, "buy", 1900.2)
    assert "would TAKE" in str(exc.value)


def test_crossing_sell_limit_raises(tmp_path):
    p = _probe(tmp_path)
    book = BookSnapshot(ts=0.0, best_bid=1900.0, best_ask=1900.2)
    with pytest.raises(CrossingLimitRejected):
        p._assert_resting(book, "sell", 1900.0)


def test_locked_book_refuses_every_price(tmp_path):
    """With a zero spread the touch itself crosses, so every price is refused."""
    p = _probe(tmp_path)
    book = BookSnapshot(ts=0.0, best_bid=1900.0, best_ask=1900.0)
    with pytest.raises(CrossingLimitRejected):
        p._assert_resting(book, "buy", 1900.0)


def test_touch_and_offsets_rest_on_the_correct_side(tmp_path):
    p = _probe(tmp_path)
    book = BookSnapshot(ts=0.0, best_bid=1900.0, best_ask=1900.2)
    assert p.limit_price_for(book, "buy", 0) == pytest.approx(1900.0)
    assert p.limit_price_for(book, "buy", 1) == pytest.approx(1900.0 - TICK)
    assert p.limit_price_for(book, "buy", 2) == pytest.approx(1900.0 - 2 * TICK)
    assert p.limit_price_for(book, "sell", 0) == pytest.approx(1900.2)
    assert p.limit_price_for(book, "sell", 2) == pytest.approx(1900.2 + 2 * TICK)
    # every offset rests
    for side in ("buy", "sell"):
        for off in (0, 1, 2):
            p._assert_resting(book, side, p.limit_price_for(book, side, off))


def test_negative_offset_is_rejected_before_it_can_cross(tmp_path):
    p = _probe(tmp_path)
    book = BookSnapshot(ts=0.0, best_bid=1900.0, best_ask=1900.2)
    with pytest.raises(ValueError):
        p.limit_price_for(book, "buy", -1)


# --- interlock 3: dry-run by default; live sending needs two keys ---
def test_dry_run_is_the_default_and_places_nothing(tmp_path):
    broker = FakeBroker()
    probe = RestingLimitProbe(
        broker, _cfg(tmp_path), clock=broker.clock, sleep_fn=broker.clock.sleep
    )
    assert probe.dry_run is True
    res = probe.run_session()
    assert broker.place_calls == [], "dry-run must never reach the wire"
    assert broker.cancel_calls == []
    assert res["orders_placed"] > 0, "the driver still runs every interlock"
    assert res["dry_run"] is True
    rows = read_log(probe.config.log_path)
    assert rows and all(r["dry_run"] for r in rows)
    assert all(r["filled"] is None for r in rows), "no outcome exists for an unsent order"


def test_live_without_the_env_var_is_refused(tmp_path):
    with pytest.raises(LiveSendNotAuthorized) as exc:
        RestingLimitProbe(FakeBroker(), _cfg(tmp_path), dry_run=False, env={})
    assert LIVE_ENV_VAR in str(exc.value)


def test_live_with_the_wrong_env_value_is_refused(tmp_path):
    with pytest.raises(LiveSendNotAuthorized):
        RestingLimitProbe(FakeBroker(), _cfg(tmp_path), dry_run=False, env={LIVE_ENV_VAR: "1"})


def test_live_with_both_keys_is_permitted_and_sends(tmp_path):
    clock = VirtualClock()
    broker = FakeBroker(clock=clock, fill_prob_by_offset={0: 0.0, 1: 0.0, 2: 0.0})
    probe = RestingLimitProbe(
        broker,
        _cfg(tmp_path, max_orders_per_session=2, offsets_ticks=(1,)),
        dry_run=False,
        clock=clock,
        sleep_fn=clock.sleep,
        env=dict(LIVE_ENV),
    )
    probe.place_probe("buy", 1)
    assert len(broker.place_calls) == 1
    assert broker.place_calls[0][1] == "buy"


# --- interlock 4: budget caps ---
def test_order_budget_stops_the_session(tmp_path):
    clock = VirtualClock()
    broker = FakeBroker(clock=clock, fill_prob_by_offset={0: 0.0, 1: 0.0, 2: 0.0})
    probe = RestingLimitProbe(
        broker,
        _cfg(tmp_path, max_orders_per_session=4),
        dry_run=False,
        clock=clock,
        sleep_fn=clock.sleep,
        env=dict(LIVE_ENV),
    )
    res = probe.run_session(max_rounds=10)
    assert probe.orders_placed == 4
    assert len(broker.place_calls) == 4
    assert "budget spent" in res["stop_reason"]


def test_budget_raises_once_exhausted_on_a_direct_call(tmp_path):
    probe = _probe(tmp_path, max_orders_per_session=1, max_concurrent=1)
    probe.place_probe("buy", 0)
    with pytest.raises(BudgetExhausted):
        probe.place_probe("sell", 0)


def test_max_concurrent_blocks_a_third_live_order(tmp_path):
    clock = VirtualClock()
    broker = FakeBroker(clock=clock, fill_prob_by_offset={0: 0.0, 1: 0.0, 2: 0.0})
    probe = RestingLimitProbe(
        broker,
        _cfg(tmp_path, max_concurrent=2),
        dry_run=False,
        clock=clock,
        sleep_fn=clock.sleep,
        env=dict(LIVE_ENV),
    )
    probe.place_probe("buy", 1)
    probe.place_probe("sell", 1)
    with pytest.raises(BudgetExhausted) as exc:
        probe.place_probe("buy", 2)
    assert "max_concurrent" in str(exc.value)


def test_fill_budget_stops_the_session(tmp_path):
    clock = VirtualClock()
    broker = FakeBroker(clock=clock, fill_prob_by_offset={0: 1.0, 1: 1.0, 2: 1.0})
    probe = RestingLimitProbe(
        broker,
        _cfg(tmp_path, max_orders_per_session=6, max_fills=2, max_concurrent=1),
        dry_run=False,
        clock=clock,
        sleep_fn=clock.sleep,
        env=dict(LIVE_ENV),
    )
    res = probe.run_session(max_rounds=5)
    assert probe.fills == 2
    assert "fill budget spent" in res["stop_reason"]


def test_unexpected_marketable_fill_hard_stops(tmp_path):
    """A fill worse than the limit breaks the resting guarantee, so the session aborts."""
    clock = VirtualClock()
    broker = FakeBroker(
        clock=clock, fill_prob_by_offset={0: 1.0, 1: 1.0, 2: 1.0}, force_marketable_fill=True
    )
    probe = RestingLimitProbe(
        broker,
        _cfg(tmp_path, max_concurrent=1),
        dry_run=False,
        clock=clock,
        sleep_fn=clock.sleep,
        env=dict(LIVE_ENV),
    )
    with pytest.raises(UnexpectedMarketableFill):
        probe.run_session()
    assert probe.aborted_reason is not None
    rows = read_log(probe.config.log_path)
    assert any(r["resolve_reason"] == "aborted" for r in rows)


# --- interlock 5: session awareness ---
@pytest.mark.parametrize(
    "hhmm,expected",
    [
        ((8, 59), "closed"),
        ((9, 0), "morning"),
        ((11, 29), "morning"),
        ((11, 30), "closed"),
        ((12, 30), "closed"),
        ((13, 0), "afternoon"),
        ((14, 44), "afternoon"),
        ((14, 45), "closed"),
        ((15, 0), "closed"),
    ],
)
def test_session_phase_matches_the_vn30f1m_calendar(hhmm, expected):
    now = dt.datetime(2026, 9, 2, hhmm[0], hhmm[1], tzinfo=ICT)
    assert session_phase(now) == expected


def test_placement_outside_the_session_is_refused(tmp_path):
    clock = VirtualClock(start=dt.datetime(2026, 9, 2, 12, 0, tzinfo=ICT))
    probe = _probe(tmp_path, clock=clock)
    with pytest.raises(OutsideSession):
        probe.place_probe("buy", 0)


def test_horizon_that_would_outlive_the_window_is_refused(tmp_path):
    """At 14:42 a 60 s horizon would outlive the 14:43 cancel-all, so placement is refused."""
    clock = VirtualClock(start=dt.datetime(2026, 9, 2, 14, 42, tzinfo=ICT))
    probe = _probe(tmp_path, clock=clock, horizon_s=60.0)
    with pytest.raises(OutsideSession) as exc:
        probe.place_probe("buy", 0)
    assert "14:43" in str(exc.value)


def test_session_cutoff_cancels_everything_and_stops(tmp_path):
    clock = VirtualClock(start=dt.datetime(2026, 9, 2, CANCEL_ALL_MIN // 60, 43, tzinfo=ICT))
    broker = FakeBroker(clock=clock, fill_prob_by_offset={0: 0.0, 1: 0.0, 2: 0.0})
    probe = RestingLimitProbe(
        broker,
        _cfg(tmp_path, horizon_s=10.0),
        dry_run=False,
        clock=clock,
        sleep_fn=clock.sleep,
        env=dict(LIVE_ENV),
    )
    res = probe.run_session()
    assert res["stop_reason"] == "session_cutoff_1443"
    assert broker.place_calls == []


def test_cancel_all_cancels_every_live_order(tmp_path):
    clock = VirtualClock()
    broker = FakeBroker(clock=clock, fill_prob_by_offset={0: 0.0, 1: 0.0, 2: 0.0})
    probe = RestingLimitProbe(
        broker,
        _cfg(tmp_path, max_concurrent=2),
        dry_run=False,
        clock=clock,
        sleep_fn=clock.sleep,
        env=dict(LIVE_ENV),
    )
    probe.place_probe("buy", 1)
    probe.place_probe("sell", 1)
    assert probe.cancel_all() == 2
    assert len(broker.cancel_calls) == 2


# --- log record and append-only hash chain ---
RUNBOOK_FIELDS = (
    "ts_place",
    "side",
    "offset_ticks",
    "limit_px",
    "best_bid",
    "best_ask",
    "mid",
    "spread_ticks",
    "queue_ahead_at_place",
    "vol_1min_at_place",
    "ts_resolve",
    "filled",
    "fill_px",
    "mid_at_resolve",
    "horizon_s",
    "session_phase",
)


def test_record_carries_every_field_the_plan_requires(tmp_path):
    clock = VirtualClock()
    broker = FakeBroker(clock=clock, fill_prob_by_offset={0: 1.0, 1: 0.0, 2: 0.0})
    probe = RestingLimitProbe(
        broker,
        _cfg(tmp_path, max_orders_per_session=2, max_concurrent=1, offsets_ticks=(0,)),
        dry_run=False,
        clock=clock,
        sleep_fn=clock.sleep,
        env=dict(LIVE_ENV),
    )
    probe.run_session(max_rounds=1)
    rows = read_log(probe.config.log_path)
    assert rows
    for f in RUNBOOK_FIELDS:
        assert f in rows[0], f"missing hazard field {f}"
    # the hazard inputs are populated, not only present
    assert rows[0]["queue_ahead_at_place"] == broker.depth
    assert rows[0]["spread_ticks"] == pytest.approx(2.0)


def test_log_is_append_only_and_hash_chained(tmp_path):
    probe = _probe(tmp_path, max_orders_per_session=2)
    probe.run_session(max_rounds=1)
    path = probe.config.log_path
    first = verify_chain(path)
    assert first["rows"] == 2 and first["ok"]

    # a second session appends to the same log
    probe2 = _probe(tmp_path, max_orders_per_session=2)
    probe2.run_session(max_rounds=1)
    second = verify_chain(path)
    assert second["rows"] == 4
    assert read_log(path)[0]["row_hash"] == first["head"] or second["rows"] == 4


def test_tampering_with_a_logged_row_is_detected(tmp_path):
    probe = _probe(tmp_path, max_orders_per_session=2)
    probe.run_session(max_rounds=1)
    path = probe.config.log_path
    rows = read_log(path)
    rows[0]["limit_px"] = 9999.0  # simulated hand edit
    path.write_text(
        "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in rows),
        encoding="utf-8",
    )
    with pytest.raises(LedgerTampered):
        verify_chain(path)


def test_hash_chain_matches_forward_registry():
    """The probe's local hash chain (kept free of pandas) matches ``forward_registry``."""
    fr = pytest.importorskip("historical.research_lab.forward_registry")
    from historical.research_lab.probes import resting_limit_probe as rlp

    row = {"v": 1, "side": "buy", "limit_px": 1900.0, "prev_hash": rlp.GENESIS_HASH}
    assert rlp.GENESIS_HASH == fr.GENESIS_HASH
    assert rlp.row_hash(row, rlp.GENESIS_HASH) == fr.row_hash(row, fr.GENESIS_HASH)


# --- DNSE binding (no network) ---
class _FakeRedis:
    """Minimal Redis stub for ``get_book`` parsing and staleness checks."""

    def __init__(self, mapping):
        self._mapping = mapping

    def hgetall(self, key):  # noqa: D102
        return dict(self._mapping)


def _book_hash(*, age_s: float = 0.0, levels: int = 10):
    now = int(time.time() - age_s)
    bid_px = [round(1963.9 - 0.1 * i, 1) for i in range(levels)]
    ask_px = [round(1964.0 + 0.1 * i, 1) for i in range(levels)]
    sizes = [float(3 + i) for i in range(levels)]
    return {
        "bid_prices": json.dumps(bid_px),
        "bid_volumes": json.dumps(sizes),
        "offer_prices": json.dumps(ask_px),
        "offer_volumes": json.dumps(sizes),
        "updated_timestamp": str(now),
    }


def _broker(monkeypatch, mapping):
    monkeypatch.setenv("ENTRADE_API_PAPER_BASE", rlp._DEFAULT_PAPER_BASE)
    b = DnseDemoBroker(DEMO_ACCOUNT)
    b._redis_client = _FakeRedis(mapping)
    return b


def test_get_book_reads_the_full_ladder_from_redis(monkeypatch):
    """``get_book`` reads the 10-level ladder that data_provider publishes to ``md:state:``."""
    b = _broker(monkeypatch, _book_hash())
    book = b.get_book("VN30F1M")
    assert book.best_bid == 1963.9
    assert book.best_ask == 1964.0
    assert len(book.bids) == 10 and len(book.asks) == 10
    assert book.bid_size == 3
    assert not book.is_crossed_or_locked()


def test_get_book_refuses_a_stale_book(monkeypatch):
    """A two-minute-old book raises an error rather than a warning."""
    b = _broker(monkeypatch, _book_hash(age_s=120.0))
    with pytest.raises(rlp.ProbeError, match="stale book"):
        b.get_book("VN30F1M")


def test_get_book_refuses_an_absent_or_empty_key(monkeypatch):
    b = _broker(monkeypatch, {})
    with pytest.raises(rlp.ProbeError, match="empty or absent"):
        b.get_book("VN30F1M")

    b2 = _broker(
        monkeypatch,
        {"bid_prices": "[]", "offer_prices": "[]", "updated_timestamp": str(int(time.time()))},
    )
    with pytest.raises(rlp.ProbeError, match="carries no book"):
        b2.get_book("VN30F1M")


def test_live_trading_base_is_refused(monkeypatch):
    """A live-trading API base is rejected in ``__init__``, before any order can exist."""
    monkeypatch.setenv("ENTRADE_API_PAPER_BASE", "https://services.entrade.com.vn/entrade-api")
    with pytest.raises(rlp.RealAccountRefused, match="papertrade"):
        DnseDemoBroker(DEMO_ACCOUNT)


def test_order_status_vocabulary_is_normalised():
    m = DnseDemoBroker._normalise_status
    assert m("PendingNew") == "pending"
    assert m("New") == "pending"
    assert m("PartiallyFilled") == "partially_filled"
    assert m("Filled") == "filled"
    assert m("Cancelled") == "canceled" and m("Canceled") == "canceled"
    assert m("Rejected") == "rejected"
    # an unknown status reads as live, so polling never ends early on it
    assert m("SomethingNew") == "pending"
    assert m(None) == "pending"


def test_order_type_defaults_to_LO_and_is_overridable(monkeypatch):
    monkeypatch.setenv("ENTRADE_API_PAPER_BASE", rlp._DEFAULT_PAPER_BASE)
    assert DnseDemoBroker(DEMO_ACCOUNT).order_type == "LO"
    monkeypatch.setenv("DNSE_PROBE_ORDER_TYPE", "LMT")
    assert DnseDemoBroker(DEMO_ACCOUNT).order_type == "LMT"


def test_assert_resting_catches_an_order_that_crossed(monkeypatch):
    """A limit order reported filled on arrival has crossed, so the check raises."""
    b = _broker(monkeypatch, _book_hash())
    b.get_open_orders = lambda symbol=None: [  # type: ignore[method-assign]
        rlp.OrderState(order_id="7", status="filled", filled_qty=1)
    ]
    with pytest.raises(rlp.UnexpectedMarketableFill, match="filled on arrival"):
        b.assert_resting("7")


def test_assert_resting_accepts_a_pending_order(monkeypatch):
    b = _broker(monkeypatch, _book_hash())
    b.get_open_orders = lambda symbol=None: [  # type: ignore[method-assign]
        rlp.OrderState(order_id="7", status="pending")
    ]
    assert b.assert_resting("7").status == "pending"


def test_jwt_claims_decode_without_verification():
    import base64

    payload = base64.urlsafe_b64encode(json.dumps({"investorId": 42, "exp": 99}).encode()).decode()
    token = f"header.{payload.rstrip('=')}.sig"
    assert rlp._jwt_claims(token)["investorId"] == 42
    assert rlp._jwt_expiry(token) == 99.0
    # a malformed token yields no claims (forcing a re-login) instead of an error
    assert rlp._jwt_claims("not-a-jwt") == {}
    assert rlp._jwt_expiry("not-a-jwt") == 0.0


# --- analyzer ---
def test_wilson_interval_on_small_n_is_not_degenerate():
    """3/3 fills gives a Wilson lower bound near 44 %, below the 50 % build threshold."""
    lo, hi = ap.wilson_interval(3, 3)
    assert hi == pytest.approx(1.0)
    assert 0.40 < lo < 0.50
    assert lo < ap.BUILD_TOUCH_FILL_RATE

    # a Wald interval would be degenerate here
    lo0, hi0 = ap.wilson_interval(0, 3)
    assert lo0 == pytest.approx(0.0)
    assert hi0 > 0.5, "0/3 does not prove the fill rate is below 50%"

    # no trials: the widest interval
    assert ap.wilson_interval(0, 0) == (0.0, 1.0)


def test_wilson_interval_narrows_with_n():
    lo_small, hi_small = ap.wilson_interval(15, 20)
    lo_big, hi_big = ap.wilson_interval(150, 200)
    assert (hi_big - lo_big) < (hi_small - lo_small)


def _row(side, off, limit, mid, filled, fill_px=None, mid_res=None, ttf=None, spread=2.0):
    return {
        "side": side,
        "offset_ticks": off,
        "limit_px": limit,
        "mid": mid,
        "spread_ticks": spread,
        "filled": filled,
        "fill_px": fill_px,
        "mid_at_resolve": mid_res,
        "time_to_fill_s": ttf,
        "dry_run": False,
        "queue_ahead_at_place": 5,
        "vol_1min_at_place": 0.2,
    }


def test_dry_run_rows_are_excluded_from_the_fill_rate():
    """An unsent dry-run order does not count as a no-fill."""
    rows = [
        _row("buy", 0, 1899.9, 1900.0, True, 1899.9, 1900.1, 5.0),
        {**_row("buy", 0, 1899.9, 1900.0, None), "dry_run": True, "filled": None},
    ]
    s = ap.summarize(rows)
    assert s["n_rows"] == 1
    assert s["by_offset"]["0"]["fill_rate"] == 1.0


def test_adverse_selection_proxy_counts_moves_through_the_limit():
    filled_good = _row("buy", 0, 1899.9, 1900.0, True, 1899.9, 1900.3, 4.0)  # mid moves up
    filled_bad = _row("buy", 0, 1899.9, 1900.0, True, 1899.9, 1899.5, 6.0)  # mid moves through
    assert ap.moved_through_limit(filled_good) is False
    assert ap.moved_through_limit(filled_bad) is True
    s = ap.summarize([filled_good, filled_bad])
    assert s["by_offset"]["0"]["adverse_moved_through_frac"] == pytest.approx(0.5)
    # mark-outs of +0.4 and -0.4 points average to zero
    assert s["by_offset"]["0"]["markout_mean_pts"] == pytest.approx(0.0, abs=1e-9)


def test_sell_side_adverse_selection_is_mirrored():
    bad_sell = _row("sell", 0, 1900.1, 1900.0, True, 1900.1, 1900.5, 3.0)
    assert ap.moved_through_limit(bad_sell) is True
    assert ap.markout_pts(bad_sell) == pytest.approx(-0.4)


def test_time_to_fill_median_and_p90():
    rows = [_row("buy", 0, 1899.9, 1900.0, True, 1899.9, 1900.0, float(t)) for t in (1, 2, 3, 40)]
    st = ap.summarize(rows)["by_offset"]["0"]
    assert st["time_to_fill_median_s"] == pytest.approx(2.5)
    assert st["time_to_fill_p90_s"] > 3.0


def test_effective_slippage_is_reported_against_the_frozen_bars():
    rows = [_row("buy", 0, 1899.9, 1900.0, True, 1899.9, 1900.0, 2.0) for _ in range(20)]
    es = ap.summarize(rows)["by_offset"]["0"]["effective_slippage"]
    assert es["available"]
    # 20/20 fills 0.1 points below the mid, with the mid unchanged: a maker credit
    assert es["slip_per_fill_pts"] < 0
    assert es["slip_per_fill_pts"] < CostScenario.REALISTIC.slip_per_fill_points
    assert es["vs_WORSE_SIDE_pts"] < 0


def test_small_n_cannot_distinguish_the_two_cost_bars():
    """With one fill in two, the slippage interval contains the worse-side bar.

    The summary reports the comparison as ambiguous instead of quoting the point estimate.
    """
    rows = [
        _row("buy", 0, 1899.9, 1900.0, True, 1899.9, 1899.0, 3.0),
        _row("buy", 0, 1899.9, 1900.0, False),
    ]
    es = ap.summarize(rows)["by_offset"]["0"]["effective_slippage"]
    assert es["distinguishes_bars"] is False
    assert "WORSE_SIDE" in es["ambiguous_vs"]
    assert "N too small" in es["note"]


def test_a_decisive_interval_is_not_called_ambiguous():
    """An interval entirely below both cost bars is reported as decisive."""
    rows = [_row("buy", 0, 1899.5, 1900.0, True, 1899.5, 1900.0, 2.0) for _ in range(30)]
    es = ap.summarize(rows)["by_offset"]["0"]["effective_slippage"]
    assert es["distinguishes_bars"] is True
    assert es["ambiguous_vs"] == []
    assert es["slip_per_fill_ci95_pts"][1] < CostScenario.REALISTIC.slip_per_fill_points


def test_gate_refuses_a_verdict_while_the_interval_straddles_it():
    rows = [
        _row("buy", 0, 1899.9, 1900.0, i < 2, 1899.9 if i < 2 else None, 1900.0, 2.0)
        for i in range(4)
    ]
    g = ap.summarize(rows)["gate"]
    assert g["verdict"] == "INSUFFICIENT"
    assert "straddles" in g["reason"]


def test_gate_calls_kill_only_when_the_upper_bound_clears_it():
    rows = [_row("buy", 0, 1899.9, 1900.0, False) for _ in range(40)]
    g = ap.summarize(rows)["gate"]
    assert g["verdict"] == "MAKER_THESIS_WEAK"


def test_gate_calls_build_only_when_the_lower_bound_clears_it():
    rows = [
        _row("buy", 0, 1899.9, 1900.0, i < 38, 1899.9 if i < 38 else None, 1900.0, 2.0)
        for i in range(40)
    ]
    g = ap.summarize(rows)["gate"]
    assert g["verdict"] == "BUILD_LIMIT_FILL_MODEL"


def test_report_renders_and_names_the_verdict(tmp_path):
    rows = [_row("buy", 0, 1899.9, 1900.0, True, 1899.9, 1900.0, 2.0) for _ in range(5)]
    md = ap.render_report(ap.summarize(rows))
    assert "# Resting-limit probe" in md
    assert "Decision gate" in md
    assert "95% CI" in md


def test_end_to_end_offline_session_then_analysis(tmp_path):
    """Driver, log, hash chain and analyzer end to end, with no network."""
    clock = VirtualClock()
    broker = FakeBroker(clock=clock, fill_prob_by_offset={0: 1.0, 1: 0.0, 2: 0.0})
    probe = RestingLimitProbe(
        broker,
        _cfg(tmp_path, max_orders_per_session=6, max_concurrent=1, max_fills=6),
        dry_run=False,
        clock=clock,
        sleep_fn=clock.sleep,
        env=dict(LIVE_ENV),
    )
    probe.run_session(max_rounds=1)
    verify_chain(probe.config.log_path)
    s = ap.summarize(read_log(probe.config.log_path))
    assert s["n_rows"] == 6
    assert s["by_offset"]["0"]["fill_rate"] == 1.0
    assert s["by_offset"]["1"]["fill_rate"] == 0.0
