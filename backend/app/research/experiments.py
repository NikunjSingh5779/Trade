from typing import Any

from app.backtesting.engine import evaluate_simple_moving_average
from app.storage import Database


def run_sma_experiment(db: Database, symbol: str, fast: int = 5, slow: int = 20) -> dict[str, Any]:
    bars = db.get_bars(symbol, limit=500)
    metrics = evaluate_simple_moving_average(bars, fast_window=fast, slow_window=slow)
    return {
        "symbol": symbol.upper(),
        "metrics": metrics,
        "status": "research_only",
        "active_strategy_changed": False,
    }
