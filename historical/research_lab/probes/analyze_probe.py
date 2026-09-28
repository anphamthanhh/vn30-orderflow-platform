"""Summarise the resting-limit probe log into fill statistics and slippage per fill.

Per offset: fill rate with a 95 % Wilson interval, time to fill (median, p90), adverse
selection (mid moved through the limit; mark-out) and an effective slippage per fill from
``costs.passive_queue_slip_points`` with measured inputs, comparable to the ``CostScenario``
bars. The decision gate is read from the interval, never from the point estimate.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(REPO_ROOT))

from historical.backtest_engine.costs import (  # noqa: E402
    TICK,
    CostScenario,
    passive_queue_slip_points,
)
from historical.research_lab.probes.resting_limit_probe import (  # noqa: E402
    DEFAULT_LOG_PATH,
    read_log,
)

Z_95 = 1.959963984540054

# Touch fill-rate gate, applied to the 95 % CI bounds (see _touch_verdict).
KILL_TOUCH_FILL_RATE = 0.30
BUILD_TOUCH_FILL_RATE = 0.50


# --- statistics ---
def wilson_interval(k: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval for ``k`` successes in ``n`` trials; ``(0.0, 1.0)`` when n = 0.

    Unlike the Wald interval it does not collapse to a point at k = 0 or k = n.
    """
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    return (max(0.0, center - margin), min(1.0, center + margin))


def _percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile (standard library only); None for no values."""
    xs = sorted(float(v) for v in values)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return xs[int(pos)]
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


# --- row-level quantities ---
def half_spread_earned_pts(row: dict) -> float | None:
    """Maker credit of the limit versus the placement mid, in points.

    ``mid - limit`` for a buy, ``limit - mid`` for a sell; positive for a resting order.
    """
    mid, px, side = row.get("mid"), row.get("limit_px"), row.get("side")
    if mid is None or px is None or side not in ("buy", "sell"):
        return None
    return round((mid - px) if side == "buy" else (px - mid), 6)


def markout_pts(row: dict) -> float | None:
    """Mark-to-mid P&L of a fill at resolution, in points; negative is adverse.

    ``mid_at_resolve - fill_px`` for a buy, ``fill_px - mid_at_resolve`` for a sell.
    """
    if not row.get("filled"):
        return None
    fx, mr, side = row.get("fill_px"), row.get("mid_at_resolve"), row.get("side")
    if fx is None or mr is None or side not in ("buy", "sell"):
        return None
    return round((mr - fx) if side == "buy" else (fx - mr), 6)


def moved_through_limit(row: dict) -> bool | None:
    """Whether the mid at resolution moved through the limit of a filled order.

    A stricter adverse-selection test than a negative mark-out; None for unfilled rows.
    """
    if not row.get("filled"):
        return None
    px, mr, side = row.get("limit_px"), row.get("mid_at_resolve"), row.get("side")
    if px is None or mr is None or side not in ("buy", "sell"):
        return None
    return bool(mr < px) if side == "buy" else bool(mr > px)


def measurable_rows(rows: Iterable[dict], include_dry_run: bool = False) -> list[dict]:
    """Return rows with a fill/no-fill outcome (``filled`` is not None).

    Dry-run rows are excluded by default; counted as no-fills they would bias the fill rate down.
    """
    out = []
    for r in rows:
        if r.get("dry_run") and not include_dry_run:
            continue
        if r.get("filled") is None:
            continue
        out.append(r)
    return out


# --- aggregation ---
def _offset_stats(rows: Sequence[dict]) -> dict:
    n = len(rows)
    fills = [r for r in rows if r.get("filled")]
    k = len(fills)
    lo, hi = wilson_interval(k, n)
    ttf = [r["time_to_fill_s"] for r in fills if r.get("time_to_fill_s") is not None]
    through = [moved_through_limit(r) for r in fills]
    through = [t for t in through if t is not None]
    marks = [m for m in (markout_pts(r) for r in fills) if m is not None]
    halves = [h for h in (half_spread_earned_pts(r) for r in rows) if h is not None]
    spreads = [r["spread_ticks"] for r in rows if r.get("spread_ticks") is not None]
    queues = [r["queue_ahead_at_place"] for r in rows if r.get("queue_ahead_at_place") is not None]
    vols = [r["vol_1min_at_place"] for r in rows if r.get("vol_1min_at_place") is not None]
    return {
        "n": n,
        "fills": k,
        "fill_rate": (k / n) if n else None,
        "fill_rate_ci95": [round(lo, 4), round(hi, 4)],
        "time_to_fill_median_s": _percentile(ttf, 0.50),
        "time_to_fill_p90_s": _percentile(ttf, 0.90),
        "adverse_moved_through_frac": (sum(through) / len(through)) if through else None,
        "markout_mean_pts": (sum(marks) / len(marks)) if marks else None,
        "half_spread_mean_pts": (sum(halves) / len(halves)) if halves else None,
        "spread_ticks_median": _percentile(spreads, 0.5),
        "queue_ahead_median": _percentile(queues, 0.5),
        "queue_ahead_measured_n": len(queues),
        "vol_1min_median_pts": _percentile(vols, 0.5),
    }


def effective_slippage(stats: dict) -> dict:
    """Convert one offset's stats into slippage per fill in points, comparable to the bars.

    Inputs: ``p`` = fill rate; ``half_spread`` = mean credit versus the placement mid;
    ``adverse_selection`` = half_spread - mean mark-out; ``chase`` = half the median spread (a
    proxy, since no book is recorded at resolution). The 95 % range comes from the Wilson
    bounds on ``p``; ``distinguishes_bars`` is False when that range contains a bar.
    """
    p = stats.get("fill_rate")
    hs = stats.get("half_spread_mean_pts")
    if p is None or hs is None:
        return {"available": False, "reason": "no fill rate or no half-spread measured"}
    mk = stats.get("markout_mean_pts")
    adverse_selection = hs - mk if mk is not None else hs  # no fills => all credit assumed lost
    spread_ticks = stats.get("spread_ticks_median")
    chase = (spread_ticks * TICK / 2.0) if spread_ticks is not None else hs

    def at(prob: float) -> float:
        return passive_queue_slip_points(
            half_spread_pts=hs,
            fill_prob=prob,
            adverse_selection_pts=adverse_selection,
            chase_pts=chase,
        )

    lo_p, hi_p = stats["fill_rate_ci95"]
    # sort by value: which end of the fill-probability interval is worse depends on the inputs
    bounds = sorted((at(lo_p), at(hi_p)))
    realistic = CostScenario.REALISTIC.slip_per_fill_points
    worse = CostScenario.WORSE_SIDE.slip_per_fill_points
    # a bar inside the interval cannot be ranked against the measurement
    ambiguous_bars = [
        name
        for name, bar in (("REALISTIC", realistic), ("WORSE_SIDE", worse))
        if bounds[0] <= bar <= bounds[1]
    ]
    spans_bars = bool(ambiguous_bars)
    return {
        "available": True,
        "slip_per_fill_pts": round(at(p), 5),
        "slip_per_fill_ci95_pts": [round(bounds[0], 5), round(bounds[1], 5)],
        "inputs": {
            "fill_prob": round(p, 4),
            "half_spread_pts": round(hs, 5),
            "adverse_selection_pts": round(adverse_selection, 5),
            "chase_pts": round(chase, 5),
        },
        "vs_REALISTIC_pts": round(at(p) - realistic, 5),
        "vs_WORSE_SIDE_pts": round(at(p) - worse, 5),
        "distinguishes_bars": not spans_bars,
        "ambiguous_vs": ambiguous_bars,
        "note": (
            ""
            if not spans_bars
            else "N too small: the 95% interval CONTAINS "
            + " and ".join(ambiguous_bars)
            + ", so this measurement cannot say whether the design is cheaper or "
            "dearer than that bar. The point estimate would decide it by rounding."
        ),
    }


def _touch_verdict(touch: dict) -> dict:
    """Touch fill-rate gate on the 95 % CI; ``INSUFFICIENT`` while the CI straddles it."""
    n, k = touch.get("n", 0), touch.get("fills", 0)
    lo, hi = touch.get("fill_rate_ci95", [0.0, 1.0])
    if n == 0:
        return {"verdict": "NO_DATA", "reason": "no touch-offset orders resolved"}
    if hi < KILL_TOUCH_FILL_RATE:
        return {
            "verdict": "MAKER_THESIS_WEAK",
            "reason": (
                f"touch fill-rate 95% CI upper bound {hi:.0%} < {KILL_TOUCH_FILL_RATE:.0%}. "
                "Keep worseside (taker) as the floor; do NOT build a queue model."
            ),
        }
    if lo >= BUILD_TOUCH_FILL_RATE:
        return {
            "verdict": "BUILD_LIMIT_FILL_MODEL",
            "reason": (
                f"touch fill-rate 95% CI lower bound {lo:.0%} >= {BUILD_TOUCH_FILL_RATE:.0%} "
                f"on n={n} ({k} fills). Calibrate LimitFillModel v1 to the measured "
                "fill-rate + adverse-selection penalty."
            ),
        }
    return {
        "verdict": "INSUFFICIENT",
        "reason": (
            f"touch fill-rate {k}/{n} = {k / n:.0%}, 95% CI [{lo:.0%}, {hi:.0%}] straddles the "
            f"{KILL_TOUCH_FILL_RATE:.0%}/{BUILD_TOUCH_FILL_RATE:.0%} kill/build gate. A point "
            "estimate here would be a decision made by rounding. Accrue more sessions."
        ),
    }


def summarize(rows: Iterable[dict], include_dry_run: bool = False) -> dict:
    """Aggregate measurable rows per offset and overall, with slippage and the touch gate."""
    rows = measurable_rows(rows, include_dry_run=include_dry_run)
    by_offset: dict[str, dict] = {}
    offsets = sorted({int(r["offset_ticks"]) for r in rows if r.get("offset_ticks") is not None})
    for off in offsets:
        sel = [r for r in rows if int(r.get("offset_ticks", -1)) == off]
        st = _offset_stats(sel)
        st["effective_slippage"] = effective_slippage(st)
        by_offset[str(off)] = st
    overall = _offset_stats(rows)
    overall["effective_slippage"] = effective_slippage(overall)
    touch = by_offset.get("0", {"n": 0, "fills": 0, "fill_rate_ci95": [0.0, 1.0]})
    return {
        "n_rows": len(rows),
        "by_offset": by_offset,
        "overall": overall,
        "gate": _touch_verdict(touch),
        "bars": {
            "REALISTIC_pts_per_fill": CostScenario.REALISTIC.slip_per_fill_points,
            "WORSE_SIDE_pts_per_fill": CostScenario.WORSE_SIDE.slip_per_fill_points,
        },
    }


# --- report ---
def _fmt(v, spec: str = ".3f") -> str:
    return "n/a" if v is None else format(v, spec)


def render_report(summary: dict) -> str:
    lines = [
        "# Resting-limit probe — measured fill quality on VN30F1M (demo)",
        "",
        f"Resolved orders: **{summary['n_rows']}**. Every fill-rate below carries a 95% "
        "Wilson interval; at this N the interval, not the point estimate, is the result.",
        "",
        "| offset | n | fills | fill-rate | 95% CI | median TTF (s) | p90 TTF (s) | "
        "moved-through | mark-out (pt) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    label = {"0": "touch", "1": "+1 tick", "2": "+2 ticks"}
    for off, st in summary["by_offset"].items():
        lo, hi = st["fill_rate_ci95"]
        lines.append(
            f"| {label.get(off, off)} | {st['n']} | {st['fills']} | "
            f"{_fmt(st['fill_rate'], '.0%')} | [{lo:.0%}, {hi:.0%}] | "
            f"{_fmt(st['time_to_fill_median_s'], '.1f')} | "
            f"{_fmt(st['time_to_fill_p90_s'], '.1f')} | "
            f"{_fmt(st['adverse_moved_through_frac'], '.0%')} | "
            f"{_fmt(st['markout_mean_pts'], '+.4f')} |"
        )
    lines += ["", "## Effective slippage per fill (the comparable unit)", ""]
    bars = summary["bars"]
    lines.append(
        f"Frozen bars: REALISTIC = {bars['REALISTIC_pts_per_fill']} pt/fill, "
        f"WORSE_SIDE = {bars['WORSE_SIDE_pts_per_fill']} pt/fill. Negative = a maker CREDIT."
    )
    lines.append("")
    lines.append("| offset | slip/fill (pt) | 95% CI | vs REALISTIC | vs WORSE_SIDE | usable? |")
    lines.append("|---|---|---|---|---|---|")
    for off, st in summary["by_offset"].items():
        es = st["effective_slippage"]
        if not es.get("available"):
            lines.append(f"| {label.get(off, off)} | n/a | n/a | n/a | n/a | {es.get('reason')} |")
            continue
        lo, hi = es["slip_per_fill_ci95_pts"]
        usable = (
            "yes"
            if es["distinguishes_bars"]
            else "NO — CI contains " + "/".join(es.get("ambiguous_vs") or [])
        )
        lines.append(
            f"| {label.get(off, off)} | {es['slip_per_fill_pts']:+.4f} | "
            f"[{lo:+.4f}, {hi:+.4f}] | {es['vs_REALISTIC_pts']:+.4f} | "
            f"{es['vs_WORSE_SIDE_pts']:+.4f} | {usable} |"
        )
    gate = summary["gate"]
    lines += [
        "",
        "## Decision gate",
        "",
        f"**{gate['verdict']}** — {gate['reason']}",
        "",
        "The gate is read off the confidence interval, not the point estimate. A 3/3 "
        "touch fill-rate renders as 100% and has a 95% lower bound of 44%: it does not "
        "exclude the gate's own KILL branch, so it is not a verdict.",
        "",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Analyze the resting-limit probe log.")
    ap.add_argument("--log", default=str(DEFAULT_LOG_PATH))
    ap.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    ap.add_argument("--out", help="write the markdown report to this path")
    ap.add_argument(
        "--include-dry-run",
        action="store_true",
        help="INCLUDE dry-run rows (they have no outcome; this can only mislead)",
    )
    args = ap.parse_args(argv)
    summary = summarize(read_log(args.log), include_dry_run=args.include_dry_run)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    report = render_report(summary)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
