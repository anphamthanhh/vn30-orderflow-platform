"""Evaluate the X1 basis forward ledger against its pre-registered criteria.

The ledger holds one JSON line per D+1 trade (``date_D1``, ``gross_pts``, ``fee_pts``,
``net_pts``; net includes worse-side slippage). A verdict needs at least 60 signals and 6 months
of accrued sessions; until then only ``ACCRUING`` is reported. Pass criteria: gross per trade
above the worse-side cost (fee + 2 x 0.5 pt), hit rate above 52 % and positive total net.

    python historical/research_lab/basis_eval.py <ledger.jsonl>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from historical.research_lab import forward_registry as fr  # noqa: E402

# pre-registered X1 forward criteria
HIT_MIN = 0.52
WORSE_SIDE_PTS = 0.5  # worse-side slippage per fill in points; two fills per round trip
MIN_SIGNALS = 60
MIN_MONTHS = 6.0


def load_ledger(path: Path | str) -> pd.DataFrame:
    rows = [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
    return pd.DataFrame(rows)


def _span_months(ledger: pd.DataFrame, sessions: list[dict] | None = None) -> float:
    """Return accrued sessions / 21 via ``forward_registry.accrued_months`` (not calendar span)."""
    return fr.accrued_months(ledger, sessions)


def evaluate(
    ledger: pd.DataFrame,
    sessions: list[dict] | None = None,
    force_peek: bool = False,
    peek_log=None,
) -> dict:
    """Return X1 forward metrics, per-criterion flags and a verdict.

    Below the minimum sample only ``n_signals``, ``n`` and ``verdict="ACCRUING"`` are returned;
    too many missing forward sessions give ``BLOCKED_MISSING_DATA``. ``force_peek=True``
    unblinds and, with ``peek_log``, records a numbered touch.
    """
    card = fr.CARDS_BY_ID["X1"]
    n = int(len(ledger))
    health = (
        fr.session_health(sessions, since=card.cutoff_ict_date) if sessions else None
    )
    if health and health["missing_data"] > card.missing_data_budget:
        return {
            "n_signals": n,
            "n": n,
            "verdict": "BLOCKED_MISSING_DATA",
            "missing_sessions": health["missing_data"],
            "missing_dates": health["missing_dates"],
            "missing_budget": card.missing_data_budget,
        }
    if n == 0:
        return {"n_signals": 0, "n": 0, "verdict": "ACCRUING"}

    net = ledger["net_pts"].astype(float)
    gross = ledger["gross_pts"].astype(float)
    fee = ledger["fee_pts"].astype(float)
    hit = float((net > 0).mean())
    total_net = float(net.sum())
    months = _span_months(ledger, sessions)
    if not (n >= MIN_SIGNALS and months >= MIN_MONTHS) and not force_peek:
        return {"n_signals": n, "n": n, "verdict": "ACCRUING"}

    gross_per_trade = float(gross.mean())
    fee_med = float(fee.median())
    worse_side_cost = fee_med + 2.0 * WORSE_SIDE_PTS  # the go-live cost bar
    # criteria: gross clears the worse-side bar, hit rate > 52 %, net (already worse-side) > 0
    c1 = gross_per_trade > worse_side_cost
    c2 = hit > HIT_MIN
    c3 = total_net > 0
    crits = {
        "1_gross_per_trade>worse_side_cost": bool(c1),
        "2_hit>52%": bool(c2),
        "3_net_positive_after_worse_side": bool(c3),
    }

    enough = n >= MIN_SIGNALS and months >= MIN_MONTHS
    verdict = "ACCRUING" if not enough else ("FORWARD-CONFIRMED" if all(crits.values()) else "KILL")
    res = {
        "n_signals": n,
        "n": n,
        "months": round(months, 1),
        "hit_rate": round(hit, 3),
        "total_net_pts": round(total_net, 2),
        "gross_per_trade_pts": round(gross_per_trade, 3),
        "fee_pts": round(fee_med, 3),
        "worse_side_cost_pts": round(worse_side_cost, 3),
        "criteria": crits,
        "enough_data": bool(enough),
        "verdict": verdict,
    }
    if force_peek:
        res["unblinded_peek"] = True
        if peek_log is not None:
            res["peek_touch"] = fr.log_peek(peek_log, "X1", n, reason="basis_eval --force-peek")
    return res


def _print(res: dict) -> None:
    print(f"X1 basis forward evaluation — {res['n_signals']} signals")
    if res.get("verdict") == "BLOCKED_MISSING_DATA":
        print(
            f"  ==> BLOCKED_MISSING_DATA — {res['missing_sessions']} missing forward "
            f"session(s) > budget {res['missing_budget']}"
        )
        return
    if "hit_rate" not in res:
        print(
            "  (statistics withheld below the pre-registered gate: "
            f"need >={MIN_SIGNALS} signals AND >={MIN_MONTHS:.0f}mo of ACCRUED sessions)"
        )
        print(f"  ==> {res['verdict']}")
        print(fr.format_status(fr.card_status(fr.CARDS_BY_ID['X1'], res)))
        return
    print(
        f"  span {res['months']}mo | hit {res['hit_rate']:.0%} | net {res['total_net_pts']:+.2f}pt"
    )
    print(
        f"  gross/trade {res['gross_per_trade_pts']}pt vs worse-side cost "
        f"{res['worse_side_cost_pts']}pt (fee {res['fee_pts']}pt)"
    )
    print("  FROZEN CRITERIA:")
    for k, v in res["criteria"].items():
        print(f"    [{'PASS' if v else 'FAIL'}] {k}")
    if not res["enough_data"]:
        print(f"  (need >={MIN_SIGNALS} signals AND >={MIN_MONTHS:.0f}mo before a verdict)")
    print(f"  ==> {res['verdict']}")


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        raise SystemExit("usage: python basis_eval.py <ledger.jsonl>")
    _print(evaluate(load_ledger(argv[0])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
