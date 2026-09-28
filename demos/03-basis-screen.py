"""Demo 03 - VN30F1M/VN30 basis screen: verdicts, the holdout lock and the screen's error rates.

Runs ``historical/research_lab/basis_screen.py`` on synthetic markets with a known planted effect
and measures how often the frozen Stage-0 screen passes noise (null pass rate) and real effects
(power).

    python demos/03-basis-screen.py            # ~1 min (150 markets per effect size)
    python demos/03-basis-screen.py --full     # ~3 min (400 markets per effect size)
"""
from __future__ import annotations

import datetime as dt
import sys

import numpy as np

from lib.common import md_table, out_dir, pyplot, stamp  # also puts the repo root on sys.path
from lib.basis_oc import T_CRIT, fade_trades, minimum_detectable_effect, operating_characteristics

from historical.research_lab.basis_screen import assert_dev_only_range, screen
from samples.synthetic.index_basis import MarketParams, simulate

EFFECTS = (0.0, 1.0, 2.0, 4.0, 8.0)
OUT = out_dir("03-basis-screen")


MARKETS = (
    ("null market (no edge)", MarketParams()),
    ("planted reversion k=8 (power ~100%)", MarketParams(planted_reversion_pts=8.0)),
)


def mechanical_convergence(n_markets: int = 100) -> list[dict]:
    """Mean fade gross per trade, with and without an overnight-persistent basis deviation."""
    rows = []
    for rho, label in ((0.0, "deviation redrawn each morning (true null)"),
                       (1.0, "deviation persists overnight (mechanical convergence)")):
        g = [fade_trades(simulate(MarketParams(seed=500 + s, dev_overnight_persistence=rho), with_m1=False)[1]).mean()
             for s in range(n_markets)]
        rows.append({"basis deviation": label, "markets": n_markets,
                     "mean fade gross/trade (pts)": f"{np.mean(g):.2f}",
                     "std error": f"{np.std(g, ddof=1) / np.sqrt(n_markets):.2f}"})
    return rows


def single_runs() -> tuple[list[dict], object, object]:
    rows, null_m1, null_daily = [], None, None
    for label, params in MARKETS:
        m1, daily = simulate(params)
        if null_m1 is None:
            null_m1, null_daily = m1, daily
        r = screen(daily)
        rows.append({"market": label, "clean pairs": r["n"], "IC(basis,fwd)": r["ic_basis"],
                     "fade n": r["fade_n"], "fade gross/trade": r["fade_gross_per_trade_pts"],
                     "worse-side cost": r["fade_cost_worseside_pts"], "verdict": r["verdict"]})
    return rows, null_m1, null_daily


def holdout_guard_check() -> list[str]:
    """The project's holdout lock accepts DEV-range dates and refuses the reserved range."""
    lines = []
    for start in (dt.date(2021, 1, 4), dt.date(2025, 1, 2)):
        _, daily = simulate(MarketParams(start=start, n_days=40), with_m1=False)
        try:
            assert_dev_only_range(daily)
            lines.append(f"- frame {daily.d.iloc[0]}..{daily.d.iloc[-1]}: **accepted** (DEV range)")
        except SystemExit as exc:  # the guard exits the process, as in production use
            lines.append(f"- frame {daily.d.iloc[0]}..{daily.d.iloc[-1]}: **refused** - `{str(exc)[:110]}...`")
    return lines


def plot_basis(m1, daily) -> None:
    plt = pyplot()
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(9, 5), sharex=True)
    ts = m1["ts"].dt.tz_localize(None)
    a1.plot(ts, m1["basis"], lw=0.4, color="#4c72b0", label="M1 basis = front - VN30 (pts)")
    exp = daily[daily["is_expiry"]]
    for d in exp["d"]:
        a1.axvline(dt.datetime.fromisoformat(d), color="#c44e52", lw=0.6, alpha=0.6)
    a1.set_ylabel("points")
    a1.legend(loc="upper right")
    a1.set_title("Synthetic VN30F1M basis (red = expiry: roll-boundary pairs are excluded)")
    z = (daily["basis_eod"] - daily["basis_eod"].rolling(20).mean()) / daily["basis_eod"].rolling(20).std()
    a2.plot([dt.datetime.fromisoformat(d) for d in daily["d"]], z, color="#55a868", lw=0.9)
    a2.axhline(0, color="k", lw=0.5)
    a2.set_ylabel("EOD basis z (20 sessions)")
    stamp(fig)
    fig.tight_layout()
    fig.savefig(OUT / "synthetic-basis.png")


def plot_oc(oc) -> None:
    plt = pyplot()
    fig, ax = plt.subplots(figsize=(7, 3.8))
    ax.plot(oc["planted_effect_pts_per_z"], oc["frozen_pass_rate"], "o-", label="frozen Stage-0 screen")
    ax.plot(oc["planted_effect_pts_per_z"], oc["proposed_pass_rate"], "s--",
            label=f"proposed: + fade t-stat >= {T_CRIT}")
    ax.axhline(0.05, color="#888888", lw=0.6, ls=":")
    ax.set_xlabel("planted reversion effect (points of next-session drift per unit z)")
    ax.set_ylabel("pass-to-forward rate")
    ax.set_ylim(0, 1.02)
    ax.set_title("Operating characteristics: effect 0 = null pass rate, effect > 0 = power")
    ax.legend(loc="lower right")
    stamp(fig)
    fig.tight_layout()
    fig.savefig(OUT / "operating-characteristics.png")


def main(argv: list[str]) -> int:
    n_seeds = 400 if "--full" in argv else 150
    rows, m1, daily = single_runs()
    guard = holdout_guard_check()
    mech = mechanical_convergence()
    oc = operating_characteristics(EFFECTS, n_seeds)
    null = oc.iloc[0]
    mde = minimum_detectable_effect(null["median_sd_trade_pts"], int(null["median_n_fade"]))
    plot_basis(m1, daily)
    plot_oc(oc)
    oc_rows = [{k: (f"{v:.0%}" if "rate" in k else (f"{v:.1f}" if isinstance(v, float) else v))
                for k, v in r.items()} for r in oc.to_dict("records")]
    (OUT / "report.md").write_text(
        "# Demo 03 - basis screen on synthetic markets\n\n"
        "Code under test: `historical/research_lab/basis_screen.py` (unchanged). Data: `samples/synthetic`.\n\n"
        "## 1. One null market and one market with a planted effect\n\n"
        + md_table(rows, list(rows[0])) +
        "\n## 2. Holdout lock (`historical/research_lab/holdout_lock.json`)\n\n" + "\n".join(guard) +
        "\n\n## 3. Mechanical convergence of the basis\n\n" + md_table(mech, list(mech[0])) +
        f"\n## 4. Operating characteristics ({n_seeds} markets per effect size)\n\n"
        + md_table(oc_rows, list(oc_rows[0])) +
        f"\nMinimum detectable per-trade gross at 5% / 80% power with n = {int(null['median_n_fade'])} "
        f"fade trades and sd = {null['median_sd_trade_pts']:.1f} pts: **{mde:.1f} points**.\n\n"
        "Reading:\n\n"
        "- With one year of daily data the frozen screen behaves as a direction filter: it passes a large "
        "share of pure-noise markets to a forward card. That matches its own design - a PULSE earns a "
        "frozen forward test, never an edge claim - and it quantifies why that forward test is needed.\n"
        "- The proposed t-stat requirement brings the null pass rate to the nominal 5% level, at the price "
        "of power for small effects. It would ship as a new pre-registration amendment, not as an edit.\n"
        "- A basis deviation that persists overnight gives the futures leg a mechanical convergence gain "
        "that the fade rule captures. Whether that gain survives costs is exactly the retail-vs-prop "
        "question in `docs/knowledge/04-basis-and-index-arbitrage.md`.\n\n"
        "![basis](synthetic-basis.png)\n\n![oc](operating-characteristics.png)\n",
        encoding="utf-8",
        newline="\n",
    )
    print((OUT / "report.md").read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
