"""Demo 07 - one-way loopback latency of the client frame, measured two ways.

Runs ``native_platform/crates/client-proto/examples/loopback_latency.rs`` (release build):
  paced : one frame at a time, so each sample is a frame's one-way trip (writer stamp -> typed decode)
  flood : back-to-back frames; also reports per-frame read+decode time, which is not a one-way latency
All numbers are loopback (127.0.0.1, one process, one clock), not network or exchange latency.

    python demos/07-loopback-latency.py      # needs the Rust toolchain (cargo); ~1-2 min
"""
from __future__ import annotations

import platform
import re
import sys

from lib.cargo import SKIPPED, cargo_available, cargo_example
from lib.common import md_table, out_dir

OUT = out_dir("07-loopback-latency")
RUNS = (("paced", ["--mode", "paced", "--frames", "2000", "--warmup", "200", "--interval-us", "1000"]),
        ("flood", ["--mode", "flood", "--frames", "20000", "--warmup", "2000"]))
LINE = re.compile(r"^(?P<name>\S+)\s+p50=(?P<p50>[\d.]+) p90=(?P<p90>[\d.]+) p99=(?P<p99>[\d.]+) "
                  r"p99\.9=(?P<p999>[\d.]+) max=(?P<max>[\d.]+) mean=(?P<mean>[\d.]+)")


def main() -> int:
    if not cargo_available():
        print("cargo not found: this demo needs the Rust toolchain (see native_platform/README.md)")
        return SKIPPED
    rows, raw = [], []
    for mode, args in RUNS:
        out = cargo_example("client-proto", "loopback_latency", args)
        raw.append(f"$ --mode {mode}\n{out.strip()}")
        for line in out.splitlines():
            m = LINE.match(line.strip())
            if m:
                rows.append({"mode": mode, "series": m["name"], "p50 µs": m["p50"], "p90 µs": m["p90"],
                             "p99 µs": m["p99"], "p99.9 µs": m["p999"], "max µs": m["max"], "mean µs": m["mean"]})
    report = (
        "# Demo 07 - loopback latency of the client frame\n\n"
        f"Machine: {platform.system()} {platform.release()}, {platform.processor() or platform.machine()}. "
        "All figures are **loopback** (127.0.0.1, one process, one monotonic clock).\n\n"
        + md_table(rows, list(rows[0])) +
        "\nHow to read it: the *paced* one-way series is the time a single frame takes from the writer's "
        "stamp to a typed decode on the reader. Under *flood*, frames queue behind each other; the "
        "per-frame read+decode series there is small because the next frame is usually already "
        "buffered — that is the number an older loopback harness reported as p99 ≈ 125 µs. The paced interval "
        "is bounded below by the OS timer resolution; either way it is realistic spacing for futures ticks.\n\n"
        "```text\n" + "\n\n".join(raw) + "\n```\n"
    )
    (OUT / "report.md").write_text(report, encoding="utf-8", newline="\n")
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
