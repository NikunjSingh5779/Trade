import pytest

from app.storage import Database


def test_jobs_persist_and_claim_once(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.initialize(["AAPL"])
    job = db.create_job("chat", {"message": "hello"})
    claimed = db.claim_next_job()
    assert claimed["id"] == job["id"]
    assert claimed["status"] == "running"
    assert db.claim_next_job() is None
    db.finish_job(job["id"], {"text": "done"})
    assert db.get_job(job["id"])["status"] == "succeeded"


def test_approval_is_atomic_and_idempotent(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.initialize([])
    body = {
        "symbol": "AAPL", "side": "long", "entry_price": 100.0, "stop_loss": 98.0,
        "take_profit": 104.0, "thesis": "Valid thesis based on supplied data.",
        "evidence": ["test"], "strategy_id": "unit", "timeframe": "intraday",
    }
    proposal = db.add_proposal(body)
    result = db.approve_paper_trade(proposal["id"], 1, 10, 5, 20000)
    assert result["mode"] == "paper"
    with pytest.raises(ValueError):
        db.approve_paper_trade(proposal["id"], 1, 10, 5, 20000)
    assert len(db.list_orders()) == 1


def test_market_bars_are_isolated_by_timeframe(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.initialize([])
    five_minute = [{
        "symbol": "AAPL", "timestamp": "2026-10-20T19:55:00Z",
        "open": 339.5, "high": 340.0, "low": 339.0, "close": 339.66, "volume": 100,
    }]
    daily = [{
        "symbol": "AAPL", "timestamp": "2026-10-20T13:30:00Z",
        "open": 340.0, "high": 345.0, "low": 338.0, "close": 339.66, "volume": 1000,
    }]
    db.save_bars(five_minute, source="test", timeframe="5Min")
    db.save_bars(daily, source="test", timeframe="1Day")

    assert len(db.get_bars("AAPL", timeframe="5Min")) == 1
    assert len(db.get_bars("AAPL", timeframe="1Day")) == 1
    assert db.get_bars("AAPL", timeframe="5Min")[0]["timestamp"] == "2026-10-20T19:55:00Z"
    assert db.get_bars("AAPL", timeframe="1Day")[0]["timestamp"] == "2026-10-20T13:30:00Z"
