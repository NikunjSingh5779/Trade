from datetime import UTC, datetime, timedelta

import pytest

from app.queue.worker import QueueWorker
from app.risk.consistency import ProposalMismatch
from app.storage import Database


def _bars(symbol="AAPL", close=100.0, count=12):
    now = datetime.now(UTC)
    return [{
        "symbol": symbol,
        "timestamp": (now - timedelta(minutes=5 * (count - 1 - i) + 1)).isoformat().replace(
            "+00:00", "Z"
        ),
        "open": close, "high": close + 0.5, "low": close - 0.5, "close": close, "volume": 1000.0,
    } for i in range(count)]


class StubAgent:
    """Returns a canned model answer so the worker's handling of it can be tested."""

    def __init__(self, structured):
        self.structured = structured

    async def run(self, task, payload):
        return {"task": task, "text": "stub", "provider": {}, "structured": self.structured}


def _model_answer(**overrides):
    answer = {
        "symbol": "AAPL", "side": "long", "entry_price": 100.0, "stop_loss": 98.0,
        "take_profit": 104.0, "thesis": "Trend and momentum align from supplied candles.",
        "evidence": ["latest close: 100"], "strategy_id": "unit", "timeframe": "intraday",
    }
    answer.update(overrides)
    return answer


def _worker(tmp_path, structured):
    db = Database(str(tmp_path / "worker.db"))
    db.initialize([])
    return db, QueueWorker(db, StubAgent(structured), 100, 600, 2.0)


def _payload(**overrides):
    payload = {"symbol": "AAPL", "timeframe": "intraday", "bars": _bars()}
    payload.update(overrides)
    return payload


async def test_valid_proposal_is_saved_with_server_stamped_fields(tmp_path):
    db, worker = _worker(tmp_path, _model_answer())
    payload = _payload()

    result = await worker._process("analyze_proposal", payload)

    saved = result["proposal"]
    assert saved["symbol"] == "AAPL"
    assert saved["data_timeframe"] == "intraday"
    assert saved["data_asof"] == payload["bars"][-1]["timestamp"]
    assert len(db.list_proposals()) == 1


async def test_proposal_for_another_symbol_is_rejected_and_not_saved(tmp_path):
    # Regression: an MSFT/swing/$500 answer to an AAPL/intraday (~$100) request used to be
    # saved, approvable, and filled at $500.
    answer = _model_answer(
        symbol="MSFT", entry_price=500.0, stop_loss=490.0, take_profit=520.0, timeframe="swing"
    )
    db, worker = _worker(tmp_path, answer)

    with pytest.raises(ProposalMismatch, match="MSFT but AAPL was requested"):
        await worker._process("analyze_proposal", _payload())

    assert db.list_proposals() == []


async def test_invented_price_on_the_right_symbol_is_rejected(tmp_path):
    answer = _model_answer(entry_price=250.0, stop_loss=245.0, take_profit=260.0)
    db, worker = _worker(tmp_path, answer)

    with pytest.raises(ProposalMismatch, match="invented prices"):
        await worker._process("analyze_proposal", _payload())

    assert db.list_proposals() == []


async def test_timeframe_that_contradicts_the_request_is_rejected(tmp_path):
    db, worker = _worker(tmp_path, _model_answer(timeframe="swing"))

    with pytest.raises(ProposalMismatch, match="setup swing but the request was for intraday"):
        await worker._process("analyze_proposal", _payload())

    assert db.list_proposals() == []


async def test_omitted_timeframe_inherits_intraday_default(tmp_path):
    answer = _model_answer()
    del answer["timeframe"]
    db, worker = _worker(tmp_path, answer)

    result = await worker._process("analyze_proposal", _payload(timeframe="intraday"))

    assert result["proposal"]["timeframe"] == "intraday"
    assert result["proposal"]["data_timeframe"] == "intraday"


async def test_legacy_swing_analysis_job_is_rejected_before_inference(tmp_path):
    db, worker = _worker(tmp_path, _model_answer())

    with pytest.raises(ValueError, match="intraday proposal jobs only"):
        await worker._process("analyze_proposal", _payload(timeframe="swing"))

    assert db.list_proposals() == []


async def test_no_setup_answer_creates_no_proposal(tmp_path):
    db, worker = _worker(tmp_path, {"no_setup": True, "reason": "Range-bound."})

    result = await worker._process("analyze_proposal", _payload())

    assert "proposal" not in result
    assert result["no_setup_reason"] == "Range-bound."
    assert db.list_proposals() == []
