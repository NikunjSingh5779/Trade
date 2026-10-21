import sqlite3
from datetime import UTC, datetime

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



def _insert_open_position(
    db, *, side="long", timeframe="intraday", opened_at="2026-10-20T19:00:00+00:00"
):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO positions(id,proposal_id,symbol,side,quantity,entry_price,current_price,"
            "stop_loss,take_profit,status,opened_at,timeframe) "
            "VALUES (?,?,?,?,?,?,?,?,?,'open',?,?)",
            ("pos-test", "prop-test", "AAPL", side, 10, 100.0, 100.0,
             95.0 if side == "long" else 105.0,
             110.0 if side == "long" else 90.0, opened_at, timeframe),
        )


def _bar(*, timestamp="2026-10-20T19:05:00Z", open_price=100, high=103, low=97, close=101):
    return {
        "symbol": "AAPL", "timestamp": timestamp,
        "open": open_price, "high": high, "low": low, "close": close, "volume": 100,
    }


def test_paper_position_auto_closes_at_take_profit_and_records_realized_pnl(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.initialize([])
    _insert_open_position(db)

    exits = db.process_position_bar(_bar(high=111, close=109))

    assert len(exits) == 1
    assert exits[0]["reason"] == "take_profit"
    assert exits[0]["exit_price"] == 110.0
    assert exits[0]["realized_pnl"] == 100.0
    assert exits[0]["mode"] == "paper"
    with db.connect() as conn:
        position = conn.execute("SELECT * FROM positions WHERE id='pos-test'").fetchone()
    assert position["status"] == "closed"
    assert position["current_price"] == 110.0
    assert position["realized_pnl"] == 100.0

    # Replayed historical bars must not close or book the same position twice.
    assert db.process_position_bar(_bar(high=111, close=109)) == []
    assert len(db.list_orders()) == 0


def test_paper_stop_wins_when_one_bar_crosses_both_levels(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.initialize([])
    _insert_open_position(db)

    exits = db.process_position_bar(_bar(high=112, low=94, close=105))

    assert len(exits) == 1
    assert exits[0]["reason"] == "stop_loss_ambiguous_bar"
    assert exits[0]["exit_price"] == 95.0
    assert exits[0]["realized_pnl"] == -50.0


def test_paper_exit_handles_gap_beyond_stop_conservatively(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.initialize([])
    _insert_open_position(db)

    exits = db.process_position_bar(_bar(open_price=92, high=96, low=91, close=94))

    assert len(exits) == 1
    assert exits[0]["reason"] == "stop_loss_gap"
    assert exits[0]["exit_price"] == 92.0
    assert exits[0]["realized_pnl"] == -80.0


def test_paper_short_position_closes_at_take_profit(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.initialize([])
    _insert_open_position(db, side="short")

    exits = db.process_position_bar(_bar(high=102, low=89, close=92))

    assert len(exits) == 1
    assert exits[0]["reason"] == "take_profit"
    assert exits[0]["exit_price"] == 90.0
    assert exits[0]["realized_pnl"] == 100.0


def test_position_bar_before_entry_is_ignored(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.initialize([])
    _insert_open_position(db, opened_at="2026-10-20T19:06:00+00:00")

    exits = db.process_position_bar(_bar(high=112, low=94))

    assert exits == []
    with db.connect() as conn:
        position = conn.execute("SELECT * FROM positions WHERE id='pos-test'").fetchone()
    assert position["status"] == "open"



def test_database_connection_is_closed_after_context_exit(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    with db.connect() as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")


def test_manual_close_can_use_only_the_recorded_server_mark(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.initialize([])
    _insert_open_position(db)
    db.mark_position("AAPL", 101.25)

    closed = db.close_position("pos-test", None)

    assert closed["simulated_exit_price"] == 101.25
    assert closed["pricing_method"] == "last_recorded_server_mark"
    assert closed["realized_pnl"] == 12.5
    with db.connect() as conn:
        row = conn.execute("SELECT status, closed_at FROM positions WHERE id='pos-test'").fetchone()
    assert row["status"] == "closed"
    assert row["closed_at"] is not None


def test_swing_position_is_evaluated_only_against_daily_bars(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.initialize([])
    _insert_open_position(db, timeframe="swing")
    target_bar = _bar(high=111, close=109)

    assert db.process_position_bar(target_bar, timeframe="intraday") == []
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM positions WHERE id='pos-test'").fetchone()[0] == "open"

    exits = db.process_position_bar(target_bar, timeframe="swing")
    assert len(exits) == 1
    assert exits[0]["reason"] == "take_profit"


def test_legacy_positions_table_gets_timeframe_column_on_initialize(tmp_path):
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TABLE positions (
                id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL UNIQUE,
                symbol TEXT NOT NULL, side TEXT NOT NULL, quantity INTEGER NOT NULL,
                entry_price REAL NOT NULL, current_price REAL NOT NULL,
                stop_loss REAL NOT NULL, take_profit REAL NOT NULL,
                status TEXT NOT NULL, opened_at TEXT NOT NULL, closed_at TEXT,
                realized_pnl REAL NOT NULL DEFAULT 0
            )
        """)
    db = Database(path)
    db.initialize([])
    with db.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(positions)")}
    assert "timeframe" in columns
    assert "last_processed_bar_at" in columns



def test_exit_checkpoint_prevents_an_older_bar_from_rewinding_the_mark(tmp_path):
    db = Database(str(tmp_path / "checkpoint.db"))
    db.initialize([])
    _insert_open_position(db)

    first = _bar(timestamp="2026-10-20T19:05:00Z", close=101.0)
    assert db.process_position_bar(first) == []
    older = _bar(timestamp="2026-10-20T19:04:00Z", close=99.0)
    assert db.process_position_bar(older) == []

    with db.connect() as conn:
        row = conn.execute(
            "SELECT current_price, last_processed_bar_at FROM positions WHERE id='pos-test'"
        ).fetchone()
    assert row["current_price"] == 101.0
    assert row["last_processed_bar_at"] == "2026-10-20T19:05:00+00:00"
