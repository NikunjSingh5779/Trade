import json
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.config import Settings
from app.execution.alpaca_paper import (
    AlpacaOrderOutcomeUnknown, AlpacaOrderRejected, AlpacaPaperError, AlpacaPaperTrading,
)
from app.market_data.freshness import (
    MarketDataFreshnessError, ensure_fresh_bars, max_age_seconds,
  )
from app.risk.validator import RiskRejected, validate_and_size
from app.storage import Database
from app.market_data.sessions import is_regular_us_bar, is_regular_us_session, us_session_bounds

NEW_YORK = ZoneInfo("America/New_York")


class PaperBroker:
    """Local simulation by default; optional Alpaca Paper routing only, never live orders."""

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.alpaca = AlpacaPaperTrading(settings)

    @staticmethod
    def _client_order_id(proposal_id: str) -> str:
        # Stable ID makes an approval retry idempotent at Alpaca as well as in SQLite.
        return ("trade-" + "".join(ch for ch in proposal_id if ch.isalnum() or ch == "-"))[:48]

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
        """Reconcile open holdings and recover entries whose broker response was ambiguous."""
        if not self.settings.alpaca_paper_trading_enabled:
            return {"connected": False, "reconciled": True, "issues": [], "mode": "local_simulation"}

        remote = self.alpaca.positions()
        remote_by_symbol = {str(row.get("symbol", "")).upper(): row for row in remote}
        issues: list[str] = []
        synchronized: list[str] = []
        auto_closed: list[dict[str, Any]] = []
        recovered: list[dict[str, Any]] = []

        # If a POST succeeded at Alpaca but the HTTP response timed out before SQLite
        # committed it, the proposal is still pending. Reconcile using its stable client ID
        # before allowing any further entry for that account.
        with self.db.connect() as conn:
            pending_rows = conn.execute(
                "SELECT i.proposal_id AS id, i.proposal_version AS version, i.client_order_id, "
                "i.status AS intent_status, i.broker_order_id, p.status AS proposal_status, "
                "p.body_json FROM broker_order_intents i "
                "JOIN proposals p ON p.id=i.proposal_id "
                "WHERE i.status IN ('submitting','unknown') ORDER BY i.created_at"
            ).fetchall()

        for pending in pending_rows:
            proposal_id = str(pending["id"])
            # A local row means the fill transaction completed; only the intent status update
            # was interrupted. Reconcile it without another network write.
            with self.db.connect() as conn:
                local_for_proposal = conn.execute(
                    "SELECT id,status FROM positions WHERE proposal_id=? AND execution_mode='alpaca_paper'",
                    (proposal_id,),
                ).fetchone()
            if local_for_proposal is not None:
                # This covers both open and already-closed positions if the app crashed
                # after recording the fill but before updating its intent status.
                self.db.update_broker_order_intent(
                    proposal_id, "recorded", broker_order_id=pending["broker_order_id"]
                )
                continue
            try:
                body = json.loads(pending["body_json"])
                symbol = str(body.get("symbol", "")).upper()
                order = self.alpaca.order_by_client_id(str(pending["client_order_id"]))
            except AlpacaPaperError:
                raise
            except (TypeError, ValueError, AttributeError):
                issues.append(f"Pending broker intent {proposal_id}: invalid proposal data prevents reconciliation")
                continue
            if not order:
                self.db.update_broker_order_intent(
                    proposal_id, "unknown",
                    last_error="No order is currently returned for the durable client order ID.",
                )
                issues.append(
                    f"{symbol}: broker order outcome is still unknown; Alpaca has not returned the "
                    "recorded client order ID. Verify the Alpaca Paper order list before retrying."
                )
                continue

            order_status = str(order.get("status", "")).lower()
            if order_status in {"rejected", "canceled", "expired"}:
                filled_quantity = 0.0
                try:
                    filled_quantity = abs(float(order.get("filled_qty") or 0))
                except (TypeError, ValueError):
                    pass
                if filled_quantity > 0:
                    issues.append(
                        f"{symbol}: Alpaca order ended as {order_status} after a partial fill; "
                        "manual reconciliation is required."
                    )
                    continue
                try:
                    proposal_status = str(pending["proposal_status"])
                    if proposal_status == "pending":
                        rejected = self.db.reject_proposal(proposal_id, int(pending["version"]))
                        proposal_status = str(rejected.get("status"))
                    self.db.update_broker_order_intent(
                        proposal_id, "rejected", broker_order_id=str(order.get("id") or ""),
                        last_error=f"Alpaca broker order status: {order_status}",
                    )
                    self.db.add_audit("alpaca_paper.entry_reconciled_not_filled", {
                        "proposal_id": proposal_id, "symbol": symbol,
                        "broker_order_id": order.get("id"), "broker_status": order_status,
                        "proposal_status": proposal_status,
                    })
                except (KeyError, ValueError):
                    issues.append(f"{symbol}: terminal broker order found but proposal status could not be reconciled")
                continue

            if order_status != "filled":
                self.db.update_broker_order_intent(
                    proposal_id, "unknown", broker_order_id=str(order.get("id") or ""),
                    last_error=f"Alpaca order is not terminal: {order_status or 'unknown status'}",
                )
                issues.append(
                    f"{symbol}: Alpaca entry order is unresolved ({order_status or 'unknown status'}); "
                    "new entries are blocked until it finishes."
                )
                continue

            try:
                quantity = int(float(order.get("filled_qty") or order.get("qty") or 0))
                fill_price = float(order.get("filled_avg_price") or 0)
                order_id = str(order.get("id") or "")
            except (TypeError, ValueError):
                issues.append(f"{symbol}: filled Alpaca order has invalid quantity or price")
                continue
            if quantity < 1 or fill_price <= 0 or not order_id:
                issues.append(f"{symbol}: filled Alpaca order is missing its confirmed quantity/price/ID")
                continue

            local_open = self._local_open_positions()
            conflicting_local = [
                row for row in local_open if str(row.get("symbol", "")).upper() == symbol
            ]
            if conflicting_local:
                issues.append(
                    f"{symbol}: a confirmed Alpaca fill conflicts with an existing local position; "
                    "review before reconciling."
                )
                continue

            broker_position = remote_by_symbol.get(symbol)
            if broker_position is None:
                exit_leg = self._confirmed_bracket_exit({"proposal_id": proposal_id})
                if not exit_leg:
                    issues.append(
                        f"{symbol}: entry order is filled but no broker position or confirmed bracket exit is visible"
                    )
                    continue
                try:
                    exit_price = float(exit_leg["filled_avg_price"])
                    recorded = self.db.record_recovered_alpaca_paper_trade(
                        proposal_id, int(pending["version"]), quantity, fill_price, order_id
                    )
                    closed = self.db.close_position(
                        recorded["position_id"], exit_price,
                        pricing_method_override="alpaca_paper_bracket_exit_fill",
                    )
                    event = {
                        **closed, "symbol": symbol, "reason": str(exit_leg.get("type") or "alpaca_bracket_exit"),
                        "broker_order_id": exit_leg.get("id"), "mode": "alpaca_paper",
                        "recovered_after_ambiguous_response": True,
                    }
                    self.db.add_audit("alpaca_paper.entry_and_exit_reconciled", event)
                    auto_closed.append(event)
                    recovered.append({"symbol": symbol, "position_id": recorded["position_id"], "status": "closed"})
                except (KeyError, ValueError, TypeError) as exc:
                    issues.append(f"{symbol}: could not recover broker entry/exit ({type(exc).__name__})")
                continue

            try:
                remote_quantity = abs(float(broker_position.get("qty", 0)))
                remote_side = str(broker_position.get("side", "")).lower()
                expected_side = "long" if str(body.get("side", "")).lower() == "long" else "short"
                if remote_side != expected_side or abs(remote_quantity - quantity) > 1e-6:
                    issues.append(
                        f"{symbol}: filled order and broker position quantity/side differ; manual review required"
                    )
                    continue
                recorded = self.db.record_recovered_alpaca_paper_trade(
                    proposal_id, int(pending["version"]), quantity, fill_price, order_id
                )
                event = {
                    "symbol": symbol, "position_id": recorded["position_id"],
                    "broker_order_id": order_id, "quantity": quantity,
                    "fill_price": fill_price, "mode": "alpaca_paper",
                }
                self.db.add_audit("alpaca_paper.entry_recovered", event)
                recovered.append({"symbol": symbol, "position_id": recorded["position_id"], "status": "open"})
            except (KeyError, ValueError, TypeError) as exc:
                issues.append(f"{symbol}: could not record confirmed broker entry ({type(exc).__name__})")

        # Reload because the recovery pass can add positions or reconcile bracket exits.
        remote = self.alpaca.positions()
        remote_by_symbol = {str(row.get("symbol", "")).upper(): row for row in remote}
        # Reconcile close requests whose DELETE response timed out. Never send another
        # close when a matching market order may already be open or filled.
        with self.db.connect() as conn:
            pending_closes = conn.execute(
                "SELECT ci.*, p.status AS position_status, p.side, p.quantity, "
                "p.execution_mode, p.entry_price FROM broker_close_intents ci "
                "JOIN positions p ON p.id=ci.position_id "
                "WHERE ci.status IN ('submitting','unknown') ORDER BY ci.created_at"
            ).fetchall()
        for intent_row in pending_closes:
            intent = dict(intent_row)
            position_id = str(intent["position_id"])
            symbol = str(intent["symbol"]).upper()
            if intent["position_status"] != "open":
                self.db.update_broker_close_intent(
                    position_id, "recorded", broker_order_id=intent.get("broker_order_id")
                )
                continue
            try:
                recent_orders = self.alpaca.list_orders(
                    symbol=symbol, after=str(intent["created_at"]), limit=100
                )
            except AlpacaPaperError as exc:
                issues.append(
                    f"{symbol}: could not reconcile an ambiguous close ({type(exc).__name__}); "
                    "do not retry the close until Alpaca Orders is checked."
                )
                continue

            close_order = self._matching_market_close_order(recent_orders, intent)
            if close_order is not None:
                close_status = str(close_order.get("status") or "unknown").lower()
                try:
                    filled_qty = abs(float(close_order.get("filled_qty") or 0))
                    requested_qty = abs(float(close_order.get("qty") or intent["quantity"]))
                except (TypeError, ValueError):
                    filled_qty, requested_qty = 0.0, abs(float(intent["quantity"]))

                if close_status in {"canceled", "rejected", "expired"} and filled_qty <= 0:
                    self.db.update_broker_close_intent(
                        position_id, "failed", broker_order_id=str(close_order.get("id") or ""),
                        last_error=f"Alpaca close order ended as {close_status} without a fill.",
                    )
                    self.db.add_audit("alpaca_paper.close_reconciled_not_filled", {
                        "position_id": position_id, "symbol": symbol,
                        "broker_order_id": close_order.get("id"), "status": close_status,
                    })
                    continue

                if (
                    close_status == "filled" and filled_qty > 0
                    and abs(filled_qty - requested_qty) <= 1e-6
                    and symbol not in remote_by_symbol
                ):
                    try:
                        exit_price = float(close_order.get("filled_avg_price") or 0)
                        if exit_price <= 0:
                            raise ValueError("missing confirmed average fill price")
                        result = self.db.close_position(
                            position_id, exit_price,
                            pricing_method_override="alpaca_paper_broker_fill_recovered",
                        )
                        result.update({
                            "symbol": symbol, "mode": "alpaca_paper",
                            "broker_order_id": close_order.get("id"),
                            "reason": "ambiguous_close_recovered",
                        })
                        self.db.update_broker_close_intent(
                            position_id, "recorded", broker_order_id=str(close_order.get("id") or ""),
                        )
                        self.db.add_audit("alpaca_paper.close_recovered", result)
                        auto_closed.append(result)
                    except (KeyError, ValueError, TypeError) as exc:
                        issues.append(
                            f"{symbol}: a close order appears filled but its local record could not be reconciled "
                            f"({type(exc).__name__})."
                        )
                    continue

                self.db.update_broker_close_intent(
                    position_id, "unknown", broker_order_id=str(close_order.get("id") or ""),
                    last_error=f"Matching close order is not fully reconciled (status={close_status}, filled_qty={filled_qty}).",
                )
                issues.append(
                    f"{symbol}: a matching close order exists in Alpaca (status={close_status}, "
                    f"filled quantity {filled_qty:g}); do not submit another close."
                )
                continue

            if symbol in remote_by_symbol:
                self.db.update_broker_close_intent(
                    position_id, "unknown",
                    last_error="No matching market close order was returned; broker position remains open.",
                )
                issues.append(
                    f"{symbol}: a prior close response is unresolved and no matching close order is visible. "
                    "Verify Alpaca Paper Orders before resolving or retrying."
                )

        local = self._local_open_positions()
        tracked = {
            str(row["symbol"]).upper(): row for row in local
            if row.get("execution_mode") == "alpaca_paper"
        }

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
                    result = self.db.close_position(
                        str(position["id"]), exit_price,
                        pricing_method_override="alpaca_paper_bracket_exit_fill",
                    )
                    event = {
                        **result, "symbol": symbol,
                        "reason": str(exit_leg.get("type") or "alpaca_bracket_exit"),
                        "broker_order_id": exit_leg.get("id"),
                        "mode": "alpaca_paper",
                    }
                    self.db.update_broker_close_intent(
                        str(position["id"]), "recorded",
                        broker_order_id=str(exit_leg.get("id") or ""),
                    )
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
            "recovered_entries": recovered,
            "mode": "alpaca_paper",
        }

    def resolve_unknown_order_intent(
        self, proposal_id: str, *, confirmed_no_order_and_no_position: bool
    ) -> dict[str, Any]:
        """Release a stale unknown intent only after explicit operator confirmation and live Paper API checks."""
        if not self.settings.alpaca_paper_trading_enabled or not self.alpaca.configured:
            raise RiskRejected("Enable and connect Alpaca Paper before resolving a broker intent.")
        if confirmed_no_order_and_no_position is not True:
            raise RiskRejected(
                "Explicit confirmation that the order and position are absent from Alpaca Paper is required."
            )

        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT i.*, p.status AS proposal_status, p.body_json "
                "FROM broker_order_intents i JOIN proposals p ON p.id=i.proposal_id "
                "WHERE i.proposal_id=?",
                (proposal_id,),
            ).fetchone()
        if row is None:
            raise KeyError("No broker order intent exists for this proposal.")
        intent = dict(row)
        if intent["status"] not in {"submitting", "unknown"}:
            raise ValueError(
                f"Intent status is {intent['status']!r}; only submitting/unknown intents can be resolved."
            )

        try:
            created_at = datetime.fromisoformat(
                str(intent["created_at"]).replace("Z", "+00:00")
            )
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
        except (TypeError, ValueError) as exc:
            raise RiskRejected(
                "Broker intent has no valid creation timestamp; it needs manual database review."
            ) from exc
        age_seconds = (datetime.now(UTC) - created_at.astimezone(UTC)).total_seconds()
        if age_seconds < 120:
            raise RiskRejected(
                "Wait at least 120 seconds after the ambiguous request before resolving it; "
                "this reduces the chance of retrying an order whose response is delayed."
            )

        existing_order = self.alpaca.order_by_client_id(str(intent["client_order_id"]))
        if existing_order is not None:
            raise RiskRejected(
                "Alpaca still returns an order for this client ID "
                f"(status={str(existing_order.get('status') or 'unknown')}). "
                "Do not release this intent; let broker reconciliation handle it."
            )

        symbol = str(intent["symbol"]).upper()
        if any(str(position.get("symbol", "")).upper() == symbol
               for position in self.alpaca.positions()):
            raise RiskRejected(
                f"Alpaca Paper still reports a {symbol} position. Do not release this intent."
            )

        for position in self._local_open_positions():
            if str(position.get("symbol", "")).upper() == symbol:
                raise RiskRejected(
                    f"The local ledger still has an open {symbol} position. Review and reconcile it first."
                )

        with self.db.connect() as conn:
            other = conn.execute(
                "SELECT proposal_id FROM broker_order_intents "
                "WHERE symbol=? AND proposal_id<>? AND status IN ('submitting','unknown') LIMIT 1",
                (symbol, proposal_id),
            ).fetchone()
        if other is not None:
            raise RiskRejected(
                f"Another unresolved {symbol} broker intent exists; resolve it first."
            )

        self.db.update_broker_order_intent(
            proposal_id, "resolved_no_order",
            last_error="Operator confirmed no order/position in Alpaca Paper after API checks.",
        )
        self.db.add_audit("alpaca_paper.intent_resolved_no_order", {
            "proposal_id": proposal_id,
            "symbol": symbol,
            "operator_confirmed_no_order_and_no_position": True,
            "age_seconds": int(age_seconds),
        })
        return {
            "proposal_id": proposal_id,
            "symbol": symbol,
            "status": "resolved_no_order",
            "proposal_status": intent["proposal_status"],
            "note": (
                "No order or position was found in Alpaca Paper and the operator confirmed the dashboard check. "
                "A subsequent approval will reuse the same client order ID."
            ),
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
            with self.db.connect() as conn:
                unresolved_rows = conn.execute(
                    "SELECT i.proposal_id, i.symbol, i.status, i.created_at, "
                    "p.status AS proposal_status FROM broker_order_intents i "
                    "JOIN proposals p ON p.id=i.proposal_id "
                    "WHERE i.status IN ('submitting','unknown') ORDER BY i.created_at"
                ).fetchall()
                unresolved_close_rows = conn.execute(
                    "SELECT ci.position_id, ci.symbol, ci.status, ci.created_at, "
                    "p.side, p.quantity FROM broker_close_intents ci "
                    "JOIN positions p ON p.id=ci.position_id "
                    "WHERE ci.status IN ('submitting','unknown') AND p.status='open' "
                    "ORDER BY ci.created_at"
                ).fetchall()
            unresolved_intents = [dict(row) for row in unresolved_rows]
            unresolved_closes = [dict(row) for row in unresolved_close_rows]
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
                "unresolved_intents": unresolved_intents,
                "unresolved_closes": unresolved_closes,
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

    @staticmethod
    def _matching_market_close_order(
        orders: list[dict[str, Any]], position: dict[str, Any]
    ) -> dict[str, Any] | None:
        expected_side = "sell" if str(position.get("side", "")).lower() == "long" else "buy"
        expected_qty = abs(float(position.get("quantity", 0)))
        matching: list[dict[str, Any]] = []
        for order in orders:
            try:
                side = str(order.get("side", "")).lower()
                order_type = str(order.get("type", "")).lower()
                order_qty = abs(float(order.get("qty") or 0))
            except (TypeError, ValueError):
                continue
            # DELETE /positions creates a market order to flatten the open position.
            # Ignore bracket stop/limit legs and the older parent entry order.
            if (
                side == expected_side and order_type == "market"
                and expected_qty > 0 and abs(order_qty - expected_qty) <= 1e-6
            ):
                matching.append(order)
        if not matching:
            return None
        matching.sort(key=lambda item: str(
            item.get("submitted_at") or item.get("created_at") or item.get("updated_at") or ""
        ))
        return matching[-1]

    def resolve_unknown_close_intent(
        self, position_id: str, *, confirmed_no_close_order_and_position_open: bool
    ) -> dict[str, Any]:
        """Release a stale close intent only after explicit confirmation and broker API checks."""
        if not self.settings.alpaca_paper_trading_enabled or not self.alpaca.configured:
            raise RiskRejected("Enable and connect Alpaca Paper before resolving a close intent.")
        if confirmed_no_close_order_and_position_open is not True:
            raise RiskRejected(
                "Explicit confirmation that no close order exists and the position remains open is required."
            )
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT ci.*, p.status AS position_status, p.side, p.quantity, "
                "p.execution_mode, p.current_price FROM broker_close_intents ci "
                "JOIN positions p ON p.id=ci.position_id WHERE ci.position_id=?",
                (position_id,),
            ).fetchone()
        if row is None:
            raise KeyError("No broker close intent exists for this position.")
        intent = dict(row)
        if intent["status"] not in {"submitting", "unknown"}:
            raise ValueError(
                f"Close intent status is {intent['status']!r}; only submitting/unknown intents can be resolved."
            )
        if intent["position_status"] != "open" or intent["execution_mode"] != "alpaca_paper":
            raise RiskRejected("The local broker-backed position is not open; refresh reconciliation first.")
        try:
            created_at = datetime.fromisoformat(
                str(intent["created_at"]).replace("Z", "+00:00")
            )
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
        except (TypeError, ValueError) as exc:
            raise RiskRejected("Broker close intent has an invalid timestamp; manual review is required.") from exc
        age_seconds = (datetime.now(UTC) - created_at.astimezone(UTC)).total_seconds()
        if age_seconds < 120:
            raise RiskRejected(
                "Wait at least 120 seconds after the ambiguous close before resolving it."
            )

        symbol = str(intent["symbol"]).upper()
        orders = self.alpaca.list_orders(symbol=symbol, after=str(intent["created_at"]), limit=100)
        matching_order = self._matching_market_close_order(orders, intent)
        if matching_order is not None:
            status = str(matching_order.get("status") or "unknown").lower()
            filled_qty = abs(float(matching_order.get("filled_qty") or 0))
            if status in {"canceled", "rejected", "expired"} and filled_qty <= 0:
                self.db.update_broker_close_intent(
                    position_id, "failed", broker_order_id=str(matching_order.get("id") or ""),
                    last_error=f"Alpaca close order ended as {status} with no fill.",
                )
                raise RiskRejected(
                    f"Alpaca shows a terminal close order ({status}) with no fill. You may retry close after refresh."
                )
            raise RiskRejected(
                f"Alpaca shows a matching close order (status={status}); do not release or resubmit it."
            )

        remote_by_symbol = {
            str(item.get("symbol", "")).upper(): item for item in self.alpaca.positions()
        }
        remote = remote_by_symbol.get(symbol)
        if remote is None:
            raise RiskRejected(
                "Alpaca no longer reports this position. Refresh execution status so its confirmed exit can be reconciled."
            )
        try:
            remote_qty = abs(float(remote.get("qty", 0)))
            remote_side = str(remote.get("side", "")).lower()
            local_qty = abs(float(intent["quantity"]))
            local_side = str(intent["side"]).lower()
        except (TypeError, ValueError) as exc:
            raise RiskRejected("Alpaca/local position quantity is invalid; manual review is required.") from exc
        if remote_side != local_side or abs(remote_qty - local_qty) > 1e-6:
            raise RiskRejected("Alpaca/local position size or side differs; do not release the close intent.")

        self.db.update_broker_close_intent(
            position_id, "resolved_no_order",
            last_error="Operator confirmed no close order and position remains open after API checks.",
        )
        self.db.add_audit("alpaca_paper.close_intent_resolved_no_order", {
            "position_id": position_id, "symbol": symbol,
            "operator_confirmed_no_close_order_and_position_open": True,
            "age_seconds": int(age_seconds),
        })
        return {
            "position_id": position_id, "symbol": symbol, "status": "resolved_no_order",
            "note": "No matching close order was found and the broker position remains open. You may now retry Close.",
        }

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
        symbol = str(position["symbol"]).upper()
        if symbol not in remote:
            self.sync_broker_positions()
            raise RiskRejected(
                "Alpaca no longer reports this position. Refresh execution status and reconcile its exit first."
            )

        # Persist a close intent before DELETE /positions. If the response is lost,
        # another click must not send a second liquidation request.
        self.db.register_broker_close_intent(position_id, symbol)
        try:
            order = self.alpaca.close_position(symbol)
        except AlpacaOrderOutcomeUnknown as exc:
            self.db.update_broker_close_intent(
                position_id, "unknown", last_error=str(exc)
            )
            self.db.add_audit("alpaca_paper.close_outcome_unknown", {
                "position_id": position_id, "symbol": symbol,
                "error_type": type(exc).__name__, "reason": str(exc)[:300],
            })
            raise
        except AlpacaOrderRejected as exc:
            self.db.update_broker_close_intent(
                position_id, "failed", last_error=str(exc)
            )
            raise
        except AlpacaPaperError as exc:
            self.db.update_broker_close_intent(
                position_id, "failed", last_error=str(exc)
            )
            raise

        try:
            exit_price = float(order["filled_avg_price"])
            broker_order_id = str(order["id"])
            if exit_price <= 0 or not broker_order_id:
                raise ValueError("invalid fill")
        except (KeyError, TypeError, ValueError) as exc:
            self.db.update_broker_close_intent(
                position_id, "unknown",
                broker_order_id=str(order.get("id") or ""),
                last_error="Alpaca close response omitted a valid confirmed fill price/order ID.",
            )
            raise AlpacaOrderOutcomeUnknown(
                "Alpaca close may have executed but did not return a valid confirmed fill. "
                "Do not retry; reconcile the Paper Orders list first."
            ) from exc

        result = self.db.close_position(
            position_id, exit_price, pricing_method_override="alpaca_paper_broker_fill"
        )
        result.update({
            "mode": "alpaca_paper", "execution_mode": "alpaca_paper",
            "broker_order_id": broker_order_id, "reason": reason,
        })
        self.db.update_broker_close_intent(
            position_id, "recorded", broker_order_id=broker_order_id
        )
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
            if proposal_record.get("side") == "short" and account.get("shorting_enabled") is False:
                raise RiskRejected(
                    "Short entries are disabled in this Alpaca Paper account configuration."
                )
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
            # Persist the intent before contacting Alpaca. If the HTTP response is lost,
            # a later sync can look up the same client ID rather than creating a duplicate.
            client_order_id = self._client_order_id(proposal_id)
            self.db.register_broker_order_intent(
                proposal_id, version, client_order_id, validated.symbol
            )
            try:
                broker_order = self.alpaca.submit_bracket(
                    symbol=validated.symbol, side=validated.side, quantity=quantity,
                    stop_loss=validated.stop_loss, take_profit=validated.take_profit,
                    client_order_id=client_order_id,
                )
            except AlpacaOrderOutcomeUnknown as exc:
                self.db.update_broker_order_intent(
                    proposal_id, "unknown", last_error=str(exc)
                )
                self.db.add_audit("alpaca_paper.entry_outcome_unknown", {
                    "proposal_id": proposal_id, "symbol": validated.symbol,
                    "client_order_id": client_order_id, "error": str(exc)[:400],
                })
                raise
            except AlpacaOrderRejected as exc:
                self.db.update_broker_order_intent(
                    proposal_id, "rejected", last_error=str(exc)
                )
                try:
                    self.db.reject_proposal(proposal_id, version)
                except (KeyError, ValueError):
                    pass
                raise
            except AlpacaPaperError as exc:
                # Authentication/data errors before a confirmed write can be retried after
                # correcting credentials; no local position is recorded.
                self.db.update_broker_order_intent(
                    proposal_id, "failed", last_error=str(exc)
                )
                raise
            broker_order_id = str(broker_order["id"])
            fill_price = float(broker_order["filled_avg_price"])
            estimated_risk = quantity * abs(fill_price - float(validated.stop_loss))
            if fill_price <= 0:
                self.db.update_broker_order_intent(
                    proposal_id, "unknown", broker_order_id=broker_order_id,
                    last_error="Alpaca returned a non-positive fill price.",
                )
                raise AlpacaOrderOutcomeUnknown(
                    "Alpaca Paper reported a non-positive fill price; broker intent is retained for reconciliation."
                )

        result = self.db.approve_paper_trade(
            proposal_id, version, quantity, self.settings.max_open_positions,
            risk_equity * self.settings.max_gross_exposure_pct / 100,
            simulated_fill_price=fill_price,
            slippage_bps=0 if alpaca_mode else self.settings.simulated_slippage_bps,
            execution_mode=execution_mode,
            broker_order_id=broker_order_id,
        )
        if alpaca_mode:
            self.db.update_broker_order_intent(
                proposal_id, "recorded", broker_order_id=broker_order_id
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
