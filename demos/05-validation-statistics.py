"""Demo 05 - selection bias in the best of many backtests: DSR, PBO and MinBTL on noise.

Applies ``historical/optimization/dsr.py`` and ``overfitting.py`` to synthetic daily returns:
200 skill-less strategies, with and without one skilled strategy.

    python demos/05-validation-statistics.py
"""
from __future__ import annotations

import sys

import numpy as np

from lib.common import md_table, out_dir, pyplot, stamp  # also puts the repo root on sys.path
from lib.selection_bias import best_of_n, cscv_pbo, make_universe, min_backtest_length, required_sharpe_for_dsr

OUT = out_dir("05-validation-statistics")
N_STRATS = 200
CASES = (  # (label, true annual Sharpe of strategy 0 or None, sessions of data)
    ("200 skill-less, 1 year", None, 250),
    ("199 skill-less + 1 skilled (true Sharpe 2.5), 1 year", 2.5, 250),
    ("199 skill-less + 1 skilled (true Sharpe 2.5), 4 years", 2.5, 1000),
)


def fmt(x: float) -> str:
    return f"{x:.3f}"


def main() -> int:
    rows = []
    for label, skilled, n_sessions in CASES:
        u = make_universe(N_STRATS, n_sessions, skilled, seed=20260923)
        b = best_of_n(u)
        rows.append({"universe": label,
                     "best annualised Sharpe": fmt(b["best_annual_sharpe"]),
                     "best is the skilled one": "-" if skilled is None else str(b["best_is_the_skilled_one"]),
                     "PSR vs 0": fmt(b["psr_vs_zero"]), "DSR vs 200 trials": fmt(b["dsr_vs_trials"]),
                     "PBO (CSCV, 16 blocks)": fmt(cscv_pbo(u))})
    minbtl = [{"trials N": n, "years for annual Sharpe 1.0": f"{min_backtest_length(n, 1.0):.1f}",
               "years for annual Sharpe 2.0": f"{min_backtest_length(n, 2.0):.1f}"} for n in (10, 100, 659, 5000)]

    ns = np.unique(np.logspace(0.5, 3.7, 25).astype(int))
    plt = pyplot()
    fig, ax = plt.subplots(figsize=(7, 3.8))
    for n_obs in (250, 1000, 2500):
        ax.plot(ns, [required_sharpe_for_dsr(int(n), n_obs) for n in ns], label=f"{n_obs} sessions of data")
    ax.axvline(659, color="#888888", lw=0.7, ls=":")
    ax.text(700, ax.get_ylim()[1] * 0.9, "N = 659\n(project ledger)", fontsize=7, color="#555555")
    ax.set_xscale("log")
    ax.set_xlabel("number of trials behind the selected strategy")
    ax.set_ylabel("per-session Sharpe needed for DSR >= 0.95")
    ax.set_title("The bar a selected strategy must clear rises with every trial")
    ax.legend(fontsize=7)
    stamp(fig)
    fig.tight_layout()
    fig.savefig(OUT / "required-sharpe-vs-trials.png")

    report = (
        "# Demo 05 - validation statistics on noise\n\n"
        "Code under test: `historical/optimization/dsr.py` and `overfitting.py` (unchanged). "
        "Data: seeded simulated daily returns (`demos/lib/selection_bias.py`), synthetic by construction.\n\n"
        "## Best of 200: looks significant, is not\n\n" + md_table(rows, list(rows[0])) +
        "\nReading, row by row:\n\n"
        "1. The best of 200 skill-less strategies earns a PSR against zero close to 1 — it *looks* "
        "significant — while its deflated Sharpe, which accounts for the 200 trials that produced it, "
        "does not; a PBO at or above 0.5 says the in-sample winner is no better than a coin flip out of "
        "sample.\n"
        "2. With one year of data a genuinely skilled strategy is not even guaranteed to be the one "
        "selected: the luckiest noise can out-rank it.\n"
        "3. With four years of data the skilled strategy is selected, its deflated Sharpe rises and the "
        "PBO falls — the statistics separate skill from luck once the sample allows it.\n\n"
        "## Minimum backtest length (years) grows with the number of trials\n\n" + md_table(minbtl, list(minbtl[0])) +
        "\n## The bar that retired a gate\n\nWith hundreds of trials the per-session Sharpe a candidate "
        "needs for DSR >= 0.95 is several times what one year of data can resolve. The research "
        "programme measured this on its own ledger (amendment A18: required 0.080 per session against "
        "a best-ever 0.027) and stopped using DSR as permission, keeping it as reporting.\n\n"
        "![required sharpe](required-sharpe-vs-trials.png)\n"
    )
    (OUT / "report.md").write_text(report, encoding="utf-8", newline="\n")
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
