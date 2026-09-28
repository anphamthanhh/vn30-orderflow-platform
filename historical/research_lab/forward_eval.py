"""Evaluate a registered card's forward ledger against the shared pre-registered gate.

Reads chained (v2) or legacy (v1) ledgers through ``forward_registry``. Below the card's minimum
sample (default 60 signals and 6 months of accrued sessions) the gate withholds statistics;
``--force-peek`` unblinds and logs a numbered touch in ``peek_log.jsonl``. More missing sessions
than the card's budget give ``BLOCKED_MISSING_DATA``. Results also carry the card's power figures.

    python historical/research_lab/forward_eval.py <ledger.jsonl> [--card N1]
        [--sessions forward/sessions.jsonl] [--scenario WORSE_SIDE|REALISTIC]
        [--force-peek]
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from historical.research_lab import forward_registry as fr  # noqa: E402

MIN_SIGNALS = 60
MIN_MONTHS = 6.0

# fallback for an unregistered ledger: the same gate, no power figures
_GENERIC = fr.CardSpec(
    card_id="?",
    family="?",
    source="m1_daily",
    ledger_name="",
    plan_fn=lambda *a, **k: [],
    required_warmup=0,
    effect_pts=0.0,
    sigma_pts=0.0,
    signals_per_year=0.0,
    min_signals=MIN_SIGNALS,
    min_months=MIN_MONTHS,
)


def load_ledger(path: Path | str, scenario=fr.WORSE_SIDE) -> pd.DataFrame:
    """Flatten a v2 chained or legacy v1 ledger into per-trade rows under one cost scenario."""
    return fr.ledger_frame(path, scenario)


def load_sessions(path: Path | str | None) -> list[dict] | None:
    if not path or not Path(path).exists():
        return None
    return fr.read_ledger(path)


def evaluate(
    ledger: pd.DataFrame,
    card: fr.CardSpec | str | None = None,
    sessions: list[dict] | None = None,
    force_peek: bool = False,
    peek_log: Path | str | None = None,
    by_scenario: dict | None = None,
) -> dict:
    """Apply the accrual gate; below the minimum sample only ``n`` and ``verdict`` are returned."""
    spec = card if isinstance(card, fr.CardSpec) else fr.CARDS_BY_ID.get(str(card), _GENERIC)
    return fr.gate(
        ledger,
        spec,
        sessions=sessions,
        force_peek=force_peek,
        peek_log=peek_log,
        by_scenario=by_scenario,
    )


def status(
    ledger: pd.DataFrame,
    card: fr.CardSpec | str | None = None,
    **kw,
) -> dict:
    """Return :func:`evaluate` plus the card's power figures."""
    spec = card if isinstance(card, fr.CardSpec) else fr.CARDS_BY_ID.get(str(card), _GENERIC)
    return fr.card_status(spec, evaluate(ledger, spec, **kw))


def scenario_stats(path: Path | str) -> dict:
    """Return ledger statistics under both scored cost scenarios, side by side."""
    out = {}
    for scn in fr.SCORED_SCENARIOS:
        df = fr.ledger_frame(path, scn)
        if len(df):
            out[fr._scn_name(scn)] = fr._stats(df)
    return out


def _print(res: dict, label: str = "") -> None:
    """Print a gate or status result, showing only the fields the gate exposes."""
    tag = label or res.get("card", "")
    if "effect_pts" in res:
        print(fr.format_status({**res, "card": tag or res.get("card", "?")}))
        return
    print(f"Forward evaluation [{tag}] — n={res.get('n', 0)} ==> {res.get('verdict')}")
    if res.get("verdict") == "BLOCKED_MISSING_DATA":
        print(
            f"  MISSING_DATA {res['missing_sessions']} > budget {res['missing_budget']}: "
            f"{', '.join(res['missing_dates'][:8])}"
        )
    elif "hit_rate" in res:
        print(
            f"  hit {res['hit_rate']:.0%} | net {res['total_net_pts']:+.2f}pt "
            f"({res['net_per_trade_pts']:+.3f}/trade)"
        )
    else:
        print(f"  (statistics withheld below the pre-registered n>={MIN_SIGNALS} gate)")


def main(argv: list[str]) -> int:
    if not argv:
        raise SystemExit(
            "usage: python forward_eval.py <ledger.jsonl> [--card C7] "
            "[--sessions sessions.jsonl] [--scenario WORSE_SIDE|REALISTIC] [--force-peek]"
        )
    path = argv[0]
    card = argv[argv.index("--card") + 1] if "--card" in argv else None
    if card is None and "--label" in argv:  # legacy alias for --card
        card = argv[argv.index("--label") + 1]
    sess = argv[argv.index("--sessions") + 1] if "--sessions" in argv else None
    scn_name = argv[argv.index("--scenario") + 1] if "--scenario" in argv else "WORSE_SIDE"
    scn = {fr._scn_name(s): s for s in fr.SCORED_SCENARIOS}.get(scn_name, fr.WORSE_SIDE)
    force = "--force-peek" in argv
    res = status(
        load_ledger(path, scn),
        card,
        sessions=load_sessions(sess),
        force_peek=force,
        peek_log=Path(path).parent / "peek_log.jsonl",
        by_scenario=scenario_stats(path) if force else None,
    )
    _print(res, card or "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
