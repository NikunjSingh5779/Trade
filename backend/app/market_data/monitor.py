import asyncio
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.agent.tools import summarize_bars
from app.config import Settings
from app.market_data.alpaca import AlpacaMarketData
from app.market_data.feeds import ConfiguredFeedService
from app.market_data.freshness import MarketDataFreshnessError, ensure_fresh_bars
from app.market_data.sessions import current_scan_window, is_regular_us_bar, is_regular_us_session
from app.storage import Database


_US_EASTERN = ZoneInfo("America/New_York")


def is_completed_market_bar(
    bar: dict[str, Any], position_horizon: str, now: datetime | None = None
) -> bool:
    """Fail closed on unfinished candles so checkpoints never freeze a forming bar."""
    try:
        stamp = datetime.fromisoformat(str(bar["timestamp"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    stamp = stamp.astimezone(UTC)
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    current = current.astimezone(UTC)

    if position_horizon == "intraday":
        # Alpaca timestamps five-minute bars at their start.
        return stamp <= current - timedelta(minutes=5)
    if position_horizon == "swing":
        bar_date = stamp.astimezone(_US_EASTERN).date()
        now_eastern = current.astimezone(_US_EASTERN)
        return (
            bar_date < now_eastern.date()
            or (bar_date == now_eastern.date() and now_eastern.time() >= time(16, 10))
        )
    return False


class MarketMonitor:
    def __init__(self, settings: Settings, db: Database, provider: AlpacaMarketData, feeds: ConfiguredFeedService):
        self.settings = settings
        self.db = db
        self.provider = provider
        self.feeds = feeds
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                if is_regular_us_session():
                    await self.run_once()
                else:
                    # End-of-session liquidation must run after 16:00 ET as well as
                    # on startup after a missed session. It never creates new positions.
                    await self.close_intraday_positions_after_session()
            except Exception as exc:
                self.db.add_audit("market_scan.failed", {"error_type": type(exc).__name__})
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=min(self.settings.market_scan_interval_seconds, 60)
                )
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    async def close_intraday_positions_after_session(
        self, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Close overdue intraday paper positions using the last available regular-session mark.

        The routine also catches positions left open because the app was offline at 16:00 ET.
        It backfills completed regular-session candles before flattening any position that remains.
        """
        if not self.settings.autonomous_paper_trading_enabled:
            return []
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        current = current.astimezone(UTC)
        eastern_now = current.astimezone(_US_EASTERN)
        portfolio = self.db.portfolio_snapshot(self.settings.paper_initial_equity)
        closed: list[dict[str, Any]] = []

        for position in portfolio["open_positions"]:
            if position.get("timeframe", "intraday") != "intraday":
                continue
            try:
                opened_at = datetime.fromisoformat(
                    str(position["opened_at"]).replace("Z", "+00:00")
                )
                if opened_at.tzinfo is None:
                    opened_at = opened_at.replace(tzinfo=UTC)
                opened_local = opened_at.astimezone(_US_EASTERN)
            except (KeyError, TypeError, ValueError):
                self.db.add_audit("paper_position.session_close_skipped", {
                    "position_id": position.get("id"), "reason": "invalid_open_timestamp",
                })
                continue

            # A position opened today stays open during regular hours. Any intraday
            # position from an earlier date is overdue and is flattened on recovery.
            due = (
                opened_local.date() < eastern_now.date()
                or (
                    eastern_now.weekday() < 5
                    and eastern_now.time().replace(tzinfo=None) >= time(16, 0)
                    and opened_local.date() == eastern_now.date()
                )
            )
            if not due:
                continue

            checkpoint = position.get("last_processed_bar_at") or position["opened_at"]
            try:
                start_time = datetime.fromisoformat(str(checkpoint).replace("Z", "+00:00"))
                if start_time.tzinfo is None:
                    start_time = start_time.replace(tzinfo=UTC)
                final_bars = await self.provider.fetch_bars(
                    [position["symbol"]], timeframe="5Min", limit=10000,
                    start_time=start_time,
                )
                self.db.save_bars(final_bars, source="alpaca", timeframe="5Min")
                for bar in final_bars:
                    if not is_completed_market_bar(bar, "intraday", current):
                        continue
                    try:
                        stamp = datetime.fromisoformat(
                            str(bar["timestamp"]).replace("Z", "+00:00")
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=UTC)
                    if not is_regular_us_bar(bar):
                        continue
                    for event in self.db.process_position_bar(bar, timeframe="intraday"):
                        self.db.add_audit("paper_position.auto_exit", event)
            except Exception as exc:
                # Still flatten overdue paper positions with the last server-recorded mark;
                # the audit trail makes any failed final-bar refresh explicit.
                self.db.add_audit("paper_position.session_close_mark_refresh_failed", {
                    "position_id": position["id"],
                    "symbol": position["symbol"],
                    "error_type": type(exc).__name__,
                })

            try:
                result = self.db.close_position(position["id"], None)
                event = {
                    **result,
                    "symbol": position["symbol"],
                    "reason": "us_regular_session_close",
                    "mark_source": result.get("pricing_method"),
                    "closed_at_check": current.isoformat(),
                    "mode": "paper",
                }
                self.db.add_audit("paper_position.session_close", event)
                closed.append(event)
            except (KeyError, ValueError) as exc:
                self.db.add_audit("paper_position.session_close_skipped", {
                    "position_id": position["id"], "error_type": type(exc).__name__,
                })
        return closed

    async def run_once(self) -> dict[str, Any]:
        symbols = (
            self.settings.autonomous_symbol_list
            if self.settings.autonomous_paper_trading_enabled
            else self.db.list_watchlist()
        )
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
                        if not is_completed_market_bar(bar, position_horizon):
                            continue
                        if position_horizon == "intraday" and not is_regular_us_bar(bar):
                            continue
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
        bars_by_symbol: dict[str, list[dict[str, Any]]] = {}
        for symbol in symbols:
            recent = self.db.get_bars(symbol, limit=100)
            completed = [
                bar for bar in recent
                if is_completed_market_bar(bar, "intraday") and is_regular_us_bar(bar)
            ]
            if len(completed) < 10:
                self.db.add_audit("market_scan.symbol_skipped_insufficient_completed_bars", {
                    "symbol": symbol, "completed_bar_count": len(completed),
                })
                continue
            recent_completed = completed[-30:]
            try:
                ensure_fresh_bars(
                    recent_completed, "intraday", self.settings.max_stale_data_seconds
                )
            except MarketDataFreshnessError as exc:
                self.db.add_audit("market_scan.symbol_skipped_stale", {
                    "symbol": symbol, "reason": str(exc)[:200],
                })
                continue
            # Decisions use only completed candles. Never treat an unfinished bar as confirmed data.
            summaries.append(summarize_bars(recent_completed))
            bars_by_symbol[symbol] = recent_completed
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
            {
                "market": "US equities",
                "scan_window": window,
                "summaries": summaries,
                "bars_by_symbol": bars_by_symbol,
                "symbols": list(bars_by_symbol),
                "autonomous_mode": self.settings.autonomous_paper_trading_enabled,
                "news": news,
                "economic_events": events,
                "instruction": (
                    "For autonomous mode, choose long, short, or no_trade per symbol based only on the supplied data. "
                    "Every potential entry will still be checked by the server's risk controls; no live orders."
                    if self.settings.autonomous_paper_trading_enabled
                    else "Rank setups using the supplied market data and any configured feed evidence; no order execution."
                ),
            },
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
