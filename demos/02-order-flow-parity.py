"""Demo 02 - order-flow features and Python/Rust parity.

The Python producers (``historical/l2/producers``) and the Rust ``l2_dump`` binary
(``native_platform/crates/orderflow-engine``) read the same synthetic fixtures; their CVD and TPO
dumps must be byte-identical. Book features (spread, microprice, residual-depth tilt) are shown
from the Python producer.

    python demos/02-order-flow-parity.py      # Rust half needs cargo; Python half always runs
"""
from __future__ import annotations

import hashlib
import io
import sys

import pandas as pd

from lib.cargo import SKIPPED, cargo_available, cargo_run
from lib.common import REPO_ROOT, md_table, out_dir, pyplot, stamp

from historical.l2.producers import book_cost, prof_tpo, tape_cvd

OUT = out_dir("02-order-flow-parity")
FIX = REPO_ROOT / "historical" / "tests" / "fixtures"
TAPE = FIX / "l2_tape_cvd_synthetic_20210304_20210305.csv"
TPO = FIX / "l2_prof_tpo_synthetic_20210304_20210305.csv"
BOOK = FIX / "book_cost_book_synthetic_20210304_20210305.csv"


def py_dump(kind: str, feature: str, emit: str = "") -> str:
    buf = io.StringIO()
    if kind == "cvd":
        tape_cvd.write_tsv(tape_cvd.produce(TAPE, feature), buf)
    else:
        prof_tpo.write_tsv(prof_tpo.produce(TPO, feature, emit=emit), buf, emit=emit)
    return buf.getvalue()


def rust_dump(kind: str, feature: str, emit: str = "") -> str:
    args = ["--feature", feature, "--tape", str(TAPE if kind == "cvd" else TPO)]
    return cargo_run("orderflow-engine", "l2_dump", args + (["--emit", emit] if emit else []))


def parity_rows(with_rust: bool) -> list[dict]:
    cases = [("cvd", "tape_cvd_lot_v1", ""), ("cvd", "tape_cvd_norst_lot_v1", ""),
             ("tpo", "prof_tpo_poc_pts_v1", "observation"), ("tpo", "prof_tpo_poc_pts_v1", "profile")]
    rows = []
    for kind, feature, emit in cases:
        py = py_dump(kind, feature, emit)
        row = {"feature": feature + (f" ({emit})" if emit else ""), "rows": py.count("\n") - 1,
               "python sha256": hashlib.sha256(py.encode()).hexdigest()[:12]}
        if with_rust:
            rs = rust_dump(kind, feature, emit)
            row["rust sha256"] = hashlib.sha256(rs.encode()).hexdigest()[:12]
            row["byte-identical"] = "yes" if rs == py else "**NO**"
        rows.append(row)
    return rows


def plots() -> dict:
    cvd = pd.read_csv(io.StringIO(py_dump("cvd", "tape_cvd_lot_v1")), sep="\t")
    obs = pd.read_csv(io.StringIO(py_dump("tpo", "prof_tpo_poc_pts_v1", "observation")), sep="\t")
    prof = pd.read_csv(io.StringIO(py_dump("tpo", "prof_tpo_poc_pts_v1", "profile")), sep="\t")
    plt = pyplot()
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(10, 3.8), gridspec_kw={"width_ratios": [2, 1]})
    a1.plot(cvd["idx"], cvd["cvd"], lw=0.8)
    a1.set_title("CVD (lots) over the fixture's windows: day reset, 13:00 rollover")
    a1.set_xlabel("print index")
    s0 = obs.iloc[0]
    day = prof[prof["session"] == s0["session"]]
    a2.barh(day["price_tick"] / 10, day["tpo_count"], height=0.09, color="#8172b2")
    for tick, name, col in ((s0["poc_tick"], "POC", "#c44e52"), (s0["val_tick"], "VAL", "#55a868"),
                            (s0["vah_tick"], "VAH", "#55a868")):
        a2.axhline(tick / 10, color=col, lw=0.8)
        a2.text(a2.get_xlim()[1] * 0.7, tick / 10, name, color=col, fontsize=7, va="bottom")
    a2.set_title(f"TPO profile, session {s0['session']}")
    a2.set_xlabel("TPO count")
    stamp(fig)
    fig.tight_layout()
    fig.savefig(OUT / "cvd-and-profile.png")
    return s0.to_dict()


def book_table() -> list[dict]:
    feats = ("ob_spread_pts_v1", "ob_microprice_pts_v1", "ob_depth_resid_imb_ratio_v1")
    cols = {f: {r.minute: r.value for r in book_cost.produce(BOOK, f)} for f in feats}
    minutes = sorted(cols[feats[0]])[:6]
    ict = lambda m: pd.Timestamp(m * 60, unit="s", tz="UTC").tz_convert("Asia/Ho_Chi_Minh").strftime("%Y-%m-%d %H:%M")  # noqa: E731
    return [{"minute (ICT)": ict(m), **{f: f"{cols[f].get(m, float('nan')):.4f}" for f in feats}} for m in minutes]


def main() -> int:
    with_rust = cargo_available()
    parity = parity_rows(with_rust)
    obs = plots()
    book = book_table()
    rust_note = ("" if with_rust else "\n**Rust half not run** (cargo not found): parity is NOT checked "
                 "in this run — install the toolchain to see it.\n")
    report = (
        "# Demo 02 - order-flow features and cross-stack parity\n\n"
        "Fixtures: `historical/tests/fixtures/*synthetic_20210304_20210305*` (SHA-256 pinned in their "
        "manifests). Python: `historical/l2/producers`. Rust: `l2_dump` (orderflow-engine).\n\n"
        "## Python vs Rust\n\n" + md_table(parity, list(parity[0])) + rust_note +
        f"\nSession {obs['session']}: POC tick {obs['poc_tick']}, value area {obs['val_tick']}–"
        f"{obs['vah_tick']} ({obs['va_width_ticks']} ticks), {obs['prints']} prints.\n\n"
        "## Book features (Python producer, per minute, first minutes)\n\n" + md_table(book, list(book[0])) +
        "\n![cvd and profile](cvd-and-profile.png)\n"
    )
    (OUT / "report.md").write_text(report, encoding="utf-8", newline="\n")
    print(report)
    if with_rust and any(r.get("byte-identical") != "yes" for r in parity):
        return 1
    return 0 if with_rust else SKIPPED


if __name__ == "__main__":
    sys.exit(main())
