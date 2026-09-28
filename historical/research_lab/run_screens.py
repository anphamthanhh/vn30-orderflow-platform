"""Run the pre-registered daily screens: C2 overnight/calendar drift and C3 day-type persistence.

    python historical/research_lab/run_screens.py [--m1 <csv>] [--out <report.md>]

The pass criteria are fixed in advance. Both screens are reconnaissance and count zero trials.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from historical.research_lab.daily import (  # noqa: E402
    daily_bars,
    halves,
    load_m1,
    tag_calendar,
    window_stat,
)

# --- C2 pass criteria ---
C2_T_CRIT = 2.87  # two-sided alpha = 0.05/8 Bonferroni
C2_MEAN_MIN = 1.0  # points per day held

# --- C3 pass criteria; delta = P(RANGE | prior RANGE) - P(RANGE) ---
C3_TREND_THRESHOLD = 0.40  # default day-type threshold, not tuned here
C3_DELTA_MIN = 0.05


def c2_windows(df) -> dict[str, "object"]:
    """Return the eight C2 return windows; overnight windows exclude roll-boundary rows."""
    ov = df.loc[~df["roll_boundary"], "overnight"]
    ic = df["intraday"]
    ov_df = df.loc[~df["roll_boundary"]]
    # the best-|t| weekday counts as one window
    dow_stats = {d: window_stat(ov_df.loc[ov_df["dow"] == d, "overnight"]) for d in range(5)}
    best_dow = max(dow_stats, key=lambda d: abs(dow_stats[d]["t"]))
    return {
        "overnight_all": ov,
        "intraday_all": ic,
        "overnight_tom": ov_df.loc[ov_df["tom"], "overnight"],
        "overnight_expiry_week": ov_df.loc[ov_df["expiry_week"], "overnight"],
        f"overnight_best_dow({best_dow})": ov_df.loc[ov_df["dow"] == best_dow, "overnight"],
        "intraday_tom": df.loc[df["tom"], "intraday"],
        "intraday_expiry_week": df.loc[df["expiry_week"], "intraday"],
        "overnight_ex_tom_expiry": ov_df.loc[~ov_df["tom"] & ~ov_df["expiry_week"], "overnight"],
    }


def run_c2(df) -> tuple[list[str], bool]:
    """Return the C2 report lines and whether any window passes."""
    lines = ["## C2 — Overnight & Calendar (frozen: |mean|>=1.0pt, |t|>=2.87, halves same-sign)"]
    lines.append("| window | n | mean (pt) | t | halves same-sign | PASS |")
    lines.append("|---|---|---|---|---|---|")
    h1, h2 = halves(df)
    any_pass = False
    for name, series in c2_windows(df).items():
        s = window_stat(series)
        s1 = window_stat(series.loc[series.index.intersection(h1.index)])
        s2 = window_stat(series.loc[series.index.intersection(h2.index)])
        stable = bool(np.sign(s1["mean"]) == np.sign(s2["mean"]) and s1["n"] and s2["n"])
        passed = bool(abs(s["mean"]) >= C2_MEAN_MIN and abs(s["t"]) >= C2_T_CRIT and stable)
        any_pass |= passed
        lines.append(
            f"| {name} | {s['n']} | {s['mean']:+.3f} | {s['t']:+.2f} | "
            f"{'yes' if stable else 'NO'} | {'**PASS**' if passed else 'fail'} |"
        )
    verdict = (
        "PASS — promote family to a Phase-03 trial" if any_pass else "KILL — no window survives"
    )
    lines.append(f"\n**C2 verdict: {verdict}**")
    return lines, any_pass


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Return the Wilson score interval for ``k`` successes in ``n`` trials."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    half = z * np.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return center - half, center + half


def daytypes(df) -> np.ndarray:
    """Label each day TREND if |close - open| / (high - low) reaches the threshold, else RANGE."""
    rng = (df["high"] - df["low"]).to_numpy(dtype=float)
    net = (df["close"] - df["open"]).abs().to_numpy(dtype=float)
    eff = np.where(rng > 0, net / rng, 0.0)
    return np.where(eff >= C3_TREND_THRESHOLD, "TREND", "RANGE")


def c3_stats(df) -> dict:
    """Return P(RANGE), P(RANGE | prior RANGE), their difference and its Wilson 95 % interval."""
    t = daytypes(df)
    prev, cur = t[:-1], t[1:]
    n_range_prev = int((prev == "RANGE").sum())
    k = int(((prev == "RANGE") & (cur == "RANGE")).sum())
    p_uncond = float((t == "RANGE").mean())
    p_cond = k / n_range_prev if n_range_prev else 0.0
    lo, hi = wilson_ci(k, n_range_prev)
    return {
        "n_days": int(len(t)),
        "p_range": p_uncond,
        "p_range_given_range": p_cond,
        "delta": p_cond - p_uncond,
        "wilson_lo": lo,
        "wilson_hi": hi,
        "ci_excludes_uncond": bool(lo > p_uncond or hi < p_uncond),
    }


def run_c3(df) -> tuple[list[str], bool]:
    """Return the C3 report lines and whether the screen passes."""
    full = c3_stats(df)
    h1, h2 = halves(df)
    s1, s2 = c3_stats(h1), c3_stats(h2)
    stable = bool(np.sign(s1["delta"]) == np.sign(s2["delta"]))
    passed = bool(full["delta"] >= C3_DELTA_MIN and full["ci_excludes_uncond"] and stable)
    lines = [
        "## C3 — Day-Type Persistence (frozen: Δ>=+0.05, Wilson CI excludes uncond, halves same-sign)",
        f"- days={full['n_days']}  P(RANGE)={full['p_range']:.3f}  "
        f"P(RANGE|RANGE)={full['p_range_given_range']:.3f}  Δ={full['delta']:+.3f}",
        f"- Wilson95 CI on P(RANGE|RANGE): [{full['wilson_lo']:.3f}, {full['wilson_hi']:.3f}]  "
        f"excludes-unconditional={'yes' if full['ci_excludes_uncond'] else 'NO'}",
        f"- halves Δ: {s1['delta']:+.3f} / {s2['delta']:+.3f}  same-sign={'yes' if stable else 'NO'}",
        f"\n**C3 verdict: {'PASS — prior-day-RANGE gate licensed for Phase-02' if passed else 'KILL — no persistence basis; Phase-02 needs a different causal gate'}**",
    ]
    return lines, passed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run pre-registered screens C2 + C3.")
    ap.add_argument("--m1", default=None, help="M1 CSV (default: data_provider/VN30F1M_Final.csv)")
    ap.add_argument("--out", default=None, help="output report md")
    args = ap.parse_args(argv)

    m1 = load_m1(args.m1) if args.m1 else load_m1()
    df = tag_calendar(daily_bars(m1))
    n_roll = int(df["roll_boundary"].sum())

    lines = [
        "# Phase-01 Screen Results (C2 + C3) — pre-registered, 0 trials",
        "",
        f"Data: {len(df)} trading days {df.index.min().date()} .. {df.index.max().date()} · "
        f"expiry days tagged: {int(df['is_expiry'].sum())} · roll-boundary overnights excluded: {n_roll}",
        "",
    ]
    c2_lines, _ = run_c2(df)
    c3_lines, _ = run_c3(df)
    lines += (
        c2_lines
        + [""]
        + c3_lines
        + [
            "",
            "C4 (cross-market): BLOCKED-OPERATOR (external data) — not run; criteria stay frozen.",
            "",
            "Reminder: copy verdicts into the C2/C3 hypothesis cards' RESULT sections and the ideas backlog.",
        ]
    )
    report = "\n".join(lines)
    # avoid UnicodeEncodeError on cp1252 consoles
    sys.stdout.reconfigure(errors="replace")
    print(report)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
