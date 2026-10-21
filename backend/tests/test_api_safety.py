from fastapi.testclient import TestClient

from app import main
from app.storage import Database


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
    monkeypatch.setattr(
        main.db, "active_queue_count", lambda: main.settings.ai_queue_max_pending
    )
    response = TestClient(main.app).post(
        "/api/proposals/analyze",
        json={"symbol": "AAPL", "timeframe": "intraday"},
    )
    assert response.status_code == 429
    assert "queue is full" in response.json()["detail"]


def test_close_endpoint_ignores_client_supplied_exit_price(monkeypatch, tmp_path):
    test_db = _db_with_open_position(tmp_path / "api.db")
    monkeypatch.setattr(main, "db", test_db)

    response = TestClient(main.app).post(
        "/api/positions/pos-api/close",
        json={"simulated_exit_price": 999999.0},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["simulated_exit_price"] == 101.25
    assert result["pricing_method"] == "last_recorded_server_mark"
    assert result["realized_pnl"] == 12.5
