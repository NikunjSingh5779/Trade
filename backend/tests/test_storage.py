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
