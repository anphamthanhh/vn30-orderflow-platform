"""Run binaries of the Rust workspace from the demos (or report that the toolchain is missing)."""
from __future__ import annotations

import shutil
import subprocess

from .common import REPO_ROOT

NATIVE = REPO_ROOT / "native_platform"
SKIPPED = 3  # demo exit code meaning "prerequisite missing" (see demos/run-all.py)


def cargo_available() -> bool:
    return shutil.which("cargo") is not None


def cargo_run(package: str, binary: str, args: list[str], release: bool = False) -> str:
    """`cargo run -q -p <package> --bin <binary> -- <args>` in native_platform; returns stdout (LF)."""
    cmd = ["cargo", "run", "-q", "--locked", "-p", package, "--bin", binary]
    if release:
        cmd.insert(3, "--release")
    proc = subprocess.run(cmd + ["--", *args], cwd=NATIVE, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=1800)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed ({proc.returncode}):\n{proc.stderr[-3000:]}")
    return proc.stdout.replace("\r\n", "\n")


def cargo_example(package: str, example: str, args: list[str]) -> str:
    """`cargo run -q --release -p <package> --example <example> -- <args>` (for benchmarks)."""
    cmd = ["cargo", "run", "-q", "--release", "--locked", "-p", package, "--example", example, "--", *args]
    proc = subprocess.run(cmd, cwd=NATIVE, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=3600)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed ({proc.returncode}):\n{proc.stderr[-3000:]}")
    return proc.stdout.replace("\r\n", "\n")
