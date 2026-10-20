from datetime import UTC, datetime

from app.config import Settings
from app.execution.paper_broker import PaperBroker
from app.storage import Database


def test_approval_creates_simulated_order_and_position(tmp_path):
    settings = Settings(
        database_path=str(tmp_path / "paper.db"),
        inference_lock_path=str(tmp_path / "infer.lock"),
        market_scan_enabled=False,
        paper_initial_equity=100000,
        risk_per_trade_pct=0.25,
        max_position_value_pct=20,
    )
    db = Database(settings.database_path)
    db.initialize([])
    proposal = db.add_proposal({
        "symbol": "AAPL", "side": "long", "entry_price": 100.0, "stop_loss": 98.0,
        "take_profit": 104.0, "thesis": "Trend and momentum align from supplied candles.",
        "evidence": ["latest close: 100"], "strategy_id": "unit", "timeframe": "intraday",
        "data_asof": datetime.now(UTC).isoformat(), "data_source": "test",
    })
    result = PaperBroker(settings, db).approve(proposal["id"], 1)
    assert result["mode"] == "paper"
    assert result["quantity"] == 125
    assert db.get_proposal(proposal["id"])["status"] == "approved"
    portfolio = db.portfolio_snapshot(settings.paper_initial_equity)
    assert len(portfolio["open_positions"]) == 1
    assert portfolio["open_positions"][0]["symbol"] == "AAPL"
