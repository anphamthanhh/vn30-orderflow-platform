"""Regenerate the synthetic L2 test fixtures (tape, book, TPO prices) and their manifests.

The parity tests pin every fixture by the SHA-256 of its LF-normalised bytes, so the output is
deterministic: same parameters -> same bytes. No real exchange data is included.

The TPO fixture must exercise the edge cases its tests cover: a tie at the maximum TPO count that
the canonical rule and a naive first-argmax resolve differently, and a trend day and a balance day
with clearly different shapes. Its seed comes from a deterministic search from a fixed base seed
and is recorded in the manifest.

    python samples/generate-fixtures.py            # write fixtures + manifests
    python samples/generate-fixtures.py --check    # verify committed fixtures are reproducible
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import sys
import tempfile
from dataclasses import asdict, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from historical.l2.prof_tape import read_price_tape  # noqa: E402
from historical.l2.producers import prof_tpo as pt  # noqa: E402
from samples.synthetic.microstructure import BOOK_MINUTES, TAPE_WINDOWS, TapeParams, simulate_l2, window  # noqa: E402

FIXTURES = ROOT / "historical" / "tests" / "fixtures"
TAG = "synthetic_20210304_20210305"
WHY = ("Synthetic sessions dated inside the DEV range 2017-11-06..2023-12-31 of holdout_lock.json, "
       "so the holdout guard accepts them; neither day is an expiry day (March 2021 expiry is the 18th).")
CVD_PARAMS = TapeParams(trades_per_sec=8.5)
TPO_BASE = TapeParams(seed=20260924, regimes=("trend", "balance"), trades_per_sec=3.0)


def _csv_bytes(df) -> bytes:
    buf = io.StringIO()
    df.to_csv(buf, index=False, quoting=csv.QUOTE_NONNUMERIC, lineterminator="\n")
    return buf.getvalue().encode("utf-8")


def _sha256_lf(data: bytes) -> str:
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def _tpo_candidate(params: TapeParams):
    """Return ``(csv_bytes, raw_measurements, properties, ok)`` for one TPO seed candidate."""
    tape, _ = simulate_l2(params)
    raw_rows = tape["event_timestamp"].str.slice(0, 10).value_counts().to_dict()
    kept = tape[["event_timestamp", "price"]].drop_duplicates().sort_values(["event_timestamp", "price"])
    data = _csv_bytes(kept.reset_index(drop=True))
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "tpo.csv"
        f.write_bytes(data)
        prints = read_price_tape(f)
    raw, levels, tie_breaks = {}, [], False
    obs = {o.session: o for o in pt.observations(prints)}
    for session, rows in pt.group_by_session(prints):
        hist = pt.tpo_histogram(rows)
        top = max(hist.values())
        tied = sorted(k for k, v in hist.items() if v == top)
        tie_breaks |= len(tied) > 1 and obs[session].poc_tick != tied[0]
        levels.append(len(hist))
        raw[session] = {"rows_raw": int(raw_rows[session]), "rows_kept": len(rows),
                        "tpo_cells": len({(pt.tpo_period_id(p.epoch_s), pt.bucket_tick(p.price)) for p in rows})}
    ok = tie_breaks and max(levels) >= 2 * min(levels) and all(r["rows_raw"] > r["rows_kept"] for r in raw.values())
    return data, raw, {"levels_per_session": levels, "canonical_tie_break_differs": tie_breaks}, ok


def build() -> dict[str, tuple[bytes, dict]]:
    tape, book = simulate_l2(CVD_PARAMS)
    tape_w, book_w = window(tape, seconds=TAPE_WINDOWS), window(book, minutes=BOOK_MINUTES)
    for offset in range(500):
        tpo_params = replace(TPO_BASE, seed=TPO_BASE.seed + offset)
        tpo_data, raw, props, ok = _tpo_candidate(tpo_params)
        if ok:
            break
    else:
        raise RuntimeError("no TPO seed within 500 exercises the tie-break and shape properties")
    common = {"symbol": "VN30F1M", "synthetic": True, "days": list(CVD_PARAMS.days), "why_these_days": WHY,
              "source": "samples/synthetic/microstructure.py"}
    out = {}
    for name, data, extra in (
        (f"l2_tape_cvd_{TAG}.csv", _csv_bytes(tape_w),
         {"generator_params": asdict(CVD_PARAMS), "second_of_day_windows": TAPE_WINDOWS,
          "aggressor_counts": tape_w["aggressor"].value_counts().to_dict(), "rows": int(len(tape_w))}),
        (f"book_cost_book_{TAG}.csv", _csv_bytes(book_w),
         {"generator_params": asdict(CVD_PARAMS), "minutes_of_session": list(BOOK_MINUTES), "rows": int(len(book_w))}),
        (f"l2_prof_tpo_{TAG}.csv", tpo_data,
         {"generator_params": asdict(tpo_params), "feature": "prof_tpo_poc_pts_v1", "raw_measurements": raw,
          "rows": sum(r["rows_kept"] for r in raw.values()), "fixture_design": props,
          "seed_search": f"first seed from {TPO_BASE.seed} whose sessions exercise the tie-break and shape tests"}),
    ):
        manifest = {"fixture": f"historical/tests/fixtures/{name}", **common, **extra,
                    "header": data.split(b"\n", 1)[0].decode(), "sha256_lf": _sha256_lf(data),
                    "note": "SYNTHETIC. Pins formulas across the Python and Rust implementations; "
                            "it says nothing about the real market."}
        out[name] = (data, manifest)
    return out


def main(argv: list[str]) -> int:
    built = build()
    if "--check" in argv:
        bad = [n for n, (data, _) in built.items() if (FIXTURES / n).read_bytes() != data]
        print("fixtures reproducible" if not bad else f"NOT reproducible: {bad}")
        return 1 if bad else 0
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, (data, manifest) in built.items():
        (FIXTURES / name).write_bytes(data)
        (FIXTURES / name).with_suffix(".manifest.json").write_text(
            json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8", newline="\n")
        print(f"{name}: {manifest['rows']} rows, sha256_lf {manifest['sha256_lf'][:16]}...")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
