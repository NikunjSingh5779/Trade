import asyncio
from datetime import UTC, datetime
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

        # Process exit candles from each position's durable checkpoint (or entry time).
        # This catches up across longer shutdowns and paginates through all missing bars.
        # Intraday positions use 5-minute bars; swing positions use daily bars.
        exit_monitor_warnings: list[dict[str, str]] = []
        for position_horizon, provider_timeframe in (
            ("intraday", "5Min"),
            ("swing", "1Day"),
        ):
            checkpoints = self.db.list_open_position_backfill_starts(position_horizon)
            for checkpoint in checkpoints:
                symbol = checkpoint["symbol"]
                try:
                    start_time = datetime.fromisoformat(
                        checkpoint["start_time"].replace("Z", "+00:00")
                    )
                    if start_time.tzinfo is None:
                        start_time = start_time.replace(tzinfo=UTC)
                    exit_bars = await self.provider.fetch_bars(
                        [symbol], timeframe=provider_timeframe, limit=10000,
                        start_time=start_time,
                    )
                    self.db.save_bars(
                        exit_bars, source="alpaca", timeframe=provider_timeframe
                    )
                    for bar in exit_bars:
                        for exit_event in self.db.process_position_bar(
                            bar, timeframe=position_horizon
                        ):
                            self.db.add_audit("paper_position.auto_exit", exit_event)
                except Exception as exc:
                    warning = {
                        "symbol": symbol,
                        "timeframe": position_horizon,
                        "error_type": type(exc).__name__,
                    }
                    exit_monitor_warnings.append(warning)
                    self.db.add_audit("paper_position.exit_backfill_failed", warning)

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
                "exit_monitor_warnings": exit_monitor_warnings,
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
        return {
            "status": "queued", "job_id": job["id"], "bar_count": len(bars),
            "exit_monitor_warnings": exit_monitor_warnings,
        }

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.market_scan_enabled,
            "interval_seconds": self.settings.market_scan_interval_seconds,
            "provider_configured": self.provider.configured,
            "regular_us_session": is_regular_us_session(),
        }
