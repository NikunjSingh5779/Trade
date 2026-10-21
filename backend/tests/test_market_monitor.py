from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings
from app.market_data.monitor import MarketMonitor
from app.storage import Database


class FakeProvider:
    configured = True

    def __init__(self):
        self.calls = []

    async def fetch_bars(self, symbols, timeframe="5Min", limit=50, start_time=None):
        self.calls.append((list(symbols), timeframe, limit, start_time))
        now = datetime.now(UTC)
        bars = []
        for symbol in symbols:
            for index in range(12):
                if timeframe == "5Min":
                    stamp = now - timedelta(minutes=5 * (11 - index) + 1)
                    close = 101.0 + index * 0.01
                    high, low, open_price = close + 0.2, close - 0.2, close
                else:
                    stamp = now - timedelta(days=12 - index)
                    open_price, high, low, close = 100.0, 111.0 if index == 0 else 103.0, 99.0, 109.0 if index == 0 else 101.0
                bars.append({
                    "symbol": symbol,
                    "timestamp": stamp.isoformat().replace("+00:00", "Z"),
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": 1000,
                })
        return sorted(bars, key=lambda bar: (bar["timestamp"], bar["symbol"]))


class FakeFeeds:
    async def fetch_news(self, symbol=None, limit=20):
        return {"configured": False, "items": [], "errors": []}

    async def fetch_events(self, limit=20):
        return {"configured": False, "items": [], "errors": []}


def _setup(tmp_path):
    settings = Settings(
        _env_file=None,
        database_path=str(tmp_path / "monitor.db"),
        inference_lock_path=str(tmp_path / "infer.lock"),
        watchlist_symbols="AAPL",
        autonomous_paper_trading_enabled=False,
        market_scan_enabled=False,
        max_stale_data_seconds=600,
        news_rss_urls="",
        economic_events_rss_urls="",
    )
    db = Database(settings.database_path)
    db.initialize(["AAPL"])
    provider = FakeProvider()
    monitor = MarketMonitor(settings, db, provider, FakeFeeds())
    return db, provider, monitor


@pytest.mark.asyncio
async def test_monitor_fetches_a_backfill_window_for_intraday_exit_recovery(tmp_path):
    db, provider, monitor = _setup(tmp_path)

    result = await monitor.run_once()

    assert provider.calls[0] == (["AAPL"], "5Min", 1000, None)
    assert result["status"] == "queued"
    assert result["bar_count"] == 12


@pytest.mark.asyncio
async def test_swing_positions_use_daily_bars_and_exit_on_daily_crossing(tmp_path):
    db, provider, monitor = _setup(tmp_path)
    opened = (datetime.now(UTC) - timedelta(days=13)).isoformat()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO positions(id,proposal_id,symbol,side,quantity,entry_price,current_price,"
            "stop_loss,take_profit,status,opened_at,timeframe) "
            "VALUES (?,?,?,?,?,?,?,?,?,'open',?,?)",
            ("pos-swing", "prop-swing", "AAPL", "long", 10, 100, 100, 95, 110, opened, "swing"),
        )

    await monitor.run_once()

    assert len(provider.calls) == 2
    assert provider.calls[0][:3] == (["AAPL"], "5Min", 1000)
    assert provider.calls[0][3] is None
    assert provider.calls[1][:3] == (["AAPL"], "1Day", 10000)
    assert provider.calls[1][3] is not None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT status, current_price, realized_pnl FROM positions WHERE id='pos-swing'"
        ).fetchone()
    assert row["status"] == "closed"
    assert row["current_price"] == 110.0
    assert row["realized_pnl"] == 100.0
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM audit WHERE event='paper_position.auto_exit'"
        ).fetchone()
    assert row[0] == 1
