"""Backtest metrics for VN30F futures; P&L in VND (points × ``VN30F_MULTIPLIER``).

Sharpe and Sortino use daily returns, not bar returns; the profit factor is net of fees.
"""

import pandas as pd
import numpy as np
from typing import Dict, Any, Optional
from datetime import datetime


VN30F_MULTIPLIER = 100000  # VND/point
COMMISSION_PER_SIDE = 0.15  # points/contract; flat estimate, not the cost model (see costs.py)
FEE_PER_TRADE = COMMISSION_PER_SIDE * 2 * VN30F_MULTIPLIER  # 30,000 VND


def calculate_daily_returns(equity_curve: pd.Series) -> pd.Series:
    """Return daily returns from the last equity value of each calendar day."""
    daily_equity = equity_curve.resample("D").last().dropna()

    daily_returns = daily_equity.pct_change().dropna()

    return daily_returns


def calculate_returns_per_bar(equity_curve: pd.Series) -> pd.Series:
    """Return per-bar returns of an equity curve."""
    if equity_curve is None or len(equity_curve) == 0:
        return pd.Series(dtype=float)

    returns = equity_curve.pct_change().dropna()
    return returns


def calculate_returns_per_day(equity_curve: pd.Series) -> pd.Series:
    """Return daily returns restricted to VN30F trading days."""
    if equity_curve is None or len(equity_curve) == 0:
        return pd.Series(dtype=float)

    daily_equity = equity_curve.resample("D").last().dropna()
    if len(daily_equity) == 0:
        return pd.Series(dtype=float)

    from .calendar_vn30f import is_trading_day

    daily_equity = daily_equity[daily_equity.index.map(is_trading_day)]
    returns = daily_equity.pct_change().dropna()
    return returns


def calculate_returns_per_trade(
    trades: pd.DataFrame,
    initial_cash: float,
) -> pd.Series:
    """Return per-trade P&L divided by ``initial_cash``, indexed by ``exit_time``.

    Uses the first of ``net_pnl_vnd``, ``Net_PnL_VND``, ``pnl_vnd``, ``PnL_VND`` present; returns
    an empty Series if none is, if ``exit_time`` is missing or if ``initial_cash <= 0``.
    """
    if trades is None or len(trades) == 0:
        return pd.Series(dtype=float)

    if "exit_time" not in trades.columns:
        return pd.Series(dtype=float)

    if "net_pnl_vnd" in trades.columns:
        pnl = trades["net_pnl_vnd"]
    elif "Net_PnL_VND" in trades.columns:
        pnl = trades["Net_PnL_VND"]
    elif "pnl_vnd" in trades.columns:
        pnl = trades["pnl_vnd"]
    elif "PnL_VND" in trades.columns:
        pnl = trades["PnL_VND"]
    else:
        return pd.Series(dtype=float)

    if initial_cash <= 0:
        return pd.Series(dtype=float)

    returns = pnl / float(initial_cash)
    returns.index = pd.to_datetime(trades["exit_time"])
    returns = returns.sort_index()
    return returns


def calculate_tail_metrics(returns: pd.Series) -> Dict[str, Any]:
    """Return moments, historical VaR/CVaR at 95 % and 99 %, and the tail ratio of ``returns``."""
    if returns is None or len(returns) == 0:
        return {
            "count": 0,
            "mean": 0.0,
            "std": 0.0,
            "skew": 0.0,
            "kurtosis": 0.0,
            "var_95": 0.0,
            "cvar_95": 0.0,
            "var_99": 0.0,
            "cvar_99": 0.0,
            "tail_ratio": 0.0,
        }

    series = returns.dropna()
    if len(series) == 0:
        return {
            "count": 0,
            "mean": 0.0,
            "std": 0.0,
            "skew": 0.0,
            "kurtosis": 0.0,
            "var_95": 0.0,
            "cvar_95": 0.0,
            "var_99": 0.0,
            "cvar_99": 0.0,
            "tail_ratio": 0.0,
        }

    values = series.values
    var_95 = float(np.percentile(values, 5))
    var_99 = float(np.percentile(values, 1))
    cvar_95 = float(values[values <= var_95].mean()) if len(values[values <= var_95]) > 0 else 0.0
    cvar_99 = float(values[values <= var_99].mean()) if len(values[values <= var_99]) > 0 else 0.0
    p95 = float(np.percentile(values, 95))
    p5 = float(np.percentile(values, 5))
    tail_ratio = abs(p95 / p5) if p5 != 0 else 0.0

    return {
        "count": int(len(series)),
        "mean": float(series.mean()),
        "std": float(series.std()) if series.std() == series.std() else 0.0,
        "skew": float(series.skew()) if series.skew() == series.skew() else 0.0,
        "kurtosis": float(series.kurtosis()) if series.kurtosis() == series.kurtosis() else 0.0,
        "var_95": var_95,
        "cvar_95": cvar_95,
        "var_99": var_99,
        "cvar_99": cvar_99,
        "tail_ratio": tail_ratio,
    }


def calculate_sharpe_ratio(
    equity_curve: pd.Series,
    risk_free_rate: float = 0.0,
    annualization_factor: float = 252,
) -> float:
    """Return the annualised Sharpe ratio from daily returns (0 without data or variance).

    ``(mean_daily - rf / N) / std_daily * sqrt(N)`` with ``N = annualization_factor`` and ``rf``
    the annual ``risk_free_rate``.
    """
    daily_returns = calculate_daily_returns(equity_curve)

    if len(daily_returns) == 0 or daily_returns.std() == 0:
        return 0.0

    excess_return = daily_returns.mean() - (risk_free_rate / annualization_factor)
    sharpe = (excess_return / daily_returns.std()) * np.sqrt(annualization_factor)

    return sharpe


def calculate_sortino_ratio(
    equity_curve: pd.Series,
    risk_free_rate: float = 0.0,
    annualization_factor: float = 252,
) -> float:
    """Return the annualised Sortino ratio from daily returns (std of negative days as downside).

    With no downside deviation it returns ``inf`` for a positive excess return, else 0.
    """
    daily_returns = calculate_daily_returns(equity_curve)

    if len(daily_returns) == 0:
        return 0.0

    excess_return = daily_returns.mean() - (risk_free_rate / annualization_factor)

    negative_returns = daily_returns[daily_returns < 0]
    if len(negative_returns) == 0:
        return np.inf if excess_return > 0 else 0.0

    downside_dev = negative_returns.std()

    if downside_dev == 0:
        return np.inf if excess_return > 0 else 0.0

    sortino = (excess_return / downside_dev) * np.sqrt(annualization_factor)

    return sortino


def calculate_profit_factor(
    trades: pd.DataFrame,
    pnl_column: str = "PnL_VND",
    fee_column: str = "Fee_VND",
    net: bool = True,
) -> float:
    """Return the profit factor: total winning P&L over the absolute total of losing P&L.

    With ``net`` and ``fee_column`` present, fees are deducted per trade first. Returns ``inf``
    when there are profits but no losses.
    """
    if trades is None or len(trades) == 0:
        return 0.0

    if net and fee_column in trades.columns:
        net_pnl = trades[pnl_column] - trades[fee_column]
    else:
        net_pnl = trades[pnl_column]

    gross_profit = net_pnl[net_pnl > 0].sum()
    gross_loss = abs(net_pnl[net_pnl < 0].sum())

    if gross_loss == 0:
        return np.inf if gross_profit > 0 else 0.0

    return gross_profit / gross_loss


def calculate_max_drawdown(equity_curve: pd.Series) -> Dict[str, Any]:
    """Return max and average drawdown (%) and the peak-to-trough duration of the max in days.

    ``max_drawdown_points`` is the max drawdown fraction times the first equity value.
    """
    if len(equity_curve) == 0:
        return {
            "max_drawdown_pct": 0.0,
            "max_drawdown_points": 0,
            "max_drawdown_duration_days": 0,
            "avg_drawdown_pct": 0.0,
        }

    running_max = equity_curve.expanding().max()

    drawdown = (equity_curve - running_max) / running_max

    max_dd = drawdown.min()
    max_dd_idx = drawdown.idxmin()

    peak_idx = equity_curve[:max_dd_idx].idxmax()

    if isinstance(peak_idx, datetime) and isinstance(max_dd_idx, datetime):
        dd_duration = (max_dd_idx - peak_idx).days
    else:
        dd_duration = 0

    avg_dd = drawdown[drawdown < 0].mean()

    return {
        "max_drawdown_pct": max_dd * 100,
        "max_drawdown_points": max_dd * equity_curve.iloc[0],
        "max_drawdown_duration_days": dd_duration,
        "avg_drawdown_pct": avg_dd * 100 if not pd.isna(avg_dd) else 0.0,
    }


def calculate_metrics(
    equity_curve: pd.Series,
    trades: pd.DataFrame,
    initial_cash: float = 100_000_000,
) -> Dict[str, Any]:
    """Return the backtest metric set for an equity curve and its trades (VND, percentages).

    Sharpe, Sortino and volatility use daily returns; net P&L and profit factor are after
    ``Fee_VND``. Gross return uses trade-level ``PnL_VND`` when present, else the equity curve.
    """
    final_equity = equity_curve.iloc[-1] if len(equity_curve) > 0 else initial_cash

    daily_returns = calculate_daily_returns(equity_curve)
    if len(daily_returns) > 0 and daily_returns.std() > 0:
        volatility = daily_returns.std() * np.sqrt(252) * 100
    else:
        volatility = 0

    sharpe = calculate_sharpe_ratio(equity_curve)
    sortino = calculate_sortino_ratio(equity_curve)

    dd_metrics = calculate_max_drawdown(equity_curve)

    num_trades = len(trades) if trades is not None else 0

    if num_trades > 0 and "PnL_VND" in trades.columns:
        wins = (trades["PnL_VND"] > 0).sum()
        win_rate = wins / num_trades * 100
    else:
        win_rate = 0

    total_fees = 0
    net_pnl = 0
    net_pf = 0

    if trades is not None and len(trades) > 0:
        if "Fee_VND" in trades.columns:
            total_fees = trades["Fee_VND"].sum()

        if "Net_PnL_VND" in trades.columns:
            net_pnl = trades["Net_PnL_VND"].sum()
            net_pf = calculate_profit_factor(trades, "PnL_VND", "Fee_VND", net=True)
        elif "PnL_VND" in trades.columns:
            net_pnl = trades["PnL_VND"].sum() - total_fees
            net_pf = calculate_profit_factor(trades, "PnL_VND", "Fee_VND", net=True)

    net_return = ((initial_cash + net_pnl) / initial_cash - 1.0) * 100

    # trade-level PnL_VND is exact for fixed-contract futures; a ReturnPct-based curve is not
    if trades is not None and len(trades) > 0 and "PnL_VND" in trades.columns:
        gross_pnl = trades["PnL_VND"].sum()
        total_return = gross_pnl / initial_cash * 100
        effective_final = initial_cash + gross_pnl
    else:
        total_return = (final_equity - initial_cash) / initial_cash * 100
        effective_final = final_equity

    if len(equity_curve) > 1:
        days = (equity_curve.index[-1] - equity_curve.index[0]).days
        if days > 0:
            annual_return = (effective_final / initial_cash) ** (365 / days) - 1
            annual_return_pct = annual_return * 100
        else:
            annual_return_pct = 0
    else:
        annual_return_pct = 0

    return {
        "total_return_pct": total_return,
        "net_return_pct": net_return,
        "annual_return_pct": annual_return_pct,
        "volatility_pct": volatility,
        "sharpe_ratio": sharpe,
        "sortino_ratio": sortino,
        "max_drawdown_pct": dd_metrics["max_drawdown_pct"],
        "max_drawdown_duration_days": dd_metrics["max_drawdown_duration_days"],
        "num_trades": num_trades,
        "win_rate_pct": win_rate,
        "total_fees_vnd": total_fees,
        "net_pnl_vnd": net_pnl,
        "net_profit_factor": net_pf,
        "final_equity": final_equity,
        "initial_cash": initial_cash,
    }


def format_metrics_report(metrics: Dict[str, Any]) -> str:
    """Format a :func:`calculate_metrics` result as a plain-text report."""
    lines = [
        "=" * 50,
        "BACKTEST METRICS REPORT",
        "=" * 50,
        "",
        "RETURN:",
        f"  Total Return:     {metrics.get('total_return_pct', 0):>10.2f}%",
        f"  Net Return:       {metrics.get('net_return_pct', 0):>10.2f}%  (after fees)",
        f"  Annual Return:    {metrics.get('annual_return_pct', 0):>10.2f}%",
        "",
        "RISK-ADJUSTED:",
        f"  Sharpe Ratio:     {metrics.get('sharpe_ratio', 0):>10.3f}",
        f"  Sortino Ratio:    {metrics.get('sortino_ratio', 0):>10.3f}",
        f"  Volatility:      {metrics.get('volatility_pct', 0):>10.2f}%",
        "",
        "DRAWDOWN:",
        f"  Max DD:          {metrics.get('max_drawdown_pct', 0):>10.2f}%",
        f"  Max DD Duration: {metrics.get('max_drawdown_duration_days', 0):>10.0f} days",
        "",
        "TRADING:",
        f"  # Trades:        {metrics.get('num_trades', 0):>10}",
        f"  Win Rate:        {metrics.get('win_rate_pct', 0):>10.2f}%",
        f"  Net PF:          {metrics.get('net_profit_factor', 0):>10.3f}",
        "",
        "FEES:",
        f"  Total Fees:      {metrics.get('total_fees_vnd', 0):>10,.0f} VND",
        f"  Net PnL:         {metrics.get('net_pnl_vnd', 0):>10,.0f} VND",
        "",
        "EQUITY:",
        f"  Initial:         {metrics.get('initial_cash', 0):>10,.0f} VND",
        f"  Final:           {metrics.get('final_equity', 0):>10,.0f} VND",
        "=" * 50,
    ]

    return "\n".join(lines)
