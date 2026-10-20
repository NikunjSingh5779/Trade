from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.config import Settings


class MarketDataUnavailable(RuntimeError):
    pass


class AlpacaMarketData:
    """Market-data-only adapter. It has no trading/order methods."""

    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool(self.settings.market_data_api_key and self.settings.market_data_api_secret)

    async def fetch_bars(self, symbols: list[str], timeframe: str = "5Min", limit: int = 50) -> list[dict[str, Any]]:
        if not self.configured:
            raise MarketDataUnavailable(
                "Market data is not configured. Set MARKET_DATA_API_KEY and MARKET_DATA_API_SECRET."
            )
        params = {
            "symbols": ",".join(symbols),
            "timeframe": timeframe,
            "limit": limit,
            "start": (datetime.now(UTC) - timedelta(days=5)).isoformat().replace("+00:00", "Z"),
            "feed": self.settings.market_data_feed,
            "adjustment": "raw",
        }
        headers = {
            "APCA-API-KEY-ID": self.settings.market_data_api_key,
            "APCA-API-SECRET-KEY": self.settings.market_data_api_secret,
        }
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{self.settings.market_data_base_url.rstrip('/')}/v2/stocks/bars",
                params=params,
                headers=headers,
            )
            response.raise_for_status()
            body = response.json()
        bars: list[dict[str, Any]] = []
        for symbol, rows in body.get("bars", {}).items():
            for row in rows:
                bars.append({
                    "symbol": symbol.upper(),
                    "timestamp": str(row["t"]),
                    "open": float(row["o"]),
                    "high": float(row["h"]),
                    "low": float(row["l"]),
                    "close": float(row["c"]),
                    "volume": float(row.get("v", 0)),
                })
        bars.sort(key=lambda row: (row["timestamp"], row["symbol"]))
        return bars
