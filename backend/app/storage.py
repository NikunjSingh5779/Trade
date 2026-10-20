from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class Database:
    """Small SQLite repository. Each operation owns a short-lived connection."""

    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def initialize(self, symbols: list[str]) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, task TEXT NOT NULL, payload_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed')),
                    dedupe_key TEXT UNIQUE, result_json TEXT, error TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_jobs_status_created ON jobs(status, created_at);
                CREATE TABLE IF NOT EXISTS proposals (
                    id TEXT PRIMARY KEY, version INTEGER NOT NULL, status TEXT NOT NULL,
                    body_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL UNIQUE,
                    symbol TEXT NOT NULL, side TEXT NOT NULL, quantity INTEGER NOT NULL,
                    status TEXT NOT NULL, simulated_price REAL NOT NULL,
                    created_at TEXT NOT NULL, details_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS positions (
                    id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL UNIQUE,
                    symbol TEXT NOT NULL, side TEXT NOT NULL, quantity INTEGER NOT NULL,
                    entry_price REAL NOT NULL, current_price REAL NOT NULL,
                    stop_loss REAL NOT NULL, take_profit REAL NOT NULL,
                    status TEXT NOT NULL, opened_at TEXT NOT NULL, closed_at TEXT,
                    realized_pnl REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS ix_positions_status ON positions(status);
                CREATE TABLE IF NOT EXISTS watchlist (
                    symbol TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bars (
                    symbol TEXT NOT NULL, timeframe TEXT NOT NULL, timestamp TEXT NOT NULL,
                    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
                    close REAL NOT NULL, volume REAL NOT NULL, source TEXT NOT NULL,
                    PRIMARY KEY(symbol, timeframe, timestamp)
                );
                CREATE INDEX IF NOT EXISTS ix_bars_symbol_time ON bars(symbol, timestamp);
                CREATE TABLE IF NOT EXISTS audit (
                    id TEXT PRIMARY KEY, event TEXT NOT NULL, details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS experiments (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL,
                    strategy_json TEXT NOT NULL, metrics_json TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)
            for symbol in symbols:
                conn.execute(
                    "INSERT OR IGNORE INTO watchlist(symbol, enabled, created_at) VALUES (?, 1, ?)",
                    (symbol.upper(), utc_now()),
                )
            # A process crash may leave jobs marked running. Only jobs known to have
            # survived as queued may resume automatically; ambiguous running jobs fail closed.
            conn.execute(
                "UPDATE jobs SET status='failed', error='Application restarted during execution; "
                "manual review required.', updated_at=? WHERE status='running'",
                (utc_now(),),
            )

    def add_audit(self, event: str, details: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO audit(id,event,details_json,created_at) VALUES (?,?,?,?)",
                (new_id("aud"), event, json.dumps(details, default=str), utc_now()),
            )

    def create_job(self, task: str, payload: dict[str, Any], dedupe_key: str | None = None) -> dict[str, Any]:
        job_id = new_id("job")
        now = utc_now()
        with self.connect() as conn:
            if dedupe_key:
                existing = conn.execute(
                    "SELECT * FROM jobs WHERE dedupe_key=?", (dedupe_key,)
                ).fetchone()
                if existing:
                    return self._job_row(existing)
            conn.execute(
                "INSERT INTO jobs(id,task,payload_json,status,dedupe_key,created_at,updated_at) "
                "VALUES (?,?,?,'queued',?,?,?)",
                (job_id, task, json.dumps(payload), dedupe_key, now, now),
            )
            return self.get_job(job_id)

    def claim_next_job(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                "UPDATE jobs SET status='running',updated_at=? WHERE id=? AND status='queued'",
                (utc_now(), row["id"]),
            )
            claimed = conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
            conn.commit()
            return self._job_row(claimed)

    def finish_job(self, job_id: str, result: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE jobs SET status='succeeded',result_json=?,error=NULL,updated_at=? WHERE id=?",
                (json.dumps(result, default=str), utc_now(), job_id),
            )

    def fail_job(self, job_id: str, error: str) -> None:
        # Never store provider keys or unbounded exception text in public job records.
        safe_error = error[:500]
        with self.connect() as conn:
            conn.execute(
                "UPDATE jobs SET status='failed',error=?,updated_at=? WHERE id=?",
                (safe_error, utc_now(), job_id),
            )

    @staticmethod
    def _job_row(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            raise KeyError("Job not found")
        data = dict(row)
        data["payload"] = json.loads(data.pop("payload_json"))
        data["result"] = json.loads(data.pop("result_json")) if data.get("result_json") else None
        data.pop("result_json", None)
        return data

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return self._job_row(row) if row else None

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._job_row(row) for row in rows]

    def active_queue_count(self) -> int:
        with self.connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')"
            ).fetchone()[0])

    def add_proposal(self, body: dict[str, Any]) -> dict[str, Any]:
        proposal_id = new_id("prop")
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO proposals(id,version,status,body_json,created_at,updated_at) "
                "VALUES (?,1,'pending',?,?,?)",
                (proposal_id, json.dumps(body), now, now),
            )
        return self.get_proposal(proposal_id)  # type: ignore[return-value]

    @staticmethod
    def _proposal_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "id": row["id"], "version": row["version"], "status": row["status"],
            **json.loads(row["body_json"]), "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            return self._proposal_row(row)

    def list_proposals(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM proposals ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._proposal_row(row) for row in rows if row]

    def list_watchlist(self) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT symbol FROM watchlist WHERE enabled=1 ORDER BY symbol"
            ).fetchall()
            return [row["symbol"] for row in rows]

    def set_watchlist(self, symbols: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(s.strip().upper() for s in symbols if s.strip()))
        if len(cleaned) > 100:
            raise ValueError("Watchlist is limited to 100 symbols.")
        with self.connect() as conn:
            conn.execute("UPDATE watchlist SET enabled=0")
            for symbol in cleaned:
                conn.execute(
                    "INSERT INTO watchlist(symbol,enabled,created_at) VALUES (?,1,?) "
                    "ON CONFLICT(symbol) DO UPDATE SET enabled=1",
                    (symbol, utc_now()),
                )
        return self.list_watchlist()

    def save_bars(self, bars: list[dict[str, Any]], source: str, timeframe: str = "5Min") -> int:
        count = 0
        with self.connect() as conn:
            for bar in bars:
                conn.execute(
                    "INSERT OR REPLACE INTO bars(symbol,timeframe,timestamp,open,high,low,close,volume,source) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        bar["symbol"], timeframe, bar["timestamp"], bar["open"], bar["high"],
                        bar["low"], bar["close"], bar.get("volume", 0), source,
                    ),
                )
                count += 1
        return count

    def get_bars(self, symbol: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT symbol,timestamp,open,high,low,close,volume,source FROM bars "
                "WHERE symbol=? ORDER BY timestamp DESC LIMIT ?",
                (symbol.upper(), limit),
            ).fetchall()
            return [dict(row) for row in reversed(rows)]

    def create_experiment(self, name: str, strategy: dict[str, Any]) -> dict[str, Any]:
        experiment_id, now = new_id("exp"), utc_now()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO experiments(id,name,status,strategy_json,created_at,updated_at) "
                "VALUES (?,?,'proposed',?,?,?)",
                (experiment_id, name, json.dumps(strategy), now, now),
            )
        return {"id": experiment_id, "name": name, "status": "proposed",
                "strategy": strategy, "created_at": now, "updated_at": now}

    def list_experiments(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM experiments ORDER BY created_at DESC").fetchall()
            return [{
                "id": r["id"], "name": r["name"], "status": r["status"],
                "strategy": json.loads(r["strategy_json"]),
                "metrics": json.loads(r["metrics_json"]) if r["metrics_json"] else None,
                "created_at": r["created_at"], "updated_at": r["updated_at"],
            } for r in rows]

    def portfolio_snapshot(self, initial_equity: float) -> dict[str, Any]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM positions WHERE status='open' ORDER BY opened_at DESC"
            ).fetchall()
            positions = [dict(row) for row in rows]
            exposure = sum(p["quantity"] * p["current_price"] for p in positions)
            unrealized = sum(
                (p["current_price"] - p["entry_price"]) * p["quantity"]
                * (1 if p["side"] == "long" else -1) for p in positions
            )
            realized = float(conn.execute(
                "SELECT COALESCE(SUM(realized_pnl),0) FROM positions WHERE status='closed'"
            ).fetchone()[0])
            equity = initial_equity + realized + unrealized
            return {
                "mode": "paper", "initial_equity": initial_equity, "equity": equity,
                "cash_estimate": equity - exposure, "gross_exposure": exposure,
                "unrealized_pnl": unrealized, "realized_pnl": realized,
                "open_positions": positions,
            }

    def approve_paper_trade(
        self, proposal_id: str, version: int, quantity: int, max_open_positions: int,
        max_exposure_value: float,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            proposal_row = conn.execute(
                "SELECT * FROM proposals WHERE id=?", (proposal_id,)
            ).fetchone()
            if proposal_row is None:
                conn.rollback()
                raise KeyError("Proposal not found")
            if proposal_row["version"] != version or proposal_row["status"] != "pending":
                conn.rollback()
                raise ValueError("Proposal is stale, already reviewed, or not pending.")
            body = json.loads(proposal_row["body_json"])
            open_positions = conn.execute(
                "SELECT * FROM positions WHERE status='open'"
            ).fetchall()
            if len(open_positions) >= max_open_positions:
                conn.rollback()
                raise ValueError("Maximum open paper positions reached.")
            if any(r["symbol"] == body["symbol"] for r in open_positions):
                conn.rollback()
                raise ValueError("An open paper position already exists for this symbol.")
            gross_exposure = sum(r["quantity"] * r["current_price"] for r in open_positions)
            new_exposure = quantity * body["entry_price"]
            if gross_exposure + new_exposure > max_exposure_value:
                conn.rollback()
                raise ValueError("Exposure limit would be exceeded.")
            order_id, position_id = new_id("ord"), new_id("pos")
            details = {"proposal_id": proposal_id, "proposal_version": version,
                       "note": "Simulated fill at proposal entry; not a real market fill."}
            conn.execute(
                "INSERT INTO orders(id,proposal_id,symbol,side,quantity,status,simulated_price,created_at,details_json) "
                "VALUES (?,?,?,?,?,'filled',?,?,?)",
                (order_id, proposal_id, body["symbol"], body["side"], quantity,
                 body["entry_price"], now, json.dumps(details)),
            )
            conn.execute(
                "INSERT INTO positions(id,proposal_id,symbol,side,quantity,entry_price,current_price,"
                "stop_loss,take_profit,status,opened_at) VALUES (?,?,?,?,?,?,?,?,?,'open',?)",
                (position_id, proposal_id, body["symbol"], body["side"], quantity,
                 body["entry_price"], body["entry_price"], body["stop_loss"],
                 body["take_profit"], now),
            )
            conn.execute(
                "UPDATE proposals SET status='approved',updated_at=? WHERE id=?",
                (now, proposal_id),
            )
            conn.commit()
            return {"order_id": order_id, "position_id": position_id, "status": "filled",
                    "mode": "paper", "quantity": quantity, "simulated_price": body["entry_price"]}

    def reject_proposal(self, proposal_id: str, version: int) -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            if row is None:
                conn.rollback()
                raise KeyError("Proposal not found")
            if row["version"] != version or row["status"] != "pending":
                conn.rollback()
                raise ValueError("Proposal is stale, already reviewed, or not pending.")
            conn.execute(
                "UPDATE proposals SET status='rejected',updated_at=? WHERE id=?",
                (utc_now(), proposal_id),
            )
            conn.commit()
            return self.get_proposal(proposal_id)  # type: ignore[return-value]

    def list_orders(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [{**dict(r), "details": json.loads(r["details_json"])} for r in rows]

    def close_position(self, position_id: str, exit_price: float) -> dict[str, Any]:
        if exit_price <= 0:
            raise ValueError("Exit price must be positive.")
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()
            if row is None:
                conn.rollback()
                raise KeyError("Position not found")
            if row["status"] != "open":
                conn.rollback()
                raise ValueError("Position is already closed.")
            sign = 1 if row["side"] == "long" else -1
            pnl = (exit_price - row["entry_price"]) * row["quantity"] * sign
            conn.execute(
                "UPDATE positions SET current_price=?,status='closed',closed_at=?,realized_pnl=? WHERE id=?",
                (exit_price, now, pnl, position_id),
            )
            conn.commit()
            return {"position_id": position_id, "status": "closed",
                    "simulated_exit_price": exit_price, "realized_pnl": pnl}

    def mark_position(self, symbol: str, price: float) -> None:
        if price <= 0:
            return
        with self.connect() as conn:
            conn.execute(
                "UPDATE positions SET current_price=? WHERE symbol=? AND status='open'",
                (price, symbol.upper()),
            )
