from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class Database:
    """Small SQLite repository. Each operation owns a short-lived connection."""

    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Open a short-lived connection and always close it, including on exceptions."""
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

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
                    realized_pnl REAL NOT NULL DEFAULT 0,
                    timeframe TEXT NOT NULL DEFAULT 'intraday',
                    last_processed_bar_at TEXT,
                    execution_mode TEXT NOT NULL DEFAULT 'local_simulation'
                );
                CREATE INDEX IF NOT EXISTS ix_positions_status ON positions(status);
                CREATE TABLE IF NOT EXISTS broker_order_intents (
                    proposal_id TEXT PRIMARY KEY, proposal_version INTEGER NOT NULL,
                    client_order_id TEXT NOT NULL UNIQUE, symbol TEXT NOT NULL,
                    status TEXT NOT NULL, broker_order_id TEXT, last_error TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_broker_order_intents_status
                    ON broker_order_intents(status, updated_at);
                CREATE TABLE IF NOT EXISTS broker_close_intents (
                    position_id TEXT PRIMARY KEY, symbol TEXT NOT NULL,
                    status TEXT NOT NULL, broker_order_id TEXT, last_error TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_broker_close_intents_status
                    ON broker_close_intents(status, updated_at);
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
            # Migrate databases created before position-specific timeframes existed.
            position_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(positions)").fetchall()
            }
            if "timeframe" not in position_columns:
                conn.execute(
                    "ALTER TABLE positions ADD COLUMN timeframe TEXT NOT NULL DEFAULT 'intraday'"
                )
            if "last_processed_bar_at" not in position_columns:
                conn.execute(
                    "ALTER TABLE positions ADD COLUMN last_processed_bar_at TEXT"
                )
            if "execution_mode" not in position_columns:
                conn.execute(
                    "ALTER TABLE positions ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'local_simulation'"
                )
            legacy_positions = conn.execute(
                "SELECT p.id, pr.body_json FROM positions p "
                "JOIN proposals pr ON pr.id=p.proposal_id"
            ).fetchall()
            for legacy in legacy_positions:
                try:
                    recorded_timeframe = json.loads(legacy["body_json"]).get("timeframe")
                except (TypeError, json.JSONDecodeError):
                    recorded_timeframe = None
                if recorded_timeframe in {"intraday", "swing"}:
                    conn.execute(
                        "UPDATE positions SET timeframe=? WHERE id=?",
                        (recorded_timeframe, legacy["id"]),
                    )

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

    def list_open_position_backfill_starts(self, timeframe: str) -> list[dict[str, str]]:
        """Return each open position's durable exit checkpoint (or its entry time)."""
        if timeframe not in {"intraday", "swing"}:
            raise ValueError("Unsupported position timeframe.")
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT symbol, execution_mode, COALESCE(last_processed_bar_at, opened_at) AS start_time "
                "FROM positions WHERE status='open' AND timeframe=? ORDER BY symbol",
                (timeframe,),
            ).fetchall()
            return [{"symbol": row["symbol"], "start_time": row["start_time"],
                     "execution_mode": row["execution_mode"]} for row in rows]

    def list_open_position_symbols(self, timeframe: str | None = None) -> list[str]:
        with self.connect() as conn:
            if timeframe is None:
                rows = conn.execute(
                    "SELECT DISTINCT symbol FROM positions WHERE status='open' ORDER BY symbol"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT DISTINCT symbol FROM positions WHERE status='open' AND timeframe=? ORDER BY symbol",
                    (timeframe,),
                ).fetchall()
            return [row["symbol"] for row in rows]

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
            conn.execute("BEGIN IMMEDIATE")
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
            conn.commit()
        return count

    def get_bars(
        self, symbol: str, limit: int = 100, timeframe: str = "5Min"
    ) -> list[dict[str, Any]]:
        """Return only bars matching the requested timeframe, oldest first."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT symbol,timestamp,open,high,low,close,volume,source FROM bars "
                "WHERE symbol=? AND timeframe=? ORDER BY timestamp DESC LIMIT ?",
                (symbol.upper(), timeframe, limit),
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
        max_exposure_value: float, *, simulated_fill_price: float | None = None,
        slippage_bps: float = 0.0, execution_mode: str = "local_simulation",
        broker_order_id: str | None = None,
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
            fill_price = float(
                simulated_fill_price if simulated_fill_price is not None else body["entry_price"]
            )
            if not fill_price > 0:
                conn.rollback()
                raise ValueError("Simulated fill price must be positive.")
            gross_exposure = sum(r["quantity"] * r["current_price"] for r in open_positions)
            new_exposure = quantity * fill_price
            if gross_exposure + new_exposure > max_exposure_value:
                conn.rollback()
                raise ValueError("Exposure limit would be exceeded.")
            order_id, position_id = new_id("ord"), new_id("pos")
            if execution_mode not in {"local_simulation", "alpaca_paper"}:
                conn.rollback()
                raise ValueError("Unsupported paper execution mode.")
            details = {
                "proposal_id": proposal_id,
                "proposal_version": version,
                "requested_entry_price": float(body["entry_price"]),
                "fill_price": fill_price,
                "execution_mode": execution_mode,
                "broker_order_id": broker_order_id,
                "fill_source": (
                    "alpaca_paper_broker_confirmed_fill"
                    if execution_mode == "alpaca_paper"
                    else "latest_server_stored_bar_close_with_adverse_slippage"
                ),
                "slippage_bps": float(slippage_bps),
                "note": (
                    "Alpaca Paper Trading simulated brokerage fill."
                    if execution_mode == "alpaca_paper"
                    else "Local paper simulation; not a broker order or fill."
                ),
            }
            conn.execute(
                "INSERT INTO orders(id,proposal_id,symbol,side,quantity,status,simulated_price,created_at,details_json) "
                "VALUES (?,?,?,?,?,'filled',?,?,?)",
                (order_id, proposal_id, body["symbol"], body["side"], quantity,
                 fill_price, now, json.dumps(details)),
            )
            conn.execute(
                "INSERT INTO positions(id,proposal_id,symbol,side,quantity,entry_price,current_price,"
                "stop_loss,take_profit,status,opened_at,timeframe,execution_mode) "
                "VALUES (?,?,?,?,?,?,?,?,?,'open',?,?,?)",
                (position_id, proposal_id, body["symbol"], body["side"], quantity,
                 fill_price, fill_price, body["stop_loss"], body["take_profit"], now,
                 body.get("timeframe", "intraday"), execution_mode),
            )
            conn.execute(
                "UPDATE proposals SET status='approved',updated_at=? WHERE id=?",
                (now, proposal_id),
            )
            conn.commit()
            return {"order_id": order_id, "position_id": position_id, "status": "filled",
                    "mode": "alpaca_paper" if execution_mode == "alpaca_paper" else "paper",
                    "execution_mode": execution_mode, "quantity": quantity,
                    "simulated_price": fill_price, "fill_price": fill_price,
                    "broker_order_id": broker_order_id,
                    "requested_entry_price": float(body["entry_price"]),
                    "slippage_bps": float(slippage_bps)}

    def register_broker_order_intent(
        self, proposal_id: str, version: int, client_order_id: str, symbol: str
    ) -> dict[str, Any]:
        """Durably record submission intent before contacting Alpaca."""
        if not client_order_id.strip() or not symbol.strip():
            raise ValueError("Broker submission requires a client order ID and symbol.")
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            proposal = conn.execute(
                "SELECT version,status FROM proposals WHERE id=?", (proposal_id,)
            ).fetchone()
            if proposal is None:
                conn.rollback()
                raise KeyError("Proposal not found.")
            if proposal["version"] != version or proposal["status"] != "pending":
                conn.rollback()
                raise ValueError("Proposal is stale, already reviewed, or not pending.")
            existing = conn.execute(
                "SELECT * FROM broker_order_intents WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if existing is not None:
                if existing["client_order_id"] != client_order_id:
                    conn.rollback()
                    raise ValueError("Proposal already has a different broker order intent.")
                if existing["status"] in {"failed", "resolved_no_order"}:
                    conn.execute(
                        "UPDATE broker_order_intents SET status='submitting',last_error=NULL,updated_at=? "
                        "WHERE proposal_id=?",
                        (now, proposal_id),
                    )
                    existing = conn.execute(
                        "SELECT * FROM broker_order_intents WHERE proposal_id=?", (proposal_id,)
                    ).fetchone()
                conn.commit()
                return dict(existing)
            conn.execute(
                "INSERT INTO broker_order_intents(proposal_id,proposal_version,client_order_id,"
                "symbol,status,created_at,updated_at) VALUES (?,?,?,?, 'submitting',?,?)",
                (proposal_id, version, client_order_id, symbol.upper(), now, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM broker_order_intents WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            return dict(row)

    def update_broker_order_intent(
        self, proposal_id: str, status: str, *,
        broker_order_id: str | None = None, last_error: str | None = None,
    ) -> None:
        allowed = {"submitting", "unknown", "recorded", "recovered", "rejected", "failed", "resolved_no_order"}
        if status not in allowed:
            raise ValueError("Unsupported broker order intent status.")
        with self.connect() as conn:
            conn.execute(
                "UPDATE broker_order_intents SET status=?, "
                "broker_order_id=COALESCE(?,broker_order_id), last_error=?, updated_at=? "
                "WHERE proposal_id=?",
                (status, broker_order_id, last_error[:500] if last_error else None,
                 utc_now(), proposal_id),
            )

    def register_broker_close_intent(self, position_id: str, symbol: str) -> dict[str, Any]:
        """Persist a close intent before a DELETE /positions request."""
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            position = conn.execute(
                "SELECT status,execution_mode,symbol FROM positions WHERE id=?", (position_id,)
            ).fetchone()
            if position is None:
                conn.rollback()
                raise KeyError("Position not found.")
            if position["status"] != "open" or position["execution_mode"] != "alpaca_paper":
                conn.rollback()
                raise ValueError("Only an open Alpaca Paper position can create a broker close intent.")
            if str(position["symbol"]).upper() != symbol.upper():
                conn.rollback()
                raise ValueError("Close-intent symbol does not match the local broker position.")
            existing = conn.execute(
                "SELECT * FROM broker_close_intents WHERE position_id=?", (position_id,)
            ).fetchone()
            if existing is not None:
                if existing["status"] in {"submitting", "unknown"}:
                    conn.rollback()
                    raise ValueError(
                        "A prior Alpaca Paper close has an unresolved result. Check Alpaca Orders "
                        "before attempting another close."
                    )
                if existing["status"] in {"failed", "resolved_no_order"}:
                    conn.execute(
                        "UPDATE broker_close_intents SET status='submitting',broker_order_id=NULL,"
                        "last_error=NULL,created_at=?,updated_at=? WHERE position_id=?",
                        (now, now, position_id),
                    )
                else:
                    conn.rollback()
                    raise ValueError(
                        f"Close-intent status is {existing['status']!r}; review broker state before retrying."
                    )
            else:
                conn.execute(
                    "INSERT INTO broker_close_intents(position_id,symbol,status,created_at,updated_at) "
                    "VALUES (?,?, 'submitting',?,?)",
                    (position_id, symbol.upper(), now, now),
                )
            conn.commit()
            return dict(conn.execute(
                "SELECT * FROM broker_close_intents WHERE position_id=?", (position_id,)
            ).fetchone())

    def update_broker_close_intent(
        self, position_id: str, status: str, *,
        broker_order_id: str | None = None, last_error: str | None = None,
    ) -> None:
        allowed = {
            "submitting", "unknown", "recorded", "failed", "resolved_no_order",
        }
        if status not in allowed:
            raise ValueError("Unsupported broker close-intent status.")
        with self.connect() as conn:
            conn.execute(
                "UPDATE broker_close_intents SET status=?, "
                "broker_order_id=COALESCE(?,broker_order_id), last_error=?, updated_at=? "
                "WHERE position_id=?",
                (status, broker_order_id, last_error[:500] if last_error else None,
                 utc_now(), position_id),
            )

    def record_recovered_alpaca_paper_trade(
        self, proposal_id: str, version: int, quantity: int, fill_price: float,
        broker_order_id: str,
    ) -> dict[str, Any]:
        """Reconcile a confirmed broker fill that occurred before its local commit.

        This records an already-existing Alpaca Paper fill; it never submits an order.
        """
        if quantity < 1 or not isinstance(fill_price, (int, float)) or fill_price <= 0:
            raise ValueError("Recovered Alpaca fill quantity and price must be positive.")
        if not broker_order_id.strip():
            raise ValueError("A broker order ID is required to recover a fill.")

        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            proposal_row = conn.execute(
                "SELECT * FROM proposals WHERE id=?", (proposal_id,)
            ).fetchone()
            if proposal_row is None:
                conn.rollback()
                raise KeyError("Proposal not found for recovered Alpaca fill.")
            if proposal_row["version"] != version:
                conn.rollback()
                raise ValueError("Proposal version changed; recovered broker fill needs manual review.")
            existing = conn.execute(
                "SELECT * FROM positions WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if existing is not None:
                conn.commit()
                return {"position_id": existing["id"], "status": existing["status"],
                        "mode": "alpaca_paper", "already_recorded": True}
            if proposal_row["status"] != "pending":
                conn.rollback()
                raise ValueError("Proposal is no longer pending; recovered fill needs manual review.")

            body = json.loads(proposal_row["body_json"])
            if body.get("timeframe", "intraday") != "intraday":
                conn.rollback()
                raise ValueError("Only intraday fills can be recovered automatically.")
            conflict = conn.execute(
                "SELECT id FROM positions WHERE symbol=? AND status='open'",
                (str(body["symbol"]).upper(),),
            ).fetchone()
            if conflict is not None:
                conn.rollback()
                raise ValueError("An open local position conflicts with the recovered broker fill.")

            order_id, position_id = new_id("ord"), new_id("pos")
            details = {
                "proposal_id": proposal_id,
                "proposal_version": version,
                "requested_entry_price": float(body["entry_price"]),
                "fill_price": float(fill_price),
                "execution_mode": "alpaca_paper",
                "broker_order_id": broker_order_id,
                "fill_source": "alpaca_paper_broker_confirmed_fill_recovered_after_ambiguous_response",
                "recovered_after_ambiguous_response": True,
                "note": "This records an existing Alpaca Paper fill; no new broker order was submitted.",
            }
            conn.execute(
                "INSERT INTO orders(id,proposal_id,symbol,side,quantity,status,simulated_price,created_at,details_json) "
                "VALUES (?,?,?,?,?,'filled',?,?,?)",
                (order_id, proposal_id, body["symbol"], body["side"], quantity,
                 float(fill_price), now, json.dumps(details)),
            )
            conn.execute(
                "INSERT INTO positions(id,proposal_id,symbol,side,quantity,entry_price,current_price,"
                "stop_loss,take_profit,status,opened_at,timeframe,execution_mode) "
                "VALUES (?,?,?,?,?,?,?,?,?,'open',?,?,'alpaca_paper')",
                (position_id, proposal_id, body["symbol"], body["side"], quantity,
                 float(fill_price), float(fill_price), float(body["stop_loss"]),
                 float(body["take_profit"]), now, "intraday"),
            )
            conn.execute(
                "UPDATE proposals SET status='approved',updated_at=? WHERE id=?",
                (now, proposal_id),
            )
            conn.execute(
                "UPDATE broker_order_intents SET status='recovered',broker_order_id=?,"
                "last_error=NULL,updated_at=? WHERE proposal_id=?",
                (broker_order_id, now, proposal_id),
            )
            conn.commit()
            return {
                "order_id": order_id, "position_id": position_id, "status": "filled",
                "mode": "alpaca_paper", "execution_mode": "alpaca_paper",
                "quantity": quantity, "fill_price": float(fill_price),
                "broker_order_id": broker_order_id, "recovered": True,
            }

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

    def close_position(
        self, position_id: str, exit_price: float | None = None, *,
        pricing_method_override: str | None = None,
    ) -> dict[str, Any]:
        """Close a paper position; None uses the server mark, never a client-supplied price."""
        if exit_price is not None and (not isinstance(exit_price, (int, float)) or exit_price <= 0):
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
            pricing_method = pricing_method_override or "explicit_test_price"
            if exit_price is None:
                exit_price = float(row["current_price"])
                pricing_method = pricing_method_override or "last_recorded_server_mark"
            sign = 1 if row["side"] == "long" else -1
            pnl = (float(exit_price) - row["entry_price"]) * row["quantity"] * sign
            bar_timeframe = "1Day" if row["timeframe"] == "swing" else "5Min"
            bar_row = conn.execute(
                "SELECT timestamp FROM bars WHERE symbol=? AND timeframe=? ORDER BY timestamp DESC LIMIT 1",
                (row["symbol"], bar_timeframe),
            ).fetchone()
            conn.execute(
                "UPDATE positions SET current_price=?,status='closed',closed_at=?,realized_pnl=? WHERE id=?",
                (float(exit_price), now, pnl, position_id),
            )
            conn.commit()
            return {"position_id": position_id, "status": "closed",
                    "simulated_exit_price": float(exit_price), "realized_pnl": pnl,
                    "pricing_method": pricing_method,
                    "mark_timestamp": bar_row["timestamp"] if bar_row else None,
                    "mode": "paper"}

    def mark_position(self, symbol: str, price: float) -> None:
        if price <= 0:
            return
        with self.connect() as conn:
            conn.execute(
                "UPDATE positions SET current_price=? WHERE symbol=? AND status='open'",
                (price, symbol.upper()),
            )

    def process_position_bar(
        self, bar: dict[str, Any], timeframe: str = "intraday"
    ) -> list[dict[str, Any]]:
        """Process one OHLC bar only for positions using this analysis horizon.

        timeframe is 'intraday' for five-minute bars and 'swing' for daily bars.
        If a bar touches both stop and target without an opening gap resolving the order,
        the stop is chosen first as a conservative assumption. This is paper simulation only.
        """
        if timeframe not in {"intraday", "swing"}:
            raise ValueError("Unsupported paper-position timeframe.")
        symbol = str(bar.get("symbol", "")).strip().upper()
        if not symbol:
            raise ValueError("A symbol is required to process a market bar.")
        try:
            bar_time = datetime.fromisoformat(str(bar["timestamp"]).replace("Z", "+00:00"))
            open_price = float(bar["open"])
            high = float(bar["high"])
            low = float(bar["low"])
            close = float(bar["close"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Market bar is missing valid OHLC values or timestamp.") from exc
        if bar_time.tzinfo is None:
            bar_time = bar_time.replace(tzinfo=UTC)
        bar_time = bar_time.astimezone(UTC)
        if not all(price > 0 for price in (open_price, high, low, close)):
            raise ValueError("Market bar prices must be positive.")
        if low > min(open_price, close) or high < max(open_price, close) or low > high:
            raise ValueError("Market bar has inconsistent OHLC values.")

        exits: list[dict[str, Any]] = []
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM positions WHERE symbol=? AND status='open' AND timeframe=?",
                (symbol, timeframe),
            ).fetchall()
            for row in rows:
                try:
                    opened_at = datetime.fromisoformat(row["opened_at"].replace("Z", "+00:00"))
                except (AttributeError, ValueError):
                    # Fail closed if this legacy position has no trustworthy entry time.
                    continue
                if opened_at.tzinfo is None:
                    opened_at = opened_at.replace(tzinfo=UTC)
                if bar_time <= opened_at.astimezone(UTC):
                    # Never replay pre-entry candles against a newly opened position.
                    continue
                last_processed = row["last_processed_bar_at"]
                if last_processed:
                    try:
                        checkpoint = datetime.fromisoformat(last_processed.replace("Z", "+00:00"))
                    except ValueError:
                        checkpoint = None
                    if checkpoint is not None:
                        if checkpoint.tzinfo is None:
                            checkpoint = checkpoint.replace(tzinfo=UTC)
                        if bar_time <= checkpoint.astimezone(UTC):
                            continue

                stop = float(row["stop_loss"])
                target = float(row["take_profit"])
                side = row["side"]
                exit_price: float | None = None
                reason: str | None = None

                if side == "long":
                    if open_price <= stop:
                        exit_price, reason = open_price, "stop_loss_gap"
                    elif open_price >= target:
                        exit_price, reason = open_price, "take_profit_gap"
                    elif low <= stop and high >= target:
                        exit_price, reason = stop, "stop_loss_ambiguous_bar"
                    elif low <= stop:
                        exit_price, reason = stop, "stop_loss"
                    elif high >= target:
                        exit_price, reason = target, "take_profit"
                elif side == "short":
                    if open_price >= stop:
                        exit_price, reason = open_price, "stop_loss_gap"
                    elif open_price <= target:
                        exit_price, reason = open_price, "take_profit_gap"
                    elif high >= stop and low <= target:
                        exit_price, reason = stop, "stop_loss_ambiguous_bar"
                    elif high >= stop:
                        exit_price, reason = stop, "stop_loss"
                    elif low <= target:
                        exit_price, reason = target, "take_profit"
                else:
                    continue

                if exit_price is None:
                    conn.execute(
                        "UPDATE positions SET current_price=?,last_processed_bar_at=? "
                        "WHERE id=? AND status='open'",
                        (close, bar_time.isoformat(), row["id"]),
                    )
                    continue

                sign = 1 if side == "long" else -1
                pnl = (exit_price - float(row["entry_price"])) * int(row["quantity"]) * sign
                conn.execute(
                    "UPDATE positions SET current_price=?,status='closed',closed_at=?,realized_pnl=?,"
                    "last_processed_bar_at=? WHERE id=? AND status='open'",
                    (exit_price, bar_time.isoformat(), pnl, bar_time.isoformat(), row["id"]),
                )
                exits.append({
                    "position_id": row["id"],
                    "symbol": symbol,
                    "side": side,
                    "quantity": int(row["quantity"]),
                    "entry_price": float(row["entry_price"]),
                    "exit_price": exit_price,
                    "reason": reason,
                    "realized_pnl": pnl,
                    "bar_timestamp": bar_time.isoformat(),
                    "mode": "paper",
                })
            conn.commit()
        return exits
