"""Demo 06 - the Rust backtester and its fill-model ladder on a synthetic tick tape.

Each strategy (cvd, supertrend) is replayed under four execution assumptions, from taker to
optimistic maker. The engine (``native_platform/crates/backtest-engine``) is deterministic: a
decision on tick N fills on tick N+1 at a tick price, and every fill pays the fee and PIT model of
``costs.py``.

    python demos/06-rust-backtest-fill-models.py      # needs the Rust toolchain (cargo)
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

import pandas as pd

from lib.cargo import SKIPPED, cargo_available, cargo_run
from lib.common import md_table, out_dir, pyplot, stamp

from samples.synthetic.microstructure import TapeParams, simulate_l2

OUT = out_dir("06-rust-backtest-fill-models")
FILLS = ("marketable", "worseside", "passivequeue", "passive")
RUNS = (("cvd", []), ("supertrend", ["--bar-seconds", "300"]))


def write_ticks(path: Path) -> int:
    tape, _ = simulate_l2(TapeParams())
    tape = tape[tape["aggressor"].isin(["B", "S"])]
    ts = (pd.to_datetime(tape["event_timestamp"]).dt.tz_localize("Asia/Ho_Chi_Minh")
          - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(seconds=1)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for t, p, v, s in zip(ts, tape["price"], tape["volume"], tape["aggressor"]):
            f.write(json.dumps({"ts": int(t), "price": float(p), "volume": int(v), "side": s}) + "\n")
    return len(tape)


def summary(stdout: str) -> dict:
    line = next((ln for ln in stdout.splitlines() if ln.startswith("SUMMARY")), "")
    return dict(re.findall(r"(\w+)=(\S+)", line))


def main() -> int:
    if not cargo_available():
        print("cargo not found: this demo needs the Rust toolchain (see native_platform/README.md)")
        return SKIPPED
    rows, curves = [], {}
    with tempfile.TemporaryDirectory() as tmp:
        ticks = Path(tmp) / "ticks.jsonl"
        n = write_ticks(ticks)
        for strategy, extra in RUNS:
            for fill in FILLS:
                run_dir = Path(tmp) / f"{strategy}-{fill}"
                run_dir.mkdir()
                s = summary(cargo_run("backtest-engine", "backtest",
                                      ["--ticks", str(ticks), "--strategy", strategy, "--exec", fill,
                                       "--out-dir", str(run_dir), *extra]))
                rows.append({"strategy": strategy, "fill model": fill, "trades": s.get("trades", "?"),
                             "net P&L (VND)": s.get("net_vnd", "?"), "final equity (VND)": s.get("final_equity", "?")})
                eq = run_dir / "equity.jsonl"
                if eq.exists():
                    curves[(strategy, fill)] = pd.read_json(eq, lines=True)
    plt = pyplot()
    fig, ax = plt.subplots(figsize=(7, 3.6))
    for (strategy, fill), df in curves.items():
        if strategy == "cvd" and "equity" in df.columns:
            ax.plot(range(len(df)), df["equity"] / 1e6, label=fill)
    ax.set_xlabel("equity sample")
    ax.set_ylabel("equity (million VND)")
    ax.set_title("Same signals, four execution assumptions (strategy: cvd)")
    ax.legend(fontsize=7)
    stamp(fig)
    fig.tight_layout()
    fig.savefig(OUT / "equity-by-fill-model.png")
    report = (
        "# Demo 06 - Rust backtester and fill models\n\n"
        f"Input: {n:,} synthetic buyer/seller-initiated prints (two sessions). Engine: "
        "`native_platform/crates/backtest-engine` (unchanged).\n\n" + md_table(rows, list(rows[0])) +
        "\nThe ladder brackets execution: `marketable` pays one tick per fill, `worseside` a 0.5-point "
        "half-spread, `passivequeue` the E1-calibrated maker expectation, `passive` the optimistic upper "
        "bound. On a synthetic tape the P&L itself means nothing; what matters is that the verdict of a "
        "design can flip between the bounds — which is why the research protocol freezes the bar it is "
        "judged on before the run.\n\n![equity](equity-by-fill-model.png)\n"
    )
    (OUT / "report.md").write_text(report, encoding="utf-8", newline="\n")
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
