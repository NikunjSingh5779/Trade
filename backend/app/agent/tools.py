from typing import Any

import pandas as pd


def summarize_bars(bars: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministic market summary; it does not make trade decisions."""
    if not bars:
        return {"count": 0, "error": "No historical bars are available."}
    frame = pd.DataFrame(bars).sort_values("timestamp")
    close = pd.to_numeric(frame["close"], errors="coerce").dropna()
    if close.empty:
        return {"count": 0, "error": "No valid close prices are available."}
    latest = float(close.iloc[-1])
    return {
        "symbol": str(frame.iloc[-1]["symbol"]),
        "bar_count": int(len(frame)),
        "latest_timestamp": str(frame.iloc[-1]["timestamp"]),
        "latest_close": latest,
        "change_pct_over_window": (
            float((latest / close.iloc[0] - 1) * 100) if len(close) > 1 and close.iloc[0] else None
        ),
        "sma_20": float(close.tail(20).mean()),
        "volatility_std_pct": float(close.pct_change().dropna().std(ddof=0) * 100)
        if len(close) > 2 else None,
        "recent_closes": [round(float(v), 4) for v in close.tail(10).tolist()],
    }
