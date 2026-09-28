"""Signal evaluation before any backtest: information coefficient, decay, turnover and cost.

Follows the Alphalens factor-evaluation approach, with trading cost expressed in Sharpe units
so that it can be set against the signal directly. Entry points:
:func:`~historical.signal_eval.metrics.evaluate` (every metric, with input fingerprints) and
:func:`~historical.signal_eval.causal.assert_causal` (lookahead detector). Fees come from
``historical/backtest_engine/costs.py``; this package defines none.
"""

from __future__ import annotations

from historical.signal_eval.causal import (
    CausalReport,
    LookaheadError,
    assert_causal,
    check_causal,
)
from historical.signal_eval.metrics import (
    cost_in_sharpe_units,
    evaluate,
    forward_returns,
    ic_decay,
    information_coefficient,
    information_ratio,
    quantile_returns,
    turnover_per_year,
)

__all__ = [
    "CausalReport",
    "LookaheadError",
    "assert_causal",
    "check_causal",
    "cost_in_sharpe_units",
    "evaluate",
    "forward_returns",
    "ic_decay",
    "information_coefficient",
    "information_ratio",
    "quantile_returns",
    "turnover_per_year",
]
