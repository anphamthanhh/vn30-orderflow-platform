"""Demo 01 - VN30F1M transaction-cost model and the derivatives PIT correction (ADR 0020).

Shows round-trip fees before and after the correction, the frozen cost scenarios, holding-period
and initial-margin sensitivity, break-even slippage by price, and the Stage-0 fee hurdle on random
entries. All numbers come from ``historical/backtest_engine/costs.py`` (the Python mirror of the
Rust FillSim, parity-tested in ``historical/tests/test_fee_parity.py``) and ``fee_hurdle.py``.

    python demos/01-cost-model.py
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from lib.common import md_table, out_dir, pyplot, stamp  # also puts the repo root on sys.path

from historical.backtest_engine import costs
from historical.backtest_engine.costs import CostScenario
from historical.backtest_engine.fee_hurdle import fee_hurdle, gross_favorable_excursion
from samples.synthetic.index_basis import MarketParams, simulate

OUT = out_dir("01-cost-model")
PRICES = (900.0, 1242.0, 1500.0, 1900.0)
OLD_PIT_RATE_ON_FULL_NOTIONAL = 0.0005  # superseded model (ADR 0020): 0.05% of full notional per side


def old_round_trip_fee_points(price: float) -> float:
    """Round-trip fee in points under the superseded model described in ADR 0020 (for comparison)."""
    side = costs.TXN_FEE_VND_PER_SIDE + OLD_PIT_RATE_ON_FULL_NOTIONAL * price * costs.MULTIPLIER
    return (2 * side + costs.POSITION_FEE_VND_PER_DAY) / costs.MULTIPLIER


def fee_tables() -> tuple[list[dict], list[dict], list[dict]]:
    def pit_rt(p: float, rate: float, base: float) -> float:  # tax component only, both sides, points
        return 2 * rate * p * base
    fees = [{"price": p, "old fee (pts)": f"{old_round_trip_fee_points(p):.3f}",
             "corrected fee (pts)": f"{costs.round_trip_fee_points(p):.3f}",
             "total fee ratio": f"{old_round_trip_fee_points(p) / costs.round_trip_fee_points(p):.1f}x",
             "tax component ratio": f"{pit_rt(p, OLD_PIT_RATE_ON_FULL_NOTIONAL, 1.0) / pit_rt(p, costs.INCOME_TAX_RATE, costs.INITIAL_MARGIN_RATIO / 2):.1f}x"}
            for p in PRICES]
    scen = [{"scenario": s.value.name, "slip per fill": f"{s.slip_per_fill_points:+.4f}",
             "all-in round trip @1900 (pts)": f"{costs.round_trip_cost(1900.0, scenario=s):.3f}",
             "role": s.rationale.split(". ")[0]} for s in CostScenario]
    holds = [{"hold (ICT days crossed)": d, "fee @1900 (pts)": f"{costs.round_trip_fee_points(1900.0, hold_days=d):.3f}"}
             for d in (0, 1, 5, 10)]
    return fees, scen, holds


def im_sensitivity() -> list[dict]:
    """PIT base = notional x IM/2, so the fee moves with the VSDC initial-margin ratio."""
    rows, saved = [], costs.INITIAL_MARGIN_RATIO
    try:
        for im in (0.13, 0.17, 0.24):
            costs.INITIAL_MARGIN_RATIO = im  # module constant, restored below
            rows.append({"VSDC IM ratio": f"{im:.0%}", "fee @1900 (pts)": f"{costs.round_trip_fee_points(1900.0):.3f}"})
    finally:
        costs.INITIAL_MARGIN_RATIO = saved
    return rows


def stage0_on_random_entries() -> dict:
    """Stage-0 fee hurdle on random entries: the gate is necessary, not sufficient."""
    m1, _ = simulate(MarketParams(n_days=60))
    # unix seconds, independent of the datetime resolution (pandas 3 defaults to microseconds)
    ts = (m1["ts"] - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(seconds=1)
    bars = pd.DataFrame({"ts_open": ts, "ts_close": ts + 60, "high": m1["high"], "low": m1["low"]})
    rng = np.random.default_rng(7)
    pick = rng.choice(len(m1) - 60, size=300, replace=False)
    entries = pd.DataFrame({"entry_ts": ts.iloc[pick].to_numpy(), "dir": rng.choice(["long", "short"], 300),
                            "entry_price": m1["close"].iloc[pick].to_numpy()})
    return fee_hurdle(gross_favorable_excursion(bars, entries, horizon_min=45))


def plot(fees_px: np.ndarray) -> None:
    plt = pyplot()
    fig, ax = plt.subplots(figsize=(7, 3.8))
    ax.plot(fees_px, [old_round_trip_fee_points(p) for p in fees_px], label="old: PIT 0.05% x full notional")
    ax.plot(fees_px, [costs.round_trip_fee_points(p) for p in fees_px], label="corrected: PIT 0.1% x notional x IM/2")
    for s, ls in ((CostScenario.REALISTIC, "--"), (CostScenario.WORSE_SIDE, ":")):
        ax.plot(fees_px, [costs.round_trip_cost(p, scenario=s) for p in fees_px], ls, label=f"all-in, {s.value.name}")
    ax.set_xlabel("VN30F1M price (index points)")
    ax.set_ylabel("round-trip cost per contract (points)")
    ax.set_title("Round-trip fee: tax component was ~5.9x too high, total fee ~4-5x")
    ax.legend(fontsize=7)
    stamp(fig)
    fig.tight_layout()
    fig.savefig(OUT / "round-trip-cost-vs-price.png")


def main() -> int:
    fees, scen, holds = fee_tables()
    ims = im_sensitivity()
    curve = costs.break_even_slippage_curve(1.5)
    s0 = stage0_on_random_entries()
    plot(np.linspace(800, 2000, 61))
    be = [{"price": p, "break-even slip per fill for 1.5 pt gross": f"{v:.3f}"} for p, v in curve.items()]
    report = (
        "# Demo 01 - cost model and the derivatives PIT correction\n\n"
        "Code under test: `historical/backtest_engine/costs.py`, `fee_hurdle.py` (unchanged).\n\n"
        "## Fee per round trip, 1 contract, intraday (excludes slippage)\n\n" + md_table(fees, list(fees[0])) +
        "\nFormula (corrected): `0.0995 + 0.00017 x price + 0.0255 x extra_days` points.\n\n"
        "## Frozen cost scenarios (pre-registered bars)\n\n" + md_table(scen, list(scen[0])) +
        "\n## Multi-day holds pay the position fee per ICT day\n\n" + md_table(holds, list(holds[0])) +
        "\n## Sensitivity to the VSDC initial-margin ratio\n\n" + md_table(ims, list(ims[0])) +
        "\n## Break-even slippage is a curve over price, not a scalar\n\n" + md_table(be, list(be[0])) +
        "\n## Stage-0 fee hurdle on 300 RANDOM entries (synthetic tape)\n\n"
        f"`{ {k: (round(v, 3) if isinstance(v, float) else v) for k, v in s0.items()} }`\n\n"
        "Random entries clear the corrected fee on most signals: the gate is a necessary filter "
        "(an idea that cannot even reach the fee is dead) and never evidence of edge.\n\n"
        "![cost](round-trip-cost-vs-price.png)\n"
    )
    (OUT / "report.md").write_text(report, encoding="utf-8", newline="\n")
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
