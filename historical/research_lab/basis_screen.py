"""Basis screen: does the VN30F1M basis to the VN30 index predict the next session's futures return?

The cash index cannot be shorted, so the basis is a directional conditioner on a futures trade
and must clear the same round-trip cost as any other signal. Features use the basis at the D
close (level and trailing z-score); the trade enters at the D+1 open and exits at the D+1 close.
Pairs that cross an expiry roll are excluded. The screen reports the Spearman IC, the
cheap-minus-rich quintile spread and a fixed fade rule's gross per trade against fee-only,
all-in and worse-side costs. A pass nominates a forward test; it is not evidence of an edge.

    python historical/research_lab/basis_screen.py              # query ClickHouse
    python historical/research_lab/basis_screen.py --csv daily_basis.csv
"""

from __future__ import annotations

import datetime as _dt
import io
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from historical.backtest_engine.fee_hurdle import (  # noqa: E402
    round_trip_cost_points,
    round_trip_fee_points,
)
from historical.research_lab.daily import third_thursday  # noqa: E402
from historical.research_lab.lab import assert_export_range_allowed  # noqa: E402

CH_DEFAULT = "http://localhost:8123/"
Z_LOOKBACK = 20  # trailing sessions for the causal basis z-score
EXTREME_Q = 0.80  # fade only the top quintile of |z|
WORSE_SIDE_PTS = 0.5  # worse-side slippage per fill in points (PRE_REGISTRATION.md)

# aliases must not shadow source columns, or ClickHouse sees an aggregate inside an aggregate
_DAILY_QUERY = (
    "SELECT toDate(time_1m) d, "
    "argMin(front_close, time_1m) f_open, "
    "argMax(front_close, time_1m) f_close, "
    "argMax(basis, time_1m) basis_eod, "
    "round(avg(basis),3) basis_avg "
    "FROM default.basis_m1 GROUP BY d ORDER BY d FORMAT JSONEachRow"
)


def load_daily_ch(ch: str = CH_DEFAULT) -> pd.DataFrame:
    """Query ClickHouse for per-ICT-day front open/close and end-of-day and average basis."""
    proc = subprocess.run(
        ["curl", "-s", "-m", "30", "--data-binary", _DAILY_QUERY, ch],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(f"CH query failed rc={proc.returncode}: {proc.stderr.strip()}")
    if proc.stdout.lstrip().startswith("Code:"):
        raise RuntimeError(f"CH error: {proc.stdout.strip()[:200]}")
    return pd.read_json(io.StringIO(proc.stdout), lines=True)


def _flag_roll_boundary(dates: pd.Series) -> np.ndarray:
    """Return True where D is an expiry day, so D to D+1 crosses a contract roll.

    The expiry is the last date present on or before the third Thursday (holiday-safe).
    """
    idx = pd.DatetimeIndex(pd.to_datetime(dates))
    flag = np.zeros(len(idx), dtype=bool)
    for y, m in sorted({(t.year, t.month) for t in idx}):
        nominal = third_thursday(y, m)
        prior = idx[idx <= nominal]
        if len(prior) == 0:
            continue
        eff = prior.max()
        if (nominal - eff).days <= 7:
            flag |= idx == eff
    return flag


def screen(daily: pd.DataFrame) -> dict:
    """Run the screen on a daily basis frame; return IC, spread and fade metrics and a verdict."""
    from scipy import stats

    d = daily.sort_values("d").reset_index(drop=True).copy()
    d["basis_z"] = (d["basis_eod"] - d["basis_eod"].rolling(Z_LOOKBACK).mean()) / d[
        "basis_eod"
    ].rolling(Z_LOOKBACK).std()
    # forward return: enter at the D+1 open, exit at the D+1 close
    d["fwd_ret"] = d["f_close"].shift(-1) - d["f_open"].shift(-1)
    d["is_roll"] = _flag_roll_boundary(d["d"])
    # valid rows: warm z-score, a next session, and no roll between D and D+1
    v = d[d["basis_z"].notna() & d["fwd_ret"].notna() & ~d["is_roll"]].copy()
    n = int(len(v))
    if n < 30:
        return {"n": n, "verdict": "INSUFFICIENT_DATA"}

    ic = float(stats.spearmanr(v["basis_eod"], v["fwd_ret"]).statistic)
    ic_z = float(stats.spearmanr(v["basis_z"], v["fwd_ret"]).statistic)
    fee = float(v["f_close"].map(round_trip_fee_points).median())

    # quintile cohorts by end-of-day basis: top = rich, bottom = cheap
    q = v["basis_eod"].quantile([0.2, 0.8]).to_numpy()
    top = v[v["basis_eod"] >= q[1]]["fwd_ret"]
    bot = v[v["basis_eod"] <= q[0]]["fwd_ret"]
    spread = float(bot.mean() - top.mean())  # long-cheap / short-rich reversion spread

    # fixed fade rule: on the extreme-|z| cohort trade -sign(basis_z); gross points per trade
    thr = float(v["basis_z"].abs().quantile(EXTREME_Q))
    ex = v[v["basis_z"].abs() >= thr].copy()
    side = -np.sign(ex["basis_z"].to_numpy())  # +1 long (cheap) / -1 short (rich)
    ex_gross = side * ex["fwd_ret"].to_numpy()
    fade_gross = float(np.mean(ex_gross)) if len(ex) else 0.0
    fade_hit = float(np.mean(ex_gross > 0)) if len(ex) else 0.0
    fade_price = float(ex["f_close"].median()) if len(ex) else float(v["f_close"].median())
    fade_fee = round_trip_fee_points(fade_price)  # fee only (excl. slippage)
    # cost bars: all-in = fee + 2 x 0.1 pt taker slippage; worse-side = fee + 2 x 0.5 pt
    fade_cost = round_trip_cost_points(fade_price)
    fade_cost_ws = fade_fee + 2.0 * WORSE_SIDE_PTS

    # with a reversion sign (IC < 0): PULSE if the fade clears the worse-side bar, MARGINAL if
    # it clears only the fee; otherwise KILL
    if ic < 0 and fade_gross > fade_cost_ws:
        verdict = "PULSE_REGISTER_FORWARD"
    elif ic < 0 and fade_gross > fade_fee:
        verdict = "MARGINAL_REGISTER_FORWARD"
    else:
        verdict = "KILL_AT_SCREEN"
    return {
        "n": n,
        "ic_basis": round(ic, 4),
        "ic_basis_z": round(ic_z, 4),
        "reversion_spread_pts": round(spread, 3),
        "fee_pts": round(fee, 3),
        "fade_n": int(len(ex)),
        "fade_gross_per_trade_pts": round(fade_gross, 3),
        "fade_hit_rate": round(fade_hit, 3),
        "fade_fee_pts": round(fade_fee, 3),
        "fade_cost_allin_pts": round(fade_cost, 3),
        "fade_cost_worseside_pts": round(fade_cost_ws, 3),
        "fade_net_worseside_pts": round(fade_gross - fade_cost_ws, 3),
        "verdict": verdict,
    }


def _print(res: dict) -> None:
    print(f"Basis screen — {res['n']} clean daily pairs (roll-boundary excluded)")
    if res["verdict"] == "INSUFFICIENT_DATA":
        print("  ==> INSUFFICIENT_DATA (<30 clean pairs)")
        return
    print(
        f"  IC(basis, fwd) {res['ic_basis']} | IC(basis_z, fwd) {res['ic_basis_z']} "
        f"(negative = reversion / fade premium)"
    )
    print(
        f"  reversion spread (cheap-rich) {res['reversion_spread_pts']}pt vs fee {res['fee_pts']}pt"
    )
    print(
        f"  FADE extreme-|z| cohort: n={res['fade_n']} | gross/trade "
        f"{res['fade_gross_per_trade_pts']}pt | hit {res['fade_hit_rate']:.0%}"
    )
    print(
        f"    vs cost bars: fee-only {res['fade_fee_pts']}pt | all-in "
        f"{res['fade_cost_allin_pts']}pt | worse-side {res['fade_cost_worseside_pts']}pt "
        f"=> net(worse-side) {res['fade_net_worseside_pts']:+}pt"
    )
    print(f"  ==> {res['verdict']}")
    print(
        "  NOTE: all-seen 1yr, single-ish regime => hypothesis-gen only. A pulse earns "
        "a FROZEN forward card (judged post-cutoff), never a live edge claim."
    )


def assert_dev_only_range(daily: pd.DataFrame, allow_holdout: bool = False) -> None:
    """Raise ``SystemExit`` if the frame's observed date span overlaps a reserved window.

    Checks the data actually loaded, so a query without an explicit date range cannot bypass it.
    """
    if daily.empty:
        return
    d = pd.to_datetime(daily["d"] if "d" in daily.columns else daily.iloc[:, 0])
    start = d.min().date().isoformat()
    end_excl = (d.max().date() + _dt.timedelta(days=1)).isoformat()
    assert_export_range_allowed(start, end_excl, allow=allow_holdout)


def main(argv: list[str]) -> int:
    if "--csv" in argv:
        daily = pd.read_csv(argv[argv.index("--csv") + 1])
    else:
        ch = argv[argv.index("--ch") + 1] if "--ch" in argv else CH_DEFAULT
        daily = load_daily_ch(ch)
    # `--allow-holdout` is for a final verdict only; it spends the reserved range
    assert_dev_only_range(daily, allow_holdout="--allow-holdout" in argv)
    _print(screen(daily))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
