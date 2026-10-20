from typing import Any

import pandas as pd


def evaluate_simple_moving_average(
    bars: list[dict[str, Any]], fast_window: int = 5, slow_window: int = 20,
    cost_bps: float = 5.0,
) -> dict[str, Any]:
    """Simple long/flat demonstration backtest with one-bar signal delay and costs.

    This is an educational baseline, not a recommendation. Signals use close prices
    and are applied to the next bar's return to avoid look-ahead in the basic loop.
    """
    if fast_window < 2 or slow_window <= fast_window:
        raise ValueError("Require 2 <= fast_window < slow_window.")
    if len(bars) < slow_window + 2:
        raise ValueError("Not enough bars for the requested windows.")
    frame = pd.DataFrame(bars).sort_values("timestamp")
    close = pd.to_numeric(frame["close"], errors="coerce")
    if close.isna().any() or (close <= 0).any():
        raise ValueError("Bars must contain positive numeric close prices.")
    fast = close.rolling(fast_window).mean()
    slow = close.rolling(slow_window).mean()
    signal = (fast > slow).astype(int).shift(1).fillna(0)
    returns = close.pct_change().fillna(0)
    turnover = signal.diff().abs().fillna(signal.abs())
    strategy_returns = signal * returns - turnover * (cost_bps / 10000)
    equity = (1 + strategy_returns).cumprod()
    peak = equity.cummax()
    drawdown = equity / peak - 1
    return {
        "bars": int(len(frame)),
        "fast_window": fast_window,
        "slow_window": slow_window,
        "cost_bps_per_turnover": cost_bps,
        "total_return_pct": float((equity.iloc[-1] - 1) * 100),
        "max_drawdown_pct": float(drawdown.min() * 100),
        "observations": int(strategy_returns.count()),
        "warning": "Simplified long/flat baseline; validate out-of-sample and account for spread/slippage.",
    }
