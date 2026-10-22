from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from pydantic import ValidationError

from app.config import Settings
from app.execution.alpaca_paper import (
    AlpacaOrderOutcomeUnknown, AlpacaPaperError, AlpacaPaperTrading, PAPER_BASE_URL,
)
import app.execution.paper_broker as paper_broker_module
from app.execution.paper_broker import PaperBroker
from app.storage import Database
from app.queue.worker import QueueWorker


def _settings(tmp_path, **overrides):
    values = {
        "_env_file": None,
        "database_path": str(tmp_path / "alpaca-paper.db"),
        "inference_lock_path": str(tmp_path / "infer.lock"),
        "market_scan_enabled": False,
        "alpaca_paper_trading_enabled": True,
        "alpaca_paper_api_key": "paper-key-test",
        "alpaca_paper_api_secret": "paper-secret-test",
        "paper_initial_equity": 100000,
        "risk_per_trade_pct": 0.25,
        "max_position_value_pct": 10,
        "max_gross_exposure_pct": 20,
    }
    values.update(overrides)
    return Settings(**values)


def test_alpaca_execution_requires_paper_credentials():
    with pytest.raises(ValidationError, match="Alpaca Paper execution requires"):
        Settings(
            _env_file=None,
            alpaca_paper_trading_enabled=True,
            alpaca_paper_api_key="",
            alpaca_paper_api_secret="",
        )


@respx.mock
def test_account_requests_use_only_the_hardcoded_paper_host(tmp_path):
    route = respx.get(f"{PAPER_BASE_URL}/v2/account").mock(
        return_value=httpx.Response(200, json={"status": "ACTIVE", "equity": "100000"})
    )
    client = AlpacaPaperTrading(_settings(tmp_path))

    account = client.account()

    assert route.called
    request = route.calls[0].request
    assert request.headers["APCA-API-KEY-ID"] == "paper-key-test"
    assert request.headers["APCA-API-SECRET-KEY"] == "paper-secret-test"
    assert str(request.url).startswith("https://paper-api.alpaca.markets/")
    assert account["equity"] == "100000"


@respx.mock
def test_bracket_submission_uses_stable_id_and_protective_orders(tmp_path):
    lookup = respx.get(f"{PAPER_BASE_URL}/v2/orders:by_client_order_id").mock(
        return_value=httpx.Response(404, json={"message": "order not found"})
    )
    create = respx.post(f"{PAPER_BASE_URL}/v2/orders").mock(
        return_value=httpx.Response(200, json={
            "id": "alpaca-order-1", "status": "filled",
            "filled_avg_price": "100.08", "qty": "25",
            "client_order_id": "trade-proposal-1",
        })
    )
    client = AlpacaPaperTrading(_settings(tmp_path))

    order = client.submit_bracket(
        symbol="AAPL", side="long", quantity=25, stop_loss=98,
        take_profit=104, client_order_id="trade-proposal-1",
    )

    assert lookup.called and create.called
    payload = create.calls[0].request.content
    import json
    body = json.loads(payload)
    assert body["symbol"] == "AAPL"
    assert body["side"] == "buy"
    assert body["order_class"] == "bracket"
    assert body["time_in_force"] == "day"
    assert body["client_order_id"] == "trade-proposal-1"
    assert body["take_profit"] == {"limit_price": "104.00"}
    assert body["stop_loss"] == {"stop_price": "98.00"}
    assert order["filled_avg_price"] == "100.08"


@respx.mock
def test_filled_order_is_reused_instead_of_resubmitted(tmp_path):
    lookup = respx.get(f"{PAPER_BASE_URL}/v2/orders:by_client_order_id").mock(
        return_value=httpx.Response(200, json={
            "id": "alpaca-order-existing", "status": "filled",
            "filled_avg_price": "100.08", "qty": "25",
            "client_order_id": "trade-proposal-1",
        })
    )
    create = respx.post(f"{PAPER_BASE_URL}/v2/orders")
    client = AlpacaPaperTrading(_settings(tmp_path))

    order = client.submit_bracket(
        symbol="AAPL", side="long", quantity=25, stop_loss=98,
        take_profit=104, client_order_id="trade-proposal-1",
    )

    assert lookup.called
    assert not create.called
    assert order["id"] == "alpaca-order-existing"


@respx.mock
def test_api_error_contains_request_id_but_not_credentials(tmp_path):
    route = respx.get(f"{PAPER_BASE_URL}/v2/account").mock(
        return_value=httpx.Response(
            403, json={"message": "paper account denied"},
            headers={"X-Request-ID": "req-test-123"},
        )
    )
    client = AlpacaPaperTrading(_settings(tmp_path))

    with pytest.raises(AlpacaPaperError, match="req-test-123") as exc:
        client.account()

    assert route.called
    assert "paper-secret-test" not in str(exc.value)



def test_broker_approval_records_confirmed_fill_and_close_uses_broker_fill(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    db.initialize(["AAPL"])
    now = datetime.now(UTC).isoformat()
    db.save_bars([{
        "symbol": "AAPL", "timestamp": now, "open": 100.0,
        "high": 100.5, "low": 99.5, "close": 100.0, "volume": 10000,
    }], source="test", timeframe="5Min")
    proposal = db.add_proposal({
        "symbol": "AAPL", "side": "long", "entry_price": 100.0, "stop_loss": 98.0,
        "take_profit": 104.0, "thesis": "Intraday trend setup from the latest five-minute bars.",
        "evidence": ["latest close: 100"], "strategy_id": "test", "timeframe": "intraday",
        "data_asof": now, "data_timeframe": "intraday", "data_source": "test",
    })
    broker = PaperBroker(settings, db)
    monkeypatch.setattr(paper_broker_module, "is_regular_us_session", lambda now=None: True)
    monkeypatch.setattr(
        paper_broker_module, "us_session_bounds",
        lambda session_day: (datetime.now(UTC) - timedelta(hours=1),
                              datetime.now(UTC) + timedelta(hours=1)),
    )
    monkeypatch.setattr(
        broker, "_require_reconciled_broker",
        lambda: ({"status": "ACTIVE", "equity": "100000"}, []),
    )
    submitted = {}
    monkeypatch.setattr(
        broker.alpaca, "submit_bracket",
        lambda **kwargs: (
            submitted.update(kwargs)
            or {"id": "entry-order-1", "status": "filled", "filled_avg_price": "100.08"}
        ),
    )

    result = broker.approve(proposal["id"], 1)

    assert result["mode"] == "alpaca_paper"
    assert result["execution_mode"] == "alpaca_paper"
    assert result["broker_order_id"] == "entry-order-1"
    assert result["fill_price"] == pytest.approx(100.08)
    assert submitted["symbol"] == "AAPL"
    assert submitted["client_order_id"].startswith("trade-")
    with db.connect() as conn:
        position = dict(conn.execute("SELECT * FROM positions WHERE status='open'").fetchone())
    assert position["execution_mode"] == "alpaca_paper"
    assert position["entry_price"] == pytest.approx(100.08)

    monkeypatch.setattr(broker.alpaca, "positions", lambda: [{
        "symbol": "AAPL", "qty": str(position["quantity"]), "side": "long",
    }])
    monkeypatch.setattr(
        broker.alpaca, "close_position",
        lambda symbol: {"id": "exit-order-1", "status": "filled", "filled_avg_price": "100.40"},
    )

    closed = broker.close_position(position["id"], reason="test_close")

    assert closed["mode"] == "alpaca_paper"
    assert closed["broker_order_id"] == "exit-order-1"
    with db.connect() as conn:
        row = conn.execute("SELECT status, current_price FROM positions WHERE id=?",
                           (position["id"],)).fetchone()
    assert row["status"] == "closed"
    assert row["current_price"] == pytest.approx(100.40)



def _proposal_body(timestamp):
    return {
        "symbol": "AAPL", "side": "long", "entry_price": 100.0, "stop_loss": 98.0,
        "take_profit": 104.0, "thesis": "Intraday trend setup from the latest five-minute bars.",
        "evidence": ["latest completed close: 100"], "strategy_id": "test",
        "timeframe": "intraday", "data_asof": timestamp,
        "data_timeframe": "intraday", "data_source": "test",
    }


def test_ambiguous_alpaca_submission_is_recovered_from_client_order_id(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    db.initialize(["AAPL"])
    now = datetime.now(UTC)
    bars = [{
        "symbol": "AAPL", "timestamp": (now - timedelta(minutes=6 + 5 * (11 - i))).isoformat(),
        "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 10000,
    } for i in range(12)]
    db.save_bars(bars, source="test", timeframe="5Min")
    proposal = db.add_proposal(_proposal_body(now.isoformat()))
    broker = PaperBroker(settings, db)
    monkeypatch.setattr(paper_broker_module, "is_regular_us_session", lambda now=None: True)
    monkeypatch.setattr(
        paper_broker_module, "us_session_bounds",
        lambda session_day: (datetime.now(UTC) - timedelta(hours=1),
                              datetime.now(UTC) + timedelta(hours=1)),
    )
    monkeypatch.setattr(
        broker, "_require_reconciled_broker",
        lambda: ({"status": "ACTIVE", "equity": "100000"}, []),
    )
    intent = {}

    def uncertain_submit(**kwargs):
        intent.update(kwargs)
        raise AlpacaOrderOutcomeUnknown("test timeout after order submission")

    monkeypatch.setattr(broker.alpaca, "submit_bracket", uncertain_submit)
    with pytest.raises(AlpacaOrderOutcomeUnknown):
        broker.approve(proposal["id"], proposal["version"])

    assert db.get_proposal(proposal["id"])["status"] == "pending"
    with db.connect() as conn:
        saved_intent = dict(conn.execute(
            "SELECT * FROM broker_order_intents WHERE proposal_id=?", (proposal["id"],)
        ).fetchone())
    assert saved_intent["status"] == "unknown"
    assert saved_intent["client_order_id"] == intent["client_order_id"]

    quantity = intent["quantity"]
    remote_position = {
        "symbol": "AAPL", "qty": str(quantity), "side": "long",
        "current_price": "100.20", "market_value": str(quantity * 100.20),
        "unrealized_pl": str(quantity * 0.12),
    }
    monkeypatch.setattr(broker.alpaca, "positions", lambda: [remote_position])
    monkeypatch.setattr(broker.alpaca, "order_by_client_id", lambda client_id: {
        "id": "entry-order-recovered", "status": "filled", "qty": str(quantity),
        "filled_qty": str(quantity), "filled_avg_price": "100.08",
        "client_order_id": intent["client_order_id"],
    })

    status = broker.sync_broker_positions()

    assert status["reconciled"] is True
    assert status["recovered_entries"][0]["symbol"] == "AAPL"
    assert db.get_proposal(proposal["id"])["status"] == "approved"
    with db.connect() as conn:
        position = dict(conn.execute(
            "SELECT * FROM positions WHERE proposal_id=?", (proposal["id"],)
        ).fetchone())
        saved_intent = dict(conn.execute(
            "SELECT * FROM broker_order_intents WHERE proposal_id=?", (proposal["id"],)
        ).fetchone())
    assert position["execution_mode"] == "alpaca_paper"
    assert position["quantity"] == quantity
    assert position["entry_price"] == pytest.approx(100.08)
    assert saved_intent["status"] == "recovered"


def test_autonomous_worker_keeps_proposal_pending_after_ambiguous_broker_write(tmp_path, monkeypatch):
    settings = _settings(
        tmp_path, autonomous_paper_trading_enabled=True,
        autonomous_trading_symbols="AAPL", watchlist_symbols="AAPL",
    )
    db = Database(settings.database_path)
    db.initialize(["AAPL"])
    now = datetime.now(UTC)
    bars = [{
        "symbol": "AAPL",
        "timestamp": (now - timedelta(minutes=6 + 5 * (11 - i))).isoformat(),
        "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1000,
    } for i in range(12)]
    db.save_bars(bars, source="test", timeframe="5Min")
    broker = PaperBroker(settings, db)
    monkeypatch.setattr(
        broker, "approve",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AlpacaOrderOutcomeUnknown("paper broker response timed out")
        ),
    )

    class StubAgent:
        async def run(self, task, payload):
            return {"task": task, "text": "", "provider": {}}

    worker = QueueWorker(db, StubAgent(), settings=settings, paper_broker=broker)
    decision = {
        "symbol": "AAPL", "decision": "trade", "side": "long",
        "entry_price": 100.0, "stop_loss": 98.0, "take_profit": 104.0,
        "thesis": "Completed five-minute candles support this intraday test setup.",
        "evidence": ["latest close 100"], "strategy_id": "test",
    }
    result = {"structured": {"decisions": [decision]}}
    worker._execute_autonomous_scan(result, {
        "symbols": ["AAPL"], "bars_by_symbol": {"AAPL": bars},
    })

    pending = db.list_proposals()
    assert len(pending) == 1
    assert pending[0]["status"] == "pending"
    assert result["autonomous_trading"]["decision_outcomes"][0]["status"] == "broker_pending"



def _prepare_unknown_intent(tmp_path):
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    db.initialize(["AAPL"])
    now = datetime.now(UTC)
    proposal = db.add_proposal(_proposal_body(now.isoformat()))
    client_order_id = PaperBroker._client_order_id(proposal["id"])
    db.register_broker_order_intent(
        proposal["id"], proposal["version"], client_order_id, "AAPL"
    )
    return settings, db, proposal, client_order_id


def test_unknown_order_intent_requires_waiting_period(tmp_path, monkeypatch):
    settings, db, proposal, client_order_id = _prepare_unknown_intent(tmp_path)
    broker = PaperBroker(settings, db)
    monkeypatch.setattr(broker.alpaca, "order_by_client_id", lambda client_id: None)
    monkeypatch.setattr(broker.alpaca, "positions", lambda: [])

    with pytest.raises(Exception, match="at least 120 seconds"):
        broker.resolve_unknown_order_intent(
            proposal["id"], confirmed_no_order_and_no_position=True
        )

    with db.connect() as conn:
        intent = conn.execute(
            "SELECT status FROM broker_order_intents WHERE proposal_id=?", (proposal["id"],)
        ).fetchone()
    assert intent["status"] == "submitting"


def test_unknown_order_intent_requires_broker_order_absent(tmp_path, monkeypatch):
    settings, db, proposal, client_order_id = _prepare_unknown_intent(tmp_path)
    with db.connect() as conn:
        old_time = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        conn.execute(
            "UPDATE broker_order_intents SET created_at=?, updated_at=? WHERE proposal_id=?",
            (old_time, old_time, proposal["id"]),
        )
    broker = PaperBroker(settings, db)
    monkeypatch.setattr(broker.alpaca, "order_by_client_id", lambda client_id: {
        "id": "found-order", "status": "filled", "client_order_id": client_order_id,
    })
    monkeypatch.setattr(broker.alpaca, "positions", lambda: [])

    with pytest.raises(Exception, match="still returns an order"):
        broker.resolve_unknown_order_intent(
            proposal["id"], confirmed_no_order_and_no_position=True
        )

    with db.connect() as conn:
        intent = conn.execute(
            "SELECT status FROM broker_order_intents WHERE proposal_id=?", (proposal["id"],)
        ).fetchone()
    assert intent["status"] == "submitting"


def test_unknown_order_intent_requires_no_broker_position(tmp_path, monkeypatch):
    settings, db, proposal, client_order_id = _prepare_unknown_intent(tmp_path)
    with db.connect() as conn:
        old_time = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        conn.execute(
            "UPDATE broker_order_intents SET created_at=?, updated_at=? WHERE proposal_id=?",
            (old_time, old_time, proposal["id"]),
        )
    broker = PaperBroker(settings, db)
    monkeypatch.setattr(broker.alpaca, "order_by_client_id", lambda client_id: None)
    monkeypatch.setattr(broker.alpaca, "positions", lambda: [{
        "symbol": "AAPL", "qty": "5", "side": "long",
    }])

    with pytest.raises(Exception, match="still reports a AAPL position"):
        broker.resolve_unknown_order_intent(
            proposal["id"], confirmed_no_order_and_no_position=True
        )


def test_unknown_order_intent_resolves_only_after_api_checks_and_confirmation(tmp_path, monkeypatch):
    settings, db, proposal, client_order_id = _prepare_unknown_intent(tmp_path)
    with db.connect() as conn:
        old_time = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        conn.execute(
            "UPDATE broker_order_intents SET created_at=?, updated_at=? WHERE proposal_id=?",
            (old_time, old_time, proposal["id"]),
        )
    broker = PaperBroker(settings, db)
    order_checks = []
    position_checks = []
    monkeypatch.setattr(
        broker.alpaca, "order_by_client_id",
        lambda client_id: order_checks.append(client_id) or None,
    )
    monkeypatch.setattr(broker.alpaca, "positions", lambda: position_checks.append(True) or [])

    result = broker.resolve_unknown_order_intent(
        proposal["id"], confirmed_no_order_and_no_position=True
    )

    assert result["status"] == "resolved_no_order"
    assert result["proposal_status"] == "pending"
    assert order_checks == [client_order_id]
    assert position_checks == [True]
    with db.connect() as conn:
        intent = dict(conn.execute(
            "SELECT status, last_error FROM broker_order_intents WHERE proposal_id=?",
            (proposal["id"],),
        ).fetchone())
    assert intent["status"] == "resolved_no_order"
    assert "Operator confirmed" in intent["last_error"]
    assert db.get_proposal(proposal["id"])["status"] == "pending"

    # A subsequent explicit approval reuses the same idempotent client order ID.
    db.register_broker_order_intent(
        proposal["id"], proposal["version"], client_order_id, "AAPL"
    )
    with db.connect() as conn:
        retried = dict(conn.execute(
            "SELECT status, client_order_id FROM broker_order_intents WHERE proposal_id=?",
            (proposal["id"],),
        ).fetchone())
    assert retried["status"] == "submitting"
    assert retried["client_order_id"] == client_order_id
