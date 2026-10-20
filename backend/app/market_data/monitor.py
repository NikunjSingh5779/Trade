import asyncio
from typing import Any

from app.agent.tools import summarize_bars
from app.config import Settings
from app.market_data.alpaca import AlpacaMarketData
from app.market_data.sessions import current_scan_window, is_regular_us_session
from app.storage import Database


class MarketMonitor:
    def __init__(self, settings: Settings, db: Database, provider: AlpacaMarketData):
        self.settings = settings
        self.db = db
        self.provider = provider
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
        bars = await self.provider.fetch_bars(symbols)
        self.db.save_bars(bars, source="alpaca")
        summaries = []
        for symbol in symbols:
            recent = self.db.get_bars(symbol, limit=50)
            if recent:
                # Mark paper positions even when the AI queue is backed up.
                self.db.mark_position(symbol, float(recent[-1]["close"]))
                summaries.append(summarize_bars(recent))
        if not summaries:
            return {"status": "no_data", "symbols_requested": symbols}
        if self.db.active_queue_count() >= self.settings.ai_queue_max_pending:
            self.db.add_audit("market_scan.skipped", {
                "reason": "AI queue is full", "bars_received": len(bars),
            })
            return {"status": "skipped_queue_full", "bar_count": len(bars)}
        window = current_scan_window()
        job = self.db.create_job(
            "market_scan",
            {"market": "US equities", "scan_window": window, "summaries": summaries,
             "instruction": "Rank setups only using this evidence; no order execution."},
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
