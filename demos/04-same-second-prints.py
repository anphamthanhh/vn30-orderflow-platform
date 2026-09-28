"""Demo 04 - same-second prints and the M1 close: ordering by (timestamp, ingest_seq).

Exchange timestamps have one-second resolution while several trades print per second, so "the
price at the latest timestamp" depends on the physical row order, which storage merges change.
The demo rebuilds M1 closes from the synthetic tape two ways:

  argmax-by-timestamp     : latest second, first row the store happens to return  (non-deterministic)
  (timestamp, ingest_seq) : latest second, last row in arrival order              (deterministic)

    python demos/04-same-second-prints.py
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from lib.common import md_table, out_dir, pyplot, stamp  # also puts the repo root on sys.path

from samples.synthetic.microstructure import TapeParams, simulate_l2

OUT = out_dir("04-same-second-prints")
SHUFFLES = 25


def minute_closes(tape: pd.DataFrame, order: np.ndarray) -> pd.Series:
    """Close per minute taking, inside the latest second, the first row in ``order``."""
    t = tape.iloc[order]
    last_sec = t.groupby("minute")["ts"].transform("max")
    return t[t["ts"] == last_sec].groupby("minute", sort=True)["price"].first()


def main() -> int:
    tape, _ = simulate_l2(TapeParams())
    tape = tape[tape["aggressor"] != "U"].reset_index(drop=True)  # continuous-session prints only
    tape["ts"] = pd.to_datetime(tape["event_timestamp"])
    tape["minute"] = tape["ts"].dt.floor("min")
    tape["ingest_seq"] = np.arange(len(tape))  # arrival order, as the ingest process stamps it

    truth = minute_closes(tape, np.argsort(-tape["ingest_seq"].to_numpy()))  # last arrival first
    rng = np.random.default_rng(7)
    wrong = []
    for _ in range(SHUFFLES):
        wrong.append(float((minute_closes(tape, rng.permutation(len(tape))) != truth).mean()))
    deterministic = minute_closes(tape.sort_values(["ts", "ingest_seq"], ascending=False).reset_index(drop=True),
                                  np.arange(len(tape)))
    det_wrong = float((deterministic != truth).mean())

    per_sec = tape.groupby("ts").size()
    last_sec_counts = tape.assign(n=tape.groupby("ts")["ts"].transform("size")) \
        .sort_values("ts").groupby("minute")["n"].last()
    rows = [
        {"method": "argmax by timestamp (storage order)", "wrong closes (mean over shuffles)": f"{np.mean(wrong):.1%}",
         "min / max": f"{min(wrong):.1%} / {max(wrong):.1%}"},
        {"method": "(timestamp, ingest_seq)", "wrong closes (mean over shuffles)": f"{det_wrong:.1%}", "min / max": "deterministic"},
    ]
    plt = pyplot()
    fig, ax = plt.subplots(figsize=(7, 3.4))
    ax.hist(last_sec_counts, bins=range(1, int(last_sec_counts.max()) + 2), color="#4c72b0", rwidth=0.9)
    ax.set_xlabel("prints in the final second of the minute")
    ax.set_ylabel("minutes")
    ax.set_title("Most minutes end with several prints sharing one timestamp")
    stamp(fig)
    fig.tight_layout()
    fig.savefig(OUT / "prints-in-final-second.png")
    report = (
        "# Demo 04 - same-second prints and the candle close\n\n"
        f"Synthetic tape: {len(tape):,} continuous-session prints over {tape['minute'].nunique()} minutes; "
        f"{per_sec.mean():.1f} prints per active second on average; "
        f"{(last_sec_counts > 1).mean():.0%} of minutes end with more than one print in the final second.\n\n"
        + md_table(rows, list(rows[0])) +
        f"\n{SHUFFLES} storage orders were drawn at random, mimicking merges that reorder rows. The "
        "production tape showed 56 % wrong closes (46 % on a clean-feed day); the rate depends on "
        "prints per second and on how often same-second prints differ in price.\n\n"
        "![final second](prints-in-final-second.png)\n"
    )
    (OUT / "report.md").write_text(report, encoding="utf-8", newline="\n")
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
