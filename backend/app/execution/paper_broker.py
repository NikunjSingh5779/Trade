from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.config import Settings
from app.execution.alpaca_paper import AlpacaPaperError, AlpacaPaperTrading
from app.market_data.freshness import (
    MarketDataFreshnessError, ensure_fresh_bars, max_age_seconds,
  )
from app.risk.validator import RiskRejected, validate_and_size
from app.storage import Database
from app.market_data.sessions import is_regular_us_bar, is_regular_us_session, us_session_bounds

NEW_YORK = ZoneInfo("America/New_York")


class PaperBroker:
    """Local simulated broker. Intentionally contains no live-order methods."""

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.alpaca = AlpacaPaperTrading(settings)

    @staticmethod
    def _client_order_id(proposal_id: str) -> str:
        # Stable ID makes an approval retry idempotent at Alpaca as well as in SQLite.
        return ("trade-" + "".join(ch for ch in proposal_id if ch.isalnum() or ch == "-") )[:48]

    def _local_open_positions(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM positions WHERE status='open' ORDER BY opened_at DESC"
            ).fetchall()]

    def _confirmed_bracket_exit(self, position: dict[str, Any]) -> dict[str, Any] | None:
        order = self.alpaca.order_by_client_id(self._client_order_id(str(position["proposal_id"])))
        if not order:
            return None
        legs = order.get("legs")
        if not isinstance(legs, list) and order.get("id"):
            try:
                order = self.alpaca.order_by_id(str(order["id"]))
                legs = order.get("legs")
            except AlpacaPaperError:
                return None
        if not isinstance(legs, list):
            return None
        filled = [leg for leg in legs if isinstance(leg, dict)
                  and str(leg.get("status", "")).lower() == "filled"
                  and leg.get("filled_avg_price")]
        if not filled:
            return None
        filled.sort(key=lambda leg: str(leg.get("filled_at") or leg.get("updated_at") or ""))
        return filled[-1]

    def sync_broker_positions(self) -> dict[str, Any]:
        """Reconcile locally tracked Alpaca positions to Alpaca's paper account truth."""
        if not self.settings.alpaca_paper_trading_enabled:
            return {"connected": False, "reconciled": True, "issues": [], "mode": "local_simulation"}
        remote = self.alpaca.positions()
        remote_by_symbol = {str(row.get("symbol", "")).upper(): row for row in remote}
        local = self._local_open_positions()
        tracked = {str(row["symbol"]).upper(): row for row in local
                   if row.get("execution_mode") == "alpaca_paper"}
        issues: list[str] = []
        synchronized: list[str] = []
        auto_closed: list[dict[str, Any]] = []

        for position in local:
            symbol = str(position["symbol"]).upper()
            if position.get("execution_mode") != "alpaca_paper":
                issues.append(f"{symbol}: local-only simulated position is still open")
                continue
            broker_position = remote_by_symbol.get(symbol)
            if broker_position is None:
                exit_leg = self._confirmed_bracket_exit(position)
                if exit_leg is None:
                    issues.append(f"{symbol}: app tracks an Alpaca position that is absent at the broker")
                    continue
                try:
                    exit_price = float(exit_leg["filled_avg_price"])
                    result = self.db.close_position(str(position["id"]), exit_price)
                    event = {
                        **result, "symbol": symbol,
                        "reason": str(exit_leg.get("type") or "alpaca_bracket_exit"),
                        "broker_order_id": exit_leg.get("id"),
                        "mode": "alpaca_paper",
                    }
                    self.db.add_audit("alpaca_paper.position_reconciled_closed", event)
                    auto_closed.append(event)
                except (KeyError, ValueError, TypeError) as exc:
                    issues.append(f"{symbol}: could not reconcile broker exit ({type(exc).__name__})")
                continue
            try:
                remote_qty = abs(float(broker_position.get("qty", 0)))
                local_qty = abs(float(position["quantity"]))
                remote_side = str(broker_position.get("side", "")).lower()
                local_side = str(position["side"]).lower()
                if remote_side != local_side or abs(remote_qty - local_qty) > 1e-6:
                    issues.append(f"{symbol}: share quantity/side differs between app and Alpaca")
                    continue
                mark = float(broker_position.get("current_price", position["current_price"]))
                with self.db.connect() as conn:
                    conn.execute(
                        "UPDATE positions SET current_price=? WHERE id=? AND status='open'",
                        (mark, position["id"]),
                    )
                synchronized.append(symbol)
            except (TypeError, ValueError):
                issues.append(f"{symbol}: broker position has invalid quantity or mark")

        for symbol in sorted(set(remote_by_symbol) - set(tracked)):
            issues.append(f"{symbol}: Alpaca has a position the app does not track")
        return {
            "connected": True,
            "reconciled": not issues,
            "issues": issues,
            "broker_positions": remote,
            "synchronized_symbols": synchronized,
            "auto_closed": auto_closed,
            "mode": "alpaca_paper",
        }

    def execution_status(self) -> dict[str, Any]:
        if not self.settings.alpaca_paper_trading_enabled:
            local = self._local_open_positions()
            return {
                "mode": "local_simulation", "enabled": False, "connected": False,
                "configured": self.alpaca.configured, "reconciled": True,
                "open_local_simulation_positions": sum(
                    row.get("execution_mode") != "alpaca_paper" for row in local
                ),
                "note": "Local simulation only; its trades never appear in Alpaca.",
            }
        if not self.alpaca.configured:
            return {"mode": "alpaca_paper", "enabled": True, "connected": False,
                    "reconciled": False, "error": "Alpaca Paper API credentials are missing."}
        try:
            reconciliation = self.sync_broker_positions()
            account = self.alpaca.account()
            clean_account = {
                key: account.get(key) for key in (
                    "status", "currency", "equity", "last_equity", "cash", "buying_power",
                    "portfolio_value", "trading_blocked", "account_blocked", "shorting_enabled",
                )
            }
            broker_positions = []
            for position in reconciliation.get("broker_positions", []):
                broker_positions.append({
                    key: position.get(key) for key in (
                        "symbol", "qty", "side", "avg_entry_price", "current_price",
                        "market_value", "cost_basis", "unrealized_pl", "unrealized_plpc",
                    )
                })
            return {
                "mode": "alpaca_paper", "enabled": True, "connected": True,
                "configured": True, "account": clean_account,
                "broker_position_count": len(broker_positions),
                "broker_positions": broker_positions,
                "reconciled": reconciliation["reconciled"],
                "issues": reconciliation["issues"],
                "note": "Orders use the Alpaca Paper API only; no live endpoint is supported.",
            }
        except AlpacaPaperError as exc:
            return {"mode": "alpaca_paper", "enabled": True, "connected": False,
                    "configured": True, "reconciled": False, "error": str(exc)}

    def _require_reconciled_broker(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if not self.settings.alpaca_paper_trading_enabled:
            return {}, []
        reconciliation = self.sync_broker_positions()
        if not reconciliation.get("reconciled"):
            reasons = "; ".join(reconciliation.get("issues", []))
            raise RiskRejected(
                "Alpaca Paper execution is blocked until app and broker positions reconcile: "
                + reasons[:500]
            )
        account = self.alpaca.account()
        status = str(account.get("status", "")).upper()
        if status != "ACTIVE" or account.get("trading_blocked") or account.get("account_blocked"):
            raise RiskRejected("Alpaca Paper account is not active or is blocked from trading.")
        positions = self.alpaca.positions()
        return account, positions

    def close_position(self, position_id: str, *, reason: str = "manual_close") -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()
        if row is None:
            raise KeyError("Position not found")
        position = dict(row)
        if position["status"] != "open":
            raise ValueError("Position is already closed.")
        if position.get("execution_mode") != "alpaca_paper":
            result = self.db.close_position(position_id, None)
            result["mode"] = "paper"
            result["execution_mode"] = "local_simulation"
            self.db.add_audit("paper_position.closed", {**result, "reason": reason})
            return result
        if not self.settings.alpaca_paper_trading_enabled:
            raise RiskRejected("Alpaca Paper execution is disabled; broker-backed position was not closed.")
        if not is_regular_us_session():
            raise AlpacaPaperError(
                "Alpaca Paper market closes are deferred until NYSE regular hours; "
                "the position remains broker-protected and has not been locally closed."
            )
        remote = {str(item.get("symbol", "")).upper(): item for item in self.alpaca.positions()}
        if str(position["symbol"]).upper() not in remote:
            self.sync_broker_positions()
            raise RiskRejected(
                "Alpaca no longer reports this position. Refresh execution status and reconcile its exit first."
            )
        order = self.alpaca.close_position(str(position["symbol"]))
        try:
            exit_price = float(order["filled_avg_price"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AlpacaPaperError(
                "Alpaca returned a close order without a confirmed fill price; local position remains open."
            ) from exc
        result = self.db.close_position(position_id, exit_price)
        result.update({"mode": "alpaca_paper", "execution_mode": "alpaca_paper",
                       "broker_order_id": order.get("id"), "reason": reason})
        self.db.add_audit("alpaca_paper.position_closed", result)
        return result

    def approve(
        self, proposal_id: str, version: int, *, automated: bool = False
    ) -> dict[str, Any]:
        if not self.settings.paper_trading_only:
            raise RuntimeError("Safety invariant violated: only paper mode is supported.")
        if self.settings.live_trading_enabled or self.settings.allow_live_order_endpoints:
            raise RuntimeError("Live trading configuration is forbidden in this build.")
        if automated:
            if not self.settings.autonomous_paper_trading_enabled:
                raise RuntimeError("Autonomous paper trading is disabled.")
        if automated or self.settings.alpaca_paper_trading_enabled:
            now_for_session = datetime.now(UTC)
            if not is_regular_us_session(now_for_session):
                raise RiskRejected("Intraday entries are allowed only during regular US market hours.")
            session = us_session_bounds(now_for_session.astimezone(NEW_YORK).date())
            if session and now_for_session >= session[1] - timedelta(minutes=5):
                raise RiskRejected("New intraday entries are blocked during the final five minutes of the US session.")

        proposal_record = self.db.get_proposal(proposal_id)
        if proposal_record is None:
            raise KeyError("Proposal not found")
        if proposal_record["version"] != version or proposal_record["status"] != "pending":
            raise ValueError("Proposal is stale, already reviewed, or not pending.")

        data_asof = proposal_record.get("data_asof")
        if not isinstance(data_asof, str) or not data_asof.strip():
            raise RiskRejected("Proposal has no verifiable market-data timestamp.")
        try:
            data_time = datetime.fromisoformat(data_asof.replace("Z", "+00:00"))
            if data_time.tzinfo is None:
                data_time = data_time.replace(tzinfo=UTC)
        except ValueError as exc:
            raise RiskRejected("Proposal market-data timestamp is invalid.") from exc
        age_seconds = (datetime.now(UTC) - data_time.astimezone(UTC)).total_seconds()
        if age_seconds < -60:
            raise RiskRejected("Proposal references a future market-data timestamp.")
        # The model's own `timeframe` field is untrusted; only the data timeframe stamped by
        # the server when the proposal was created may choose the staleness limit.
        try:
            max_age = max_age_seconds(
                proposal_record.get("data_timeframe"), self.settings.max_stale_data_seconds
            )
        except MarketDataFreshnessError as exc:
            raise RiskRejected(
                "Proposal has no verifiable data timeframe; re-run the analysis."
            ) from exc
        if age_seconds > max_age:
            raise RiskRejected(
                f"Proposal market data is stale ({int(age_seconds)} seconds); limit is {max_age}."
            )

        # Recheck the current server-stored market mark at approval time. The requested entry
        # is not treated as an automatically achieved fill price.
        data_timeframe = proposal_record.get("data_timeframe")
        if data_timeframe != "intraday" or proposal_record.get("timeframe") != "intraday":
            raise RiskRejected("This build accepts intraday proposals only; regenerate on five-minute candles.")
        if automated and (
            data_timeframe != "intraday"
            or proposal_record.get("timeframe") != "intraday"
            or proposal_record.get("symbol") not in self.settings.autonomous_symbol_list
        ):
            raise RiskRejected(
                "Automated entries must be intraday and use the configured autonomous symbol universe."
            )
        bar_timeframe = "1Day" if data_timeframe == "swing" else "5Min"
        latest_bars = self.db.get_bars(
            proposal_record["symbol"], limit=20 if automated else 1, timeframe=bar_timeframe
        )
        if automated:
            cutoff = datetime.now(UTC) - timedelta(minutes=5)
            completed_bars = []
            for bar in latest_bars:
                try:
                    stamp = datetime.fromisoformat(str(bar["timestamp"]).replace("Z", "+00:00"))
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=UTC)
                except (KeyError, TypeError, ValueError):
                    continue
                if stamp.astimezone(UTC) <= cutoff and is_regular_us_bar(bar):
                    completed_bars.append(bar)
            latest_bars = completed_bars[-1:]
        if not latest_bars:
            raise RiskRejected("No completed regular-session market bar is available to price this paper fill.")
        latest_bar = latest_bars[-1]
        try:
            ensure_fresh_bars(
                [{"timestamp": latest_bar["timestamp"]}],
                data_timeframe,
                self.settings.max_stale_data_seconds,
            )
        except MarketDataFreshnessError as exc:
            raise RiskRejected(
                "Latest market mark is too stale to simulate a fill; refresh market data."
            ) from exc

        market_price = float(latest_bar["close"])
        if market_price <= 0:
            raise RiskRejected("Latest stored market mark is invalid.")
        entry_deviation_pct = abs(float(proposal_record["entry_price"]) - market_price) / market_price * 100
        if entry_deviation_pct > self.settings.max_entry_deviation_pct:
            raise RiskRejected(
                f"Proposal entry is {entry_deviation_pct:.2f}% from the latest market mark; "
                "regenerate the proposal before approval."
            )

        slippage_fraction = self.settings.simulated_slippage_bps / 10000
        if proposal_record["side"] == "long":
            fill_price = market_price * (1 + slippage_fraction)
        else:
            fill_price = market_price * (1 - slippage_fraction)
        priced_proposal = {**proposal_record, "entry_price": fill_price}

        portfolio = self.db.portfolio_snapshot(self.settings.paper_initial_equity)
        alpaca_mode = self.settings.alpaca_paper_trading_enabled
        account: dict[str, Any] = {}
        broker_positions: list[dict[str, Any]] = []
        if alpaca_mode:
            account, broker_positions = self._require_reconciled_broker()
            try:
                portfolio["equity"] = float(account["equity"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RiskRejected("Alpaca Paper account did not return valid equity.") from exc
            portfolio["gross_exposure"] = sum(
                abs(float(item.get("market_value") or (
                    float(item.get("qty", 0)) * float(item.get("current_price", 0))
                )))
                for item in broker_positions
            )
            portfolio["unrealized_pnl"] = sum(
                float(item.get("unrealized_pl") or 0) for item in broker_positions
            )

        # Treat each trading day as a New York calendar day, even across UTC midnight.
        local_now = datetime.now(NEW_YORK)
        day_start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end_local = day_start_local + timedelta(days=1)
        day_start = day_start_local.astimezone(UTC).isoformat()
        day_end = day_end_local.astimezone(UTC).isoformat()
        execution_mode = "alpaca_paper" if alpaca_mode else "local_simulation"
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl),0) FROM positions "
                "WHERE status='closed' AND execution_mode=? AND closed_at>=? AND closed_at<?",
                (execution_mode, day_start, day_end),
            ).fetchone()
            daily_realized = float(row[0])
        daily_marked_pnl = daily_realized + float(portfolio["unrealized_pnl"])
        risk_equity = float(portfolio["equity"])
        max_daily_loss = risk_equity * self.settings.daily_loss_limit_pct / 100
        if daily_marked_pnl <= -max_daily_loss:
            raise RiskRejected(
                "Daily paper loss limit reached; new positions are blocked for the current US trading day."
            )

        current_positions = portfolio["open_positions"]
        validated, quantity, estimated_risk = validate_and_size(
            priced_proposal,
            risk_equity,
            len(current_positions),
            portfolio["gross_exposure"],
            risk_per_trade_pct=self.settings.risk_per_trade_pct,
            max_open_positions=self.settings.max_open_positions,
            max_position_value_pct=self.settings.max_position_value_pct,
            max_gross_exposure_pct=self.settings.max_gross_exposure_pct,
            min_reward_risk=self.settings.min_reward_risk,
        )

        broker_order: dict[str, Any] | None = None
        broker_order_id: str | None = None
        if alpaca_mode:
            # The stable client ID prevents duplicate brokerage orders after a timeout/restart.
            broker_order = self.alpaca.submit_bracket(
                symbol=validated.symbol, side=validated.side, quantity=quantity,
                stop_loss=validated.stop_loss, take_profit=validated.take_profit,
                client_order_id=self._client_order_id(proposal_id),
            )
            broker_order_id = str(broker_order["id"])
            fill_price = float(broker_order["filled_avg_price"])
            estimated_risk = quantity * abs(fill_price - float(validated.stop_loss))
            if fill_price <= 0:
                raise AlpacaPaperError("Alpaca Paper reported a non-positive entry fill price.")

        result = self.db.approve_paper_trade(
            proposal_id, version, quantity, self.settings.max_open_positions,
            risk_equity * self.settings.max_gross_exposure_pct / 100,
            simulated_fill_price=fill_price,
            slippage_bps=0 if alpaca_mode else self.settings.simulated_slippage_bps,
            execution_mode=execution_mode,
            broker_order_id=broker_order_id,
        )
        self.db.add_audit("paper_trade.approved", {
            "proposal_id": proposal_id, "version": version, "symbol": validated.symbol,
            "quantity": quantity, "estimated_risk": estimated_risk, "order_id": result["order_id"],
            "market_mark": market_price, "fill_price": fill_price,
            "execution_mode": execution_mode, "broker_order_id": broker_order_id,
            "slippage_bps": 0 if alpaca_mode else self.settings.simulated_slippage_bps,
            "fill_bar_timestamp": latest_bar["timestamp"],
        })
        return {**result, "estimated_risk": estimated_risk, "risk_budget": portfolio["equity"] *
                self.settings.risk_per_trade_pct / 100}

    def reject(self, proposal_id: str, version: int) -> dict[str, Any]:
        record = self.db.reject_proposal(proposal_id, version)
        self.db.add_audit("proposal.rejected", {"proposal_id": proposal_id, "version": version})
        return record
