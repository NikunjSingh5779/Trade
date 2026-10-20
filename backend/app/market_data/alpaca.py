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

    async def fetch_bars(
        self, symbols: list[str], timeframe: str = "5Min", limit: int = 50
    ) -> list[dict[str, Any]]:
        if not self.configured:
            raise MarketDataUnavailable(
                "Market data is not configured. Set MARKET_DATA_API_KEY and MARKET_DATA_API_SECRET."
            )

        requested = list(dict.fromkeys(
            symbol.strip().upper()
            for symbol in symbols
            if isinstance(symbol, str) and symbol.strip()
        ))
        if not requested:
            return []
        per_symbol_limit = max(1, min(int(limit), 10000))
        end = datetime.now(UTC)
        start = end - timedelta(days=5)
        headers = {
            "APCA-API-KEY-ID": self.settings.market_data_api_key,
            "APCA-API-SECRET-KEY": self.settings.market_data_api_secret,
        }
        bars: list[dict[str, Any]] = []

        # Alpaca's multi-symbol historical endpoint pages by symbol first. A small
        # global limit can therefore return only one symbol and/or older bars.
        # Fetch symbols independently so every watchlist entry gets its own limit.
        async with httpx.AsyncClient(timeout=20) as client:
            for symbol in requested:
                params = {
                    "symbols": symbol,
                    "timeframe": timeframe,
                    "limit": per_symbol_limit,
                    "start": start.isoformat().replace("+00:00", "Z"),
                    "end": end.isoformat().replace("+00:00", "Z"),
                    "feed": self.settings.market_data_feed,
                    "adjustment": "raw",
                    "sort": "desc",
                }
                response = await client.get(
                    f"{self.settings.market_data_base_url.rstrip('/')}/v2/stocks/bars",
                    params=params,
                    headers=headers,
                )
                response.raise_for_status()
                body = response.json()
                rows = body.get("bars", {}).get(symbol, []) if isinstance(body, dict) else []
                for row in rows:
                    bars.append({
                        "symbol": symbol,
                        "timestamp": str(row["t"]),
                        "open": float(row["o"]),
                        "high": float(row["h"]),
                        "low": float(row["l"]),
                        "close": float(row["c"]),
                        "volume": float(row.get("v", 0)),
                    })

        # Return bars in chronological order for indicators and storage. Sorting
        # locally also protects consumers from provider response ordering changes.
        bars.sort(key=lambda row: (row["timestamp"], row["symbol"]))
        return bars
