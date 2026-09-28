"""Run every demo in order and write samples/outputs/run-all.md.

Exit codes of a demo: 0 = ok, 3 = skipped because a prerequisite (e.g. the Rust toolchain) is
missing, anything else = failure. This script exits non-zero if any demo failed.

    python demos/run-all.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMOS = sorted(p for p in (ROOT / "demos").glob("[0-9][0-9]-*.py"))
SKIPPED = 3


def main() -> int:
    rows, failed = [], 0
    for demo in DEMOS:
        t0 = time.perf_counter()
        proc = subprocess.run([sys.executable, str(demo)], cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        secs = time.perf_counter() - t0
        status = {0: "ok", SKIPPED: "skipped"}.get(proc.returncode, f"FAILED ({proc.returncode})")
        failed += status.startswith("FAILED")
        rows.append((demo.name, status, secs))
        print(f"{demo.name:<36} {status:<14} {secs:6.1f} s")
        if status != "ok":
            print((proc.stderr or proc.stdout)[-1500:])
    lines = ["# Demo run summary", "", "| demo | status | seconds |", "|---|---|---|"]
    lines += [f"| `{n}` | {s} | {t:.1f} |" for n, s, t in rows]
    out = ROOT / "samples" / "outputs"
    out.mkdir(parents=True, exist_ok=True)
    (out / "run-all.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
