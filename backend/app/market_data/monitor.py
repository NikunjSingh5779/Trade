import asyncio
from typing import Any

from app.agent.tools import summarize_bars
from app.config import Settings
from app.market_data.alpaca import AlpacaMarketData
from app.market_data.feeds import ConfiguredFeedService
from app.market_data.freshness import MarketDataFreshnessError, ensure_fresh_bars
from app.market_data.sessions import current_scan_window, is_regular_us_session
from app.storage import Database


class MarketMonitor:
    def __init__(self, settings: Settings, db: Database, provider: AlpacaMarketData, feeds: ConfiguredFeedService):
        self.settings = settings
        self.db = db
        self.provider = provider
        self.feeds = feeds
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            if is_regular_us_session():
                try:
                    await self.run_once()
                except Exception as exc:
                    self.db.add_audit("market_scan.failed", {"error_type": type(exc).__name__})
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.market_scan_interval_seconds
                )
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    async def run_once(self) -> dict[str, Any]:
        symbols = self.db.list_watchlist()
        # Fetch a full five-session window so an app restart does not silently skip
        # stop/target crossings earlier in the current session.
        bars = await self.provider.fetch_bars(symbols, timeframe="5Min", limit=1000)
        self.db.save_bars(bars, source="alpaca")

        # Intraday positions are evaluated only against five-minute bars.
        for bar in bars:
            for exit_event in self.db.process_position_bar(bar, timeframe="intraday"):
                self.db.add_audit("paper_position.auto_exit", exit_event)

        # Swing positions use daily OHLC bars, not intraday candles. The historical
        # window allows the engine to catch up after a normal overnight shutdown.
        swing_symbols = self.db.list_open_position_symbols("swing")
        if swing_symbols:
            try:
                daily_bars = await self.provider.fetch_bars(
                    swing_symbols, timeframe="1Day", limit=100
                )
                self.db.save_bars(daily_bars, source="alpaca", timeframe="1Day")
                for bar in daily_bars:
                    for exit_event in self.db.process_position_bar(bar, timeframe="swing"):
                        self.db.add_audit("paper_position.auto_exit", exit_event)
            except Exception as exc:
                # A daily-data failure must be visible but should not discard successful
                # intraday bars or prevent the normal market scan from proceeding.
                self.db.add_audit("paper_position.swing_exit_scan_failed", {
                    "symbols": swing_symbols, "error_type": type(exc).__name__,
                })

        summaries = []
        for symbol in symbols:
            recent = self.db.get_bars(symbol, limit=50)
            if not recent:
                continue
            try:
                ensure_fresh_bars(
                    recent, "intraday", self.settings.max_stale_data_seconds
                )
            except MarketDataFreshnessError as exc:
                self.db.add_audit("market_scan.symbol_skipped_stale", {
                    "symbol": symbol, "reason": str(exc)[:200],
                })
                continue
            # Position marks are updated candle-by-candle above, after checking stop/target.
            summaries.append(summarize_bars(recent))
        if not summaries:
            return {
                "status": "no_fresh_data",
                "symbols_requested": symbols,
                "bar_count": len(bars),
            }
        if self.db.active_queue_count() >= self.settings.ai_queue_max_pending:
            self.db.add_audit("market_scan.skipped", {
                "reason": "AI queue is full", "bars_received": len(bars),
            })
            return {"status": "skipped_queue_full", "bar_count": len(bars)}
        window = current_scan_window()
        news = await self.feeds.fetch_news(limit=20)
        events = await self.feeds.fetch_events(limit=20)
        job = self.db.create_job(
            "market_scan",
            {"market": "US equities", "scan_window": window, "summaries": summaries,
             "news": news, "economic_events": events,
             "instruction": "Rank setups using the supplied market data and any configured feed evidence; no order execution."},
            dedupe_key=f"market-scan:{window}",
        )
        self.db.add_audit("market_scan.completed", {
            "symbols_requested": len(symbols), "bars_received": len(bars), "job_id": job["id"],
        })
        return {"status": "queued", "job_id": job["id"], "bar_count": len(bars)}

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.market_scan_enabled,
            "interval_seconds": self.settings.market_scan_interval_seconds,
            "provider_configured": self.provider.configured,
            "regular_us_session": is_regular_us_session(),
        }
