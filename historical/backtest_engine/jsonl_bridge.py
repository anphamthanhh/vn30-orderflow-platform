"""Load Rust ``backtest-engine`` JSONL output and score it with the Python metrics stack.

The Rust ``FillSim`` already deducts fees, so metrics use ``Fee_VND = 0`` (ADR 0008).

Trade record schema (one JSON object per line):
    {"ts":<unix_s>,"side":"long|short","action":"enter|exit","qty":i,
     "price":f,"fee_vnd":f,"pnl_points":f?,"pnl_vnd":f?,"reason":s}
Equity record schema:
    {"ts":<unix_s>,"equity":f,"position":i,"unrealized":f,"realized":f}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Mapping, Union

import pandas as pd

from historical.backtest_engine.metrics import calculate_metrics

# Rust timestamps are unix seconds; shifting to ICT makes resample("D") group by Vietnam day
ICT_OFFSET_SECONDS = 7 * 3600

PathLike = Union[str, Path]


def _read_jsonl(path: PathLike) -> list[dict]:
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_equity(path: PathLike) -> pd.Series:
    """Load ``equity.jsonl`` as an equity Series indexed by naive ICT datetimes."""
    rows = _read_jsonl(path)
    if not rows:
        # empty but datetime-indexed, so a downstream resample("D") still works
        return pd.Series(dtype=float, index=pd.DatetimeIndex([]), name="equity")
    # naive ICT wall-clock index: calculate_metrics resamples by day without tz handling
    idx = pd.to_datetime([r["ts"] + ICT_OFFSET_SECONDS for r in rows], unit="s")
    return pd.Series([float(r["equity"]) for r in rows], index=idx, name="equity")


def load_trades(path: PathLike) -> pd.DataFrame:
    """Load the ``exit`` rows of ``trades.jsonl`` as one metrics-ready row per closed trade.

    ``PnL_VND`` is the Rust net P&L; ``Fee_VND`` is 0 so fees are not deducted twice.
    """
    rows = [r for r in _read_jsonl(path) if r.get("action") == "exit"]
    if not rows:
        return pd.DataFrame(columns=["PnL_VND", "Fee_VND"])
    df = pd.DataFrame(rows)
    df["PnL_VND"] = df["pnl_vnd"].astype(float)
    df["Fee_VND"] = 0.0
    return df


def evaluate_run(run_dir: PathLike, initial_cash: float = 100_000_000) -> Dict:
    """Compute metrics for a run directory holding ``trades.jsonl`` and ``equity.jsonl``."""
    run_dir = Path(run_dir)
    equity = load_equity(run_dir / "equity.jsonl")
    trades = load_trades(run_dir / "trades.jsonl")
    return calculate_metrics(equity, trades, initial_cash=initial_cash)


# columns of the comparison report, in order
_REPORT_COLUMNS = [
    "num_trades",
    "sharpe_ratio",
    "sortino_ratio",
    "max_drawdown_pct",
    "win_rate_pct",
    "net_profit_factor",
    "net_pnl_vnd",
    "total_return_pct",
]


def compare_runs(
    runs: Mapping[str, PathLike], initial_cash: float = 100_000_000
) -> pd.DataFrame:
    """Return a metrics table with one row per run; ``runs`` maps strategy name to run directory."""
    data = {name: evaluate_run(d, initial_cash) for name, d in runs.items()}
    df = pd.DataFrame(data).T
    return df[[c for c in _REPORT_COLUMNS if c in df.columns]]


def render_report(comparison: pd.DataFrame) -> str:
    """Render a :func:`compare_runs` table as a Markdown report."""
    rounded = comparison.round(4)
    try:
        table = rounded.to_markdown()  # needs `tabulate`
    except ImportError:
        table = rounded.to_string()  # fallback: plain fixed-width
    lines = [
        "# Backtest Comparison",
        "",
        "P&L net of real DNSE fees (Rust FillSim single-source). "
        "Metrics via `historical/backtest_engine/metrics.py`.",
        "",
        table,
        "",
    ]
    return "\n".join(lines)
