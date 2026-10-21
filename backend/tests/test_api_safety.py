from fastapi.testclient import TestClient

from app import main
from app.storage import Database


def _auth(monkeypatch):
    monkeypatch.setattr(main.settings, "local_api_token", "test-local-token")
    return {"authorization": "Bearer test-local-token"}


def _db_with_open_position(path):
    db = Database(str(path))
    db.initialize([])
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO positions(id,proposal_id,symbol,side,quantity,entry_price,current_price,"
            "stop_loss,take_profit,status,opened_at,timeframe) "
            "VALUES (?,?,?,?,?,?,?,?,?,'open',?,?)",
            ("pos-api", "prop-api", "AAPL", "long", 10, 100.0, 101.25,
             95.0, 110.0, "2026-10-20T19:00:00+00:00", "intraday"),
        )
    return db


def test_untrusted_host_is_rejected():
    client = TestClient(main.app)
    response = client.get("/api/health", headers={"host": "attacker.example"})
    assert response.status_code == 400


def test_proposal_analysis_obeys_queue_limit(monkeypatch):
    headers = _auth(monkeypatch)
    monkeypatch.setattr(
        main.db, "active_queue_count", lambda: main.settings.ai_queue_max_pending
    )
    response = TestClient(main.app).post(
        "/api/proposals/analyze",
        json={"symbol": "AAPL", "timeframe": "intraday"},
        headers=headers,
    )
    assert response.status_code == 429
    assert "queue is full" in response.json()["detail"]


def test_close_endpoint_ignores_client_supplied_exit_price(monkeypatch, tmp_path):
    test_db = _db_with_open_position(tmp_path / "api.db")
    headers = _auth(monkeypatch)
    monkeypatch.setattr(main, "db", test_db)

    response = TestClient(main.app).post(
        "/api/positions/pos-api/close",
        json={"simulated_exit_price": 999999.0},
        headers=headers,
    )

    assert response.status_code == 200
    result = response.json()
    assert result["simulated_exit_price"] == 101.25
    assert result["pricing_method"] == "last_recorded_server_mark"
    assert result["realized_pnl"] == 12.5



def test_stop_loss_cannot_be_widened_for_an_open_long(monkeypatch, tmp_path):
    test_db = _db_with_open_position(tmp_path / "stops.db")
    headers = _auth(monkeypatch)
    monkeypatch.setattr(main, "db", test_db)
    response = TestClient(main.app).post(
        "/api/positions/pos-api/stops",
        json={"stop_loss": 94.0, "take_profit": 110.0},
        headers=headers,
    )
    assert response.status_code == 422
    assert "cannot be widened" in response.json()["detail"]
    with test_db.connect() as conn:
        row = conn.execute("SELECT stop_loss FROM positions WHERE id='pos-api'").fetchone()
    assert row["stop_loss"] == 95.0


def test_stop_loss_can_be_tightened_for_an_open_long(monkeypatch, tmp_path):
    test_db = _db_with_open_position(tmp_path / "tighten.db")
    headers = _auth(monkeypatch)
    monkeypatch.setattr(main, "db", test_db)
    response = TestClient(main.app).post(
        "/api/positions/pos-api/stops",
        json={"stop_loss": 98.0, "take_profit": 108.0},
        headers=headers,
    )
    assert response.status_code == 200
    with test_db.connect() as conn:
        row = conn.execute("SELECT stop_loss, take_profit FROM positions WHERE id='pos-api'").fetchone()
    assert row["stop_loss"] == 98.0
    assert row["take_profit"] == 108.0



def test_mutation_routes_require_local_bearer_token(monkeypatch):
    monkeypatch.setattr(main.settings, "local_api_token", "test-local-token")
    response = TestClient(main.app).post(
        "/api/chat",
        json={"message": "test"},
    )
    assert response.status_code == 401


def test_mutation_routes_fail_closed_when_token_is_not_configured(monkeypatch):
    monkeypatch.setattr(main.settings, "local_api_token", "")
    response = TestClient(main.app).post(
        "/api/chat",
        json={"message": "test"},
    )
    assert response.status_code == 503



def test_mutation_routes_reject_invalid_bearer_token(monkeypatch):
    monkeypatch.setattr(main.settings, "local_api_token", "test-local-token")
    response = TestClient(main.app).post(
        "/api/chat",
        json={"message": "test"},
        headers={"authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401
