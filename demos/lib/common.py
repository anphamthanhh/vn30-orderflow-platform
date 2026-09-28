"""Shared plumbing for the demo scripts: repo paths, output folders, figure style."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
# Reports contain symbols such as "≈" and "µs"; a Windows console defaults to a legacy code page.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUTPUTS = REPO_ROOT / "samples" / "outputs"
SYNTHETIC_NOTE = "SYNTHETIC DATA - illustrates the mechanics, not evidence about the real market"


def out_dir(demo: str) -> Path:
    """Folder for one demo's committed artefacts (created on demand)."""
    d = OUTPUTS / demo
    d.mkdir(parents=True, exist_ok=True)
    return d


def pyplot():
    """Matplotlib in headless mode with a small, consistent style."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({
        "figure.dpi": 110, "savefig.dpi": 110, "axes.grid": True, "grid.alpha": 0.25,
        "axes.spines.top": False, "axes.spines.right": False, "font.size": 9,
    })
    return plt


def stamp(fig) -> None:
    """Put the synthetic-data disclaimer on every figure."""
    fig.text(0.01, 0.005, SYNTHETIC_NOTE, fontsize=7, color="#888888", ha="left", va="bottom")


def md_table(rows: list[dict], cols: list[str]) -> str:
    """Render a list of dicts as a GitHub/GitLab markdown table."""
    head = "| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n"
    body = "".join("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |\n" for r in rows)
    return head + body
