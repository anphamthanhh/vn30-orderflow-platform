"""Validate an M1 OHLCV CSV (VN30F1M) before ClickHouse integration.

    python data_provider/etl/validate_vn30f1m_csv.py <csv> [--cross <close_only_csv>]
        [--out-dir <dir>] [--max-jump-pct 3.0]

Checks schema, duplicate timestamps, ordering, OHLC consistency, session windows, daily
coverage, intraday close jumps and, with ``--cross``, agreement with an independent close
series. Each check reports ``PASS``, ``WARN`` or ``FAIL``; a ``FAIL`` in schema, duplicates,
ordering or OHLC makes the verdict NO-GO (exit 1). Writes ``validation_report.md`` and
``validation_summary.json`` to ``--out-dir`` (default: the CSV's directory). Offline: pandas only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# VN30F1M session windows (minutes-of-day, ICT wall clock).
AM_START, AM_END = 9 * 60, 11 * 60 + 30  # 09:00 .. 11:30
PM_START, PM_END = 13 * 60, 14 * 60 + 30  # 13:00 .. 14:30 continuous
ATC_MIN = 14 * 60 + 45  # single 14:45 ATC bar

HARD_GATES = ("schema", "duplicates", "ordering", "ohlc")


def load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    return df


def check_schema(df: pd.DataFrame) -> dict:
    required = ["timestamp", "ticker", "open", "high", "low", "close", "volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        return {"status": "FAIL", "missing_columns": missing}
    issues = {}
    ts = pd.to_datetime(df["timestamp"], errors="coerce")
    issues["unparseable_timestamps"] = int(ts.isna().sum())
    for c in ["open", "high", "low", "close", "volume"]:
        issues[f"null_{c}"] = int(pd.to_numeric(df[c], errors="coerce").isna().sum())
    issues["tickers"] = df["ticker"].astype(str).value_counts().to_dict()
    bad = issues["unparseable_timestamps"] + sum(
        v for k, v in issues.items() if k.startswith("null_")
    )
    issues["status"] = "PASS" if bad == 0 and len(issues["tickers"]) == 1 else "FAIL"
    return issues


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ts"] = pd.to_datetime(out["timestamp"])
    for c in ["open", "high", "low", "close", "volume"]:
        out[c] = pd.to_numeric(out[c])
    out["mod"] = out["ts"].dt.hour * 60 + out["ts"].dt.minute
    out["date"] = out["ts"].dt.date
    return out


def check_duplicates(df: pd.DataFrame) -> dict:
    dup_mask = df["ts"].duplicated(keep=False)
    n = int(dup_mask.sum())
    # exact duplicates (same ts and OHLCV) dedupe losslessly; conflicting ones fail the check
    full_dup = int(df.duplicated(subset=["ts", "open", "high", "low", "close", "volume"]).sum())
    conflicting = int(df["ts"].duplicated().sum()) - full_dup
    return {
        "rows_in_duplicate_groups": n,
        "removable_exact_duplicates": full_dup,
        "conflicting_duplicates": conflicting,
        "sample": [str(t) for t in df.loc[dup_mask, "ts"].head(5)],
        "status": "PASS" if conflicting == 0 else "FAIL",
    }


def check_ordering(df: pd.DataFrame) -> dict:
    ok = bool(df["ts"].is_monotonic_increasing)
    return {"monotonic_non_decreasing": ok, "status": "PASS" if ok else "FAIL"}


def check_ohlc(df: pd.DataFrame) -> dict:
    high_bad = int((df["high"] < df[["open", "close"]].max(axis=1)).sum())
    low_bad = int((df["low"] > df[["open", "close"]].min(axis=1)).sum())
    hl_bad = int((df["high"] < df["low"]).sum())
    px_bad = int((df[["open", "high", "low", "close"]] <= 0).any(axis=1).sum())
    vol_neg = int((df["volume"] < 0).sum())
    vol_zero = int((df["volume"] == 0).sum())
    bad = high_bad + low_bad + hl_bad + px_bad + vol_neg
    return {
        "high_below_body": high_bad,
        "low_above_body": low_bad,
        "high_below_low": hl_bad,
        "nonpositive_price": px_bad,
        "negative_volume": vol_neg,
        "zero_volume_bars": vol_zero,  # informational (thin minutes happen)
        "status": "PASS" if bad == 0 else "FAIL",
    }


def check_sessions(df: pd.DataFrame) -> dict:
    m = df["mod"]
    in_am = (m >= AM_START) & (m <= AM_END)
    in_pm = (m >= PM_START) & (m <= PM_END)
    is_atc = m == ATC_MIN
    outside = ~(in_am | in_pm | is_atc)
    weekend = df["ts"].dt.dayofweek >= 5
    n_out = int(outside.sum())
    n_wk = int(weekend.sum())
    return {
        "rows_outside_session": n_out,
        "outside_sample": [str(t) for t in df.loc[outside, "ts"].head(5)],
        "weekend_rows": n_wk,
        "atc_bars": int(is_atc.sum()),
        "status": "PASS" if n_out == 0 and n_wk == 0 else "WARN",
    }


def check_coverage(df: pd.DataFrame) -> dict:
    per_day = df.groupby("date").size()
    days = per_day.index
    # Expected full day ≈ 150 AM (09:00-11:29) + 91 PM (13:00-14:30) + 1 ATC.
    full = 242
    partial = per_day[per_day < 0.6 * full]
    bdays = pd.bdate_range(min(days), max(days)).date
    missing_weekdays = sorted(set(bdays) - set(days))
    return {
        "trading_days": int(len(days)),
        "first_day": str(min(days)),
        "last_day": str(max(days)),
        "bars_per_day_median": float(per_day.median()),
        "bars_per_day_p5": float(per_day.quantile(0.05)),
        "partial_days_lt60pct": int(len(partial)),
        "partial_sample": {str(k): int(v) for k, v in partial.head(5).items()},
        "missing_weekdays": int(len(missing_weekdays)),
        "missing_weekday_sample": [str(d) for d in missing_weekdays[:8]],
        "status": "WARN" if len(partial) or len(missing_weekdays) else "PASS",
    }


def check_jumps(df: pd.DataFrame, max_jump_pct: float) -> dict:
    prev_close = df["close"].shift(1)
    jump_pct = (df["close"] - prev_close).abs() / prev_close * 100.0
    same_day = df["date"] == df["date"].shift(1)
    # Lunch boundary (first PM bar after an AM bar) legitimately gaps.
    lunch_edge = same_day & (df["mod"] >= PM_START) & (df["mod"].shift(1) <= AM_END)
    intraday = same_day & ~lunch_edge
    big = df[intraday & (jump_pct > max_jump_pct)]
    overnight = df[~same_day & (jump_pct > max_jump_pct)]
    return {
        "threshold_pct": max_jump_pct,
        "intraday_jumps": int(len(big)),
        "intraday_sample": [
            f"{t} Δ{j:.2f}%" for t, j in zip(big["ts"].head(5), jump_pct[big.index].head(5))
        ],
        "overnight_gaps_gt_threshold": int(len(overnight)),  # rolls/news — expected
        "status": "PASS" if len(big) == 0 else "WARN",
    }


def check_cross(df: pd.DataFrame, cross_path: Path) -> dict:
    other = pd.read_csv(cross_path)
    # case-insensitive column match: `timestamp,...,close` or AmiBroker `Ticker,Date/Time,Close`
    cols = {c.lower().strip("/ "): c for c in other.columns}
    ts_col = cols.get("timestamp") or cols.get("date/time") or cols.get("datetime")
    close_col = cols.get("close")
    if ts_col is None or close_col is None:
        return {"status": "WARN", "note": f"cross columns not recognised: {list(other.columns)}"}
    other = other[[ts_col, close_col]].copy()
    other["ts"] = pd.to_datetime(other[ts_col], format="mixed")
    other = other.rename(columns={close_col: "close_other"})[["ts", "close_other"]]
    merged = df[["ts", "close"]].merge(other, on="ts", how="inner")
    if merged.empty:
        return {"overlap_rows": 0, "status": "WARN", "note": "no overlapping timestamps"}
    diff = (merged["close"] - merged["close_other"]).abs()
    mism = merged[diff > 0.05]  # > half a tick
    return {
        "overlap_rows": int(len(merged)),
        "overlap_window": f"{merged['ts'].min()} .. {merged['ts'].max()}",
        "mismatch_gt_half_tick": int(len(mism)),
        "mismatch_pct": float(len(mism) / len(merged) * 100.0),
        "max_abs_diff": float(diff.max()),
        "status": "PASS" if len(mism) / len(merged) < 0.001 else "WARN",
    }


def run(csv: Path, cross: Path | None, max_jump_pct: float) -> dict:
    raw = load_csv(csv)
    results: dict = {"file": str(csv), "rows": int(len(raw))}
    results["schema"] = check_schema(raw)
    if results["schema"]["status"] == "FAIL":
        results["verdict"] = "NO-GO"
        return results
    df = prepare(raw)
    results["duplicates"] = check_duplicates(df)
    results["ordering"] = check_ordering(df)
    results["ohlc"] = check_ohlc(df)
    results["sessions"] = check_sessions(df)
    results["coverage"] = check_coverage(df)
    results["jumps"] = check_jumps(df, max_jump_pct)
    if cross is not None:
        results["cross"] = check_cross(df, cross)
    hard_fail = any(results[g]["status"] == "FAIL" for g in HARD_GATES)
    results["verdict"] = "NO-GO" if hard_fail else "GO"
    return results


def render_md(r: dict) -> str:
    lines = [
        "# VN30F1M CSV Validation Report",
        "",
        f"**File:** `{r['file']}` · **Rows:** {r['rows']:,} · **Verdict: {r['verdict']}**",
        "",
        "| Check | Status | Key numbers |",
        "|---|---|---|",
    ]
    for name in [
        "schema",
        "duplicates",
        "ordering",
        "ohlc",
        "sessions",
        "coverage",
        "jumps",
        "cross",
    ]:
        if name not in r:
            continue
        c = dict(r[name])
        status = c.pop("status")
        keys = ", ".join(f"{k}={v}" for k, v in c.items() if not isinstance(v, (list, dict)))
        lines.append(f"| {name} | {status} | {keys} |")
    lines += ["", "Hard gates: " + ", ".join(HARD_GATES) + " — any FAIL => NO-GO.", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("csv")
    ap.add_argument("--cross", default=None, help="independent close-only CSV for agreement check")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--max-jump-pct", type=float, default=3.0)
    args = ap.parse_args(argv)

    csv = Path(args.csv)
    cross = Path(args.cross) if args.cross else None
    results = run(csv, cross, args.max_jump_pct)

    out_dir = Path(args.out_dir) if args.out_dir else csv.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    def _default(o):
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating,)):
            return float(o)
        return str(o)

    (out_dir / "validation_summary.json").write_text(
        json.dumps(results, indent=1, default=_default), encoding="utf-8"
    )
    (out_dir / "validation_report.md").write_text(render_md(results), encoding="utf-8")
    print(render_md(results))
    return 0 if results["verdict"] == "GO" else 1


if __name__ == "__main__":
    sys.exit(main())
