from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from app.config import Settings
from app.market_data.alpaca import AlpacaMarketData
from app.market_data.freshness import MarketDataFreshnessError, ensure_fresh_bars


@pytest.mark.asyncio
async def test_alpaca_fetches_symbols_independently_and_returns_chronological_bars():
    requested = []

    def respond(request):
        params = dict(request.url.params)
        requested.append(params)
        symbol = params["symbols"]
        assert params["sort"] == "desc"
        assert params["end"]
        return httpx.Response(200, json={
            "bars": {
                symbol: [
                    {"t": "2026-10-20T19:50:00Z", "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 20},
                    {"t": "2026-10-20T19:45:00Z", "o": 9, "h": 10, "l": 8, "c": 9.5, "v": 10},
                ]
            },
            "next_page_token": None,
        })

    settings = Settings(
        _env_file=None,
        market_data_api_key="test-key",
        market_data_api_secret="test-secret",
        local_model_base_urls="http://127.0.0.1:8080/v1",
    )
    with respx.mock(base_url=settings.market_data_base_url) as mock:
        mock.get("/v2/stocks/bars").mock(side_effect=respond)
        bars = await AlpacaMarketData(settings).fetch_bars(["AAPL", "MSFT", "AAPL"], limit=2)

    assert [item["symbols"] for item in requested] == ["AAPL", "MSFT"]
    assert len(bars) == 4
    assert [bar["timestamp"] for bar in bars] == sorted(bar["timestamp"] for bar in bars)
    assert bars[0]["timestamp"] == "2026-10-20T19:45:00Z"
    assert bars[-1]["timestamp"] == "2026-10-20T19:50:00Z"
    assert {bar["symbol"] for bar in bars} == {"AAPL", "MSFT"}


def test_intraday_stale_bars_are_rejected_and_fresh_bars_are_accepted():
    now = datetime(2026, 10, 20, 20, 0, tzinfo=UTC)
    fresh = [{"timestamp": (now - timedelta(minutes=2)).isoformat()}]
    stale = [{"timestamp": (now - timedelta(minutes=11)).isoformat()}]

    ensure_fresh_bars(fresh, "intraday", 600, now=now)
    with pytest.raises(MarketDataFreshnessError, match="stale"):
        ensure_fresh_bars(stale, "intraday", 600, now=now)


def test_future_market_timestamp_is_rejected():
    now = datetime(2026, 10, 20, 20, 0, tzinfo=UTC)
    future = [{"timestamp": (now + timedelta(minutes=3)).isoformat()}]
    with pytest.raises(MarketDataFreshnessError, match="future"):
        ensure_fresh_bars(future, "intraday", 600, now=now)
