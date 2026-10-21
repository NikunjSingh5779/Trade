from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings
from app.execution.paper_broker import PaperBroker
from app.risk.validator import RiskRejected
from app.storage import Database


def _setup(tmp_path, market_price=100.0):
    settings = Settings(
        database_path=str(tmp_path / "paper.db"),
        inference_lock_path=str(tmp_path / "infer.lock"),
        market_scan_enabled=False,
        paper_initial_equity=100000,
        risk_per_trade_pct=0.25,
        max_position_value_pct=20,
        max_gross_exposure_pct=20,
        simulated_slippage_bps=5,
    )
    db = Database(settings.database_path)
    db.initialize([])
    now = datetime.now(UTC).isoformat()
    db.save_bars([{
        "symbol": "AAPL", "timestamp": now, "open": market_price,
        "high": market_price + 1, "low": market_price - 1,
        "close": market_price, "volume": 1000,
    }], source="test", timeframe="5Min")
    db.save_bars([{
        "symbol": "AAPL", "timestamp": now, "open": market_price,
        "high": market_price + 1, "low": market_price - 1,
        "close": market_price, "volume": 10000,
    }], source="test", timeframe="1Day")
    return settings, db


def _body(**overrides):
    body = {
        "symbol": "AAPL", "side": "long", "entry_price": 100.0, "stop_loss": 98.0,
        "take_profit": 104.0, "thesis": "Trend and momentum align from supplied candles.",
        "evidence": ["latest close: 100"], "strategy_id": "unit", "timeframe": "intraday",
        "data_asof": datetime.now(UTC).isoformat(), "data_timeframe": "intraday",
        "data_source": "test",
    }
    body.update(overrides)
    return body


def test_approval_creates_simulated_order_and_position(tmp_path):
    settings, db = _setup(tmp_path)
    proposal = db.add_proposal(_body())
    result = PaperBroker(settings, db).approve(proposal["id"], 1)
    assert result["mode"] == "paper"
    assert result["quantity"] == 121
    assert result["simulated_price"] == pytest.approx(100.05)
    assert db.get_proposal(proposal["id"])["status"] == "approved"
    portfolio = db.portfolio_snapshot(settings.paper_initial_equity)
    assert len(portfolio["open_positions"]) == 1
    assert portfolio["open_positions"][0]["symbol"] == "AAPL"
    assert portfolio["open_positions"][0]["entry_price"] == pytest.approx(100.05)
    orders = db.list_orders()
    assert orders[0]["simulated_price"] == pytest.approx(100.05)
    assert orders[0]["details"]["fill_source"] == "latest_server_stored_bar_close_with_adverse_slippage"


def test_staleness_limit_comes_from_the_server_stamped_timeframe(tmp_path):
    # Regression: the limit used to follow the model's own `timeframe` label, so two-day-old
    # data could be approved by calling the idea "swing".
    settings, db = _setup(tmp_path)
    two_days_ago = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    proposal = db.add_proposal(
        _body(timeframe="swing", data_timeframe="intraday", data_asof=two_days_ago)
    )

    with pytest.raises(RiskRejected, match="stale"):
        PaperBroker(settings, db).approve(proposal["id"], 1)

    assert db.get_proposal(proposal["id"])["status"] == "pending"


def test_swing_data_within_a_week_is_approvable(tmp_path):
    settings, db = _setup(tmp_path)
    two_days_ago = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    proposal = db.add_proposal(
        _body(timeframe="swing", data_timeframe="swing", data_asof=two_days_ago)
    )

    assert PaperBroker(settings, db).approve(proposal["id"], 1)["mode"] == "paper"


@pytest.mark.parametrize("stamp", [None, "", "weekly"])
def test_unusable_data_timeframe_cannot_be_approved(tmp_path, stamp):
    settings, db = _setup(tmp_path)
    proposal = db.add_proposal(_body(data_timeframe=stamp))

    with pytest.raises(RiskRejected, match="data timeframe"):
        PaperBroker(settings, db).approve(proposal["id"], 1)


def test_proposal_saved_before_the_stamp_existed_is_rejected(tmp_path):
    settings, db = _setup(tmp_path)
    legacy = _body()
    del legacy["data_timeframe"]
    proposal = db.add_proposal(legacy)

    with pytest.raises(RiskRejected, match="data timeframe"):
        PaperBroker(settings, db).approve(proposal["id"], 1)

    assert db.get_proposal(proposal["id"])["status"] == "pending"



def test_approval_rejects_an_entry_that_drifted_too_far_from_market(tmp_path):
    settings, db = _setup(tmp_path, market_price=103.0)
    proposal = db.add_proposal(_body())

    with pytest.raises(RiskRejected, match="latest market mark"):
        PaperBroker(settings, db).approve(proposal["id"], 1)

    assert db.get_proposal(proposal["id"])["status"] == "pending"
    assert db.list_orders() == []
