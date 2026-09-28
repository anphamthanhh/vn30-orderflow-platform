"""Backtest engine package; the metrics API is exported lazily on first attribute access."""

from importlib import import_module
from typing import Any, Dict, Tuple

_EXPORT_MAP: Dict[str, Tuple[str, str]] = {
    "calculate_metrics": ("historical.backtest_engine.metrics", "calculate_metrics"),
    "calculate_sharpe_ratio": ("historical.backtest_engine.metrics", "calculate_sharpe_ratio"),
    "calculate_sortino_ratio": ("historical.backtest_engine.metrics", "calculate_sortino_ratio"),
    "calculate_profit_factor": ("historical.backtest_engine.metrics", "calculate_profit_factor"),
    "calculate_max_drawdown": ("historical.backtest_engine.metrics", "calculate_max_drawdown"),
    "format_metrics_report": ("historical.backtest_engine.metrics", "format_metrics_report"),
    "VN30F_MULTIPLIER": ("historical.backtest_engine.metrics", "VN30F_MULTIPLIER"),
    "COMMISSION_PER_SIDE": ("historical.backtest_engine.metrics", "COMMISSION_PER_SIDE"),
    "FEE_PER_TRADE": ("historical.backtest_engine.metrics", "FEE_PER_TRADE"),
}

__all__ = list(_EXPORT_MAP.keys())


def __getattr__(name: str) -> Any:
    if name not in _EXPORT_MAP:
        raise AttributeError(
            f"module 'historical.backtest_engine' has no attribute '{name}'"
        )
    module_name, attr_name = _EXPORT_MAP[name]
    module = import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
