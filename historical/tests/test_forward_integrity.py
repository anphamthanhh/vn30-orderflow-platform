"""Forward-ledger integrity: hash-chained appends, session index and sub-threshold blinding."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from historical.research_lab import forward_registry as fr

CUTOFF = "2026-08-14"


def _row(date_D="2026-08-17", date_D1="2026-08-18", net=1.0, card="C7"):
    return {
        "v": fr.LEDGER_VERSION,
        "card": card,
        "date_D": date_D,
        "date_D1": date_D1,
        "side": -1,
        "entry_mid": 100.0,
        "exit_mid": 99.0,
        "stop_dist": None,
        "d1_high": None,
        "d1_low": None,
        "signal": {"ret_D": 5.0},
        "scores": {
            "WORSE_SIDE": {
                "scenario": "WORSE_SIDE",
                "half_spread_pts": 0.5,
                "entry": 99.5,
                "stop_px": None,
                "exit": 99.5,
                "exit_reason": "close",
                "gross_pts": net,
                "fee_pts": 0.4,
                "slip_pts": 1.0,
                "net_pts": net,
            },
            "REALISTIC": {
                "scenario": "REALISTIC",
                "half_spread_pts": 0.1,
                "entry": 99.9,
                "stop_px": None,
                "exit": 99.1,
                "exit_reason": "close",
                "gross_pts": net + 0.8,
                "fee_pts": 0.4,
                "slip_pts": 0.2,
                "net_pts": net + 0.8,
            },
        },
    }


# --- append-only, tamper-evident ledger ---
def test_append_is_additive_and_idempotent(tmp_path):
    p = tmp_path / "led.jsonl"
    a = fr.append_rows(p, [_row("2026-08-17", "2026-08-18")], tape_sha256="aa")
    assert a == {"appended": 1, "skipped": 0, "total": 1}
    # re-running the same day neither duplicates nor rewrites
    b = fr.append_rows(p, [_row("2026-08-17", "2026-08-18")], tape_sha256="bb")
    assert b["appended"] == 0 and b["skipped"] == 1
    c = fr.append_rows(p, [_row("2026-08-18", "2026-08-19")], tape_sha256="cc")
    assert c["appended"] == 1
    assert len(fr.read_ledger(p)) == 2


def test_ledger_row_carries_recorded_at_tape_digest_and_prev_hash(tmp_path):
    p = tmp_path / "led.jsonl"
    fr.append_rows(p, [_row()], tape_sha256="deadbeef")
    r = fr.read_ledger(p)[0]
    assert r["tape_sha256"] == "deadbeef"
    assert r["prev_hash"] == fr.GENESIS_HASH
    assert len(r["row_hash"]) == 64
    assert r["recorded_at"].endswith("+00:00")


def test_rerun_with_a_DIFFERENT_recomputed_row_raises_never_overwrites(tmp_path):
    """Re-appending a changed past row raises and leaves the stored row intact."""
    p = tmp_path / "led.jsonl"
    fr.append_rows(p, [_row(net=1.0)], tape_sha256="aa")
    with pytest.raises(fr.LedgerConflict) as e:
        fr.append_rows(p, [_row(net=-9.0)], tape_sha256="aa")
    assert "append-only" in str(e.value)
    stored = fr.read_ledger(p)
    assert len(stored) == 1
    assert stored[0]["scores"]["WORSE_SIDE"]["net_pts"] == 1.0  # untouched


def test_verify_chain_detects_an_edited_row(tmp_path):
    p = tmp_path / "led.jsonl"
    fr.append_rows(p, [_row("2026-08-17", "2026-08-18"), _row("2026-08-18", "2026-08-19")])
    assert fr.verify_chain(p)["ok"] is True
    rows = fr.read_ledger(p)
    rows[0]["scores"]["WORSE_SIDE"]["net_pts"] = 99.0  # tamper in place
    p.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")
    with pytest.raises(fr.LedgerTampered) as e:
        fr.verify_chain(p)
    assert "row 0" in str(e.value)


def test_verify_chain_detects_a_deleted_row(tmp_path):
    p = tmp_path / "led.jsonl"
    fr.append_rows(p, [_row("2026-08-17", "2026-08-18"), _row("2026-08-18", "2026-08-19")])
    rows = fr.read_ledger(p)
    p.write_text(json.dumps(rows[1], sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(fr.LedgerTampered):
        fr.verify_chain(p)


def test_appending_onto_a_broken_chain_is_refused(tmp_path):
    p = tmp_path / "led.jsonl"
    fr.append_rows(p, [_row()])
    rows = fr.read_ledger(p)
    rows[0]["row_hash"] = "0" * 64
    p.write_text(json.dumps(rows[0], sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(fr.LedgerTampered):
        fr.append_rows(p, [_row("2026-08-18", "2026-08-19")])


def test_each_line_is_one_write_and_newline_terminated(tmp_path):
    p = tmp_path / "led.jsonl"
    fr.append_rows(p, [_row("2026-08-17", "2026-08-18"), _row("2026-08-18", "2026-08-19")])
    raw = p.read_bytes()
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == 2


# --- session index: missing sessions are visible and block the verdict ---
def test_a_missing_session_shows_up_as_MISSING_DATA(tmp_path):
    # Monday to Friday; the tape lacks the Friday session
    have = [pd.Timestamp(d).date() for d in ("2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20")]
    rows = fr.build_session_index(have, "2026-08-17", "2026-08-21", {"2026-08-18": {"C7"}})
    by = {r["date"]: r for r in rows}
    assert len(rows) == 5  # five weekday sessions
    assert by["2026-08-21"]["status"] == "MISSING_DATA"
    assert by["2026-08-18"]["status"] == "accrued" and by["2026-08-18"]["cards"] == ["C7"]
    assert by["2026-08-17"]["status"] == "no_signal"
    h = fr.session_health(rows)
    assert h["missing_data"] == 1 and h["contiguous"] is False
    assert h["missing_dates"] == ["2026-08-21"]


def test_sessions_file_roundtrips(tmp_path):
    rows = fr.build_session_index(
        [pd.Timestamp("2026-08-17").date()], "2026-08-17", "2026-08-18", {}
    )
    p = fr.write_sessions(tmp_path / "sessions.jsonl", rows)
    assert fr.read_ledger(p) == rows


def test_missing_data_over_budget_blocks_the_verdict():
    ledger = pd.DataFrame(
        [{"date_D1": f"2026-08-{d}", "net_pts": 1.0, "gross_pts": 2.0, "fee_pts": 0.4} for d in range(17, 21)]
    )
    sessions = [
        {"date": "2026-08-17", "status": "accrued"},
        {"date": "2026-08-21", "status": "MISSING_DATA"},
    ]
    res = fr.gate(ledger, fr.CARDS_BY_ID["C7"], sessions=sessions)
    assert res["verdict"] == "BLOCKED_MISSING_DATA"
    assert res["missing_sessions"] == 1 and res["missing_budget"] == 0
    assert "hit_rate" not in res and "total_net_pts" not in res


def test_span_is_counted_in_accrued_sessions_not_calendar_days():
    """A data outage does not inflate the accrued span toward the 6-month gate."""
    outage = pd.DataFrame({"date_D1": ["2026-01-05", "2026-08-20"]})  # 2 sessions, 7.5 months apart
    assert fr.accrued_months(outage) == pytest.approx(2 / 21.0)
    dense = pd.DataFrame({"date_D1": [f"d{i}" for i in range(126)]})
    assert fr.accrued_months(dense) == pytest.approx(6.0)


# --- no statistics below the gate ---
_STAT_KEYS = (
    "hit_rate",
    "total_net_pts",
    "net_per_trade_pts",
    "gross_per_trade_pts",
    "months",
    "criteria",
    "by_scenario",
)


def _one_row_ledger():
    return pd.DataFrame([{"date_D1": "2026-08-17", "net_pts": -8.02, "gross_pts": -7.6, "fee_pts": 0.419}])


def test_sub_threshold_gate_returns_only_n_and_verdict():
    res = fr.gate(_one_row_ledger(), fr.CARDS_BY_ID["C7"])
    assert res == {"n": 1, "verdict": "ACCRUING"}
    assert not any(k in res for k in _STAT_KEYS)


def test_empty_ledger_leaks_nothing():
    assert fr.gate(pd.DataFrame(), fr.CARDS_BY_ID["N1"]) == {"n": 0, "verdict": "ACCRUING"}


def test_forward_eval_evaluate_is_suppressed_below_the_gate():
    from historical.research_lab import forward_eval

    res = forward_eval.evaluate(_one_row_ledger(), "N1")
    assert res == {"n": 1, "verdict": "ACCRUING"}


def test_basis_eval_is_suppressed_below_the_gate():
    from historical.research_lab import basis_eval

    res = basis_eval.evaluate(_one_row_ledger())
    assert res["verdict"] == "ACCRUING" and res["n"] == 1
    assert not any(k in res for k in _STAT_KEYS)


def test_force_peek_unblinds_but_stamps_and_counts_the_touch(tmp_path):
    log = tmp_path / "peek_log.jsonl"
    res = fr.gate(_one_row_ledger(), fr.CARDS_BY_ID["C7"], force_peek=True, peek_log=log)
    assert res["unblinded_peek"] is True
    assert res["peek_touch"] == 1
    assert "hit_rate" in res and "total_net_pts" in res  # visible after a forced peek
    res2 = fr.gate(_one_row_ledger(), fr.CARDS_BY_ID["C7"], force_peek=True, peek_log=log)
    assert res2["peek_touch"] == 2  # each look is recorded
    logged = fr.read_ledger(log)
    assert [r["card"] for r in logged] == ["C7", "C7"]
    assert logged[0]["n_at_peek"] == 1


def test_card_status_always_carries_the_power_arithmetic():
    st = fr.card_status(fr.CARDS_BY_ID["C7"], fr.gate(_one_row_ledger(), fr.CARDS_BY_ID["C7"]))
    assert st["verdict"] == "ACCRUING"
    assert st["n_required_80pct"] > 40000
    assert st["years_to_verdict"] > 500
    # still no statistics
    assert not any(k in st for k in _STAT_KEYS)


# --- legacy v1 ledger migration ---
_REAL_V1_ROW = {
    "v": 1,
    "date_D": "2026-08-14",
    "date_D1": "2026-08-17",
    "fade_dir": 1,
    "ret_D": -33.1,
    "thresh": 21.84,
    "atr14": 34.139,
    "entry_mid": 1881.8,
    "entry": 1882.3,
    "stop_px": 1848.162,
    "exit": 1874.7,
    "exit_reason": "close",
    "gross_pts": -7.6,
    "fee_pts": 0.419,
    "net_pts": -8.02,
}


def _write_v1(p):
    p.write_text(json.dumps(_REAL_V1_ROW) + "\n", encoding="utf-8")
    return p


def test_appending_onto_an_unmigrated_v1_ledger_is_refused(tmp_path):
    p = _write_v1(tmp_path / "c7.jsonl")
    with pytest.raises(fr.LedgerConflict) as e:
        fr.append_rows(p, [_row()])
    assert "migrate_legacy_ledger" in str(e.value)


def test_migration_preserves_every_recorded_number_and_starts_the_chain(tmp_path):
    p = _write_v1(tmp_path / "c7.jsonl")
    info = fr.migrate_legacy_ledger(p, "C7")
    assert info["migrated"] == 1
    assert Path(info["backup"]).exists()  # the v1 file is kept as a backup
    fr.verify_chain(p)
    r = fr.read_ledger(p)[0]
    assert r["migrated_from"] == "v1" and r["side"] == 1
    ws = r["scores"]["WORSE_SIDE"]
    assert (ws["entry"], ws["exit"], ws["net_pts"]) == (1882.3, 1874.7, -8.02)
    # no REALISTIC score is invented for a row recorded before dual-cost scoring
    assert "REALISTIC" not in r["scores"]
    assert r["signal"] == {"ret_D": -33.1, "thresh": 21.84, "atr14": 34.139}


def test_migration_is_idempotent_and_a_rerun_does_not_duplicate(tmp_path):
    p = _write_v1(tmp_path / "c7.jsonl")
    fr.migrate_legacy_ledger(p, "C7")
    assert fr.migrate_legacy_ledger(p, "C7") == {"migrated": 0, "already_chained": True}
    # re-recording the same trade from the tape is recognised, not duplicated
    recomputed = {
        "v": fr.LEDGER_VERSION,
        "card": "C7",
        "date_D": "2026-08-14",
        "date_D1": "2026-08-17",
        "side": 1,
        "entry_mid": 1881.8,
        "exit_mid": 1875.2,
        "stop_dist": 34.139,
        "d1_high": 1900.0,
        "d1_low": 1870.0,
        "signal": {"ret_D": -33.1, "thresh": 21.84, "atr14": 34.139},
        "scores": {
            "WORSE_SIDE": {
                "scenario": "WORSE_SIDE",
                "half_spread_pts": 0.5,
                "entry": 1882.3,
                "stop_px": 1848.162,
                "exit": 1874.7,
                "exit_reason": "close",
                "gross_pts": -7.6,
                "fee_pts": 0.419,
                "slip_pts": 1.0,
                "net_pts": -8.02,
            },
            "REALISTIC": {
                "scenario": "REALISTIC",
                "half_spread_pts": 0.1,
                "entry": 1881.9,
                "stop_px": 1847.761,
                "exit": 1875.1,
                "exit_reason": "close",
                "gross_pts": -6.8,
                "fee_pts": 0.419,
                "slip_pts": 0.2,
                "net_pts": -7.22,
            },
        },
    }
    res = fr.append_rows(p, [recomputed])
    assert res == {"appended": 0, "skipped": 1, "total": 1}


def test_a_migrated_row_that_changed_underneath_us_still_conflicts(tmp_path):
    p = _write_v1(tmp_path / "c7.jsonl")
    fr.migrate_legacy_ledger(p, "C7")
    changed = dict(_REAL_V1_ROW)
    changed["net_pts"] = 5.0
    row = {
        "v": fr.LEDGER_VERSION,
        "card": "C7",
        "date_D": "2026-08-14",
        "date_D1": "2026-08-17",
        "side": 1,
        "entry_mid": 1881.8,
        "exit_mid": None,
        "stop_dist": None,
        "d1_high": None,
        "d1_low": None,
        "signal": {"ret_D": -33.1, "thresh": 21.84, "atr14": 34.139},
        "scores": {
            "WORSE_SIDE": {
                "scenario": "WORSE_SIDE",
                "half_spread_pts": 0.5,
                "entry": 1882.3,
                "stop_px": 1848.162,
                "exit": 1874.7,
                "exit_reason": "close",
                "gross_pts": -7.6,
                "fee_pts": 0.419,
                "slip_pts": 1.0,
                "net_pts": 5.0,
            }
        },
    }
    with pytest.raises(fr.LedgerConflict):
        fr.append_rows(p, [row])
