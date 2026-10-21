import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.execution.paper_broker import PaperBroker
from app.market_data.monitor import MarketMonitor
from app.queue.worker import QueueWorker
from app.storage import Database


class StubAgent:
    async def run(self, task, payload):
        return {"task": task, "text": "unused", "provider": "test"}


class EmptyProvider:
    configured = True

    async def fetch_bars(self, symbols, timeframe="5Min", limit=50, start_time=None):
        return []


class EmptyFeeds:
    async def fetch_news(self, symbol=None, limit=20):
        return {"configured": False, "items": [], "errors": []}

    async def fetch_events(self, limit=20):
        return {"configured": False, "items": [], "errors": []}


def _settings(tmp_path, **overrides):
    values = {
        "_env_file": None,
        "database_path": str(tmp_path / "autonomous.db"),
        "inference_lock_path": str(tmp_path / "infer.lock"),
        "autonomous_paper_trading_enabled": True,
        "autonomous_trading_symbols": "AAPL",
        "watchlist_symbols": "AAPL",
        "max_stale_data_seconds": 600,
        "max_position_value_pct": 10,
        "max_gross_exposure_pct": 20,
        "market_scan_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def _bars(symbol="AAPL", close=100.0):
    now = datetime.now(UTC)
    result = []
    for index in range(12):
        stamp = now - timedelta(minutes=6 + 5 * (11 - index))
        result.append({
            "symbol": symbol,
            "timestamp": stamp.isoformat(),
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 1000,
            "source": "test",
        })
    return result


def test_autonomous_decision_opens_only_a_valid_paper_position(tmp_path, monkeypatch):
    import app.execution.paper_broker as broker_module

    monkeypatch.setattr(broker_module, "is_regular_us_session", lambda: True)
    monkeypatch.setattr(broker_module, "is_regular_us_bar", lambda bar: True)
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    db.initialize(["AAPL"])
    bars = _bars()
    db.save_bars(bars, source="test", timeframe="5Min")
    broker = PaperBroker(settings, db)
    worker = QueueWorker(
        db, StubAgent(), settings=settings, paper_broker=broker,
        max_stale_data_seconds=600, max_entry_deviation_pct=2.0,
    )
    decision = {
        "symbol": "AAPL", "decision": "trade", "side": "long",
        "entry_price": 100.0, "stop_loss": 98.0, "take_profit": 104.0,
        "thesis": "Completed candles show a defensible upward setup.",
        "evidence": ["Latest completed close is 100.0."], "strategy_id": "test",
    }
    result = {"structured": {"decisions": [decision]}}
    payload = {"symbols": ["AAPL"], "bars_by_symbol": {"AAPL": bars}}

    worker._execute_autonomous_scan(result, payload)

    portfolio = db.portfolio_snapshot(settings.paper_initial_equity)
    assert len(portfolio["open_positions"]) == 1
    assert portfolio["open_positions"][0]["symbol"] == "AAPL"
    assert portfolio["open_positions"][0]["timeframe"] == "intraday"
    assert db.list_orders()[0]["status"] == "filled"
    assert result["autonomous_trading"]["paper_trades_opened"] == 1
    assert settings.live_trading_enabled is False


def test_ambiguous_or_unapproved_symbol_fails_closed(tmp_path, monkeypatch):
    import app.execution.paper_broker as broker_module

    monkeypatch.setattr(broker_module, "is_regular_us_session", lambda: True)
    monkeypatch.setattr(broker_module, "is_regular_us_bar", lambda bar: True)
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    db.initialize(["AAPL"])
    bars = _bars()
    db.save_bars(bars, source="test", timeframe="5Min")
    worker = QueueWorker(
        db, StubAgent(), settings=settings, paper_broker=PaperBroker(settings, db),
    )
    trade = {
        "symbol": "AAPL", "decision": "trade", "side": "long",
        "entry_price": 100.0, "stop_loss": 98.0, "take_profit": 104.0,
        "thesis": "Completed candles show a defensible upward setup.",
        "evidence": ["Latest completed close is 100.0."], "strategy_id": "test",
    }
    unknown = {**trade, "symbol": "TSLA"}
    result = {"structured": {"decisions": [trade, unknown]}}
    worker._execute_autonomous_scan(
        result, {"symbols": ["AAPL"], "bars_by_symbol": {"AAPL": bars}}
    )
    assert len(db.portfolio_snapshot(settings.paper_initial_equity)["open_positions"]) == 1
    assert result["autonomous_trading"]["paper_trades_opened"] == 1
    events = []
    with db.connect() as conn:
        events = [row["event"] for row in conn.execute("SELECT event FROM audit").fetchall()]
    assert "autonomous_scan.symbol_rejected" in events


@pytest.mark.asyncio
async def test_intraday_positions_close_after_us_session_end(tmp_path):
    ny = ZoneInfo("America/New_York")
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    db.initialize(["AAPL"])
    opened = datetime(2026, 10, 22, 15, 30, tzinfo=ny).isoformat()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO positions(id,proposal_id,symbol,side,quantity,entry_price,current_price,"
            "stop_loss,take_profit,status,opened_at,timeframe) "
            "VALUES (?,?,?,?,?,?,?,?,?,'open',?,?)",
            ("pos-eod", "proposal-eod", "AAPL", "long", 10, 100, 101, 95, 110, opened, "intraday"),
        )
    monitor = MarketMonitor(settings, db, EmptyProvider(), EmptyFeeds())
    after_close = datetime(2026, 10, 22, 16, 5, tzinfo=ny)

    events = await monitor.close_intraday_positions_after_session(after_close)

    with db.connect() as conn:
        row = conn.execute(
            "SELECT status, realized_pnl FROM positions WHERE id='pos-eod'"
        ).fetchone()
        audit_row = conn.execute(
            "SELECT details_json FROM audit WHERE event='paper_position.session_close'"
        ).fetchone()
    assert row["status"] == "closed"
    assert row["realized_pnl"] == pytest.approx(10.0)
    assert audit_row is not None
    assert json.loads(audit_row["details_json"])["reason"] == "us_regular_session_close"
    assert events[0]["mode"] == "paper"
