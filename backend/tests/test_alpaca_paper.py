import httpx
import pytest
import respx
from pydantic import ValidationError

from app.config import Settings
from app.execution.alpaca_paper import AlpacaPaperError, AlpacaPaperTrading, PAPER_BASE_URL
from app.execution.paper_broker import PaperBroker
from app.storage import Database


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
