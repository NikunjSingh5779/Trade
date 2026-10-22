import asyncio
from collections import Counter
from typing import Any

from pydantic import ValidationError

from app.agent.controller import AgentController
from app.agent.schemas import TradeProposal
from app.config import Settings, get_settings
from app.execution.paper_broker import PaperBroker
from app.inference.gate import InferenceGateBusy
from app.market_data.freshness import ensure_fresh_bars
from app.risk.consistency import check_proposal_consistency
from app.storage import Database, utc_now


class QueueWorker:
    def __init__(
        self, db: Database, agent: AgentController, max_pending: int = 100,
        max_stale_data_seconds: int = 600, max_entry_deviation_pct: float = 2.0,
        settings: Settings | None = None, paper_broker: PaperBroker | None = None,
    ):
        self.db = db
        self.agent = agent
        self.settings = settings or get_settings()
        self.paper_broker = paper_broker or PaperBroker(self.settings, db)
        self.max_pending = max_pending
        self.max_stale_data_seconds = max_stale_data_seconds
        self.max_entry_deviation_pct = max_entry_deviation_pct
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            job = self.db.claim_next_job()
            if not job:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=0.5)
                except TimeoutError:
                    pass
                continue
            try:
                result = await self._process(job["task"], job["payload"])
                self.db.finish_job(job["id"], result)
                self.db.add_audit("job.succeeded", {"job_id": job["id"], "task": job["task"]})
            except InferenceGateBusy:
                with self.db.connect() as conn:
                    conn.execute(
                        "UPDATE jobs SET status='queued',updated_at=? WHERE id=?",
                        (utc_now(), job["id"]),
                    )
                await asyncio.sleep(0.5)
            except Exception as exc:
                message = str(exc).replace("\n", " ")[:400]
                self.db.fail_job(job["id"], f"{type(exc).__name__}: {message}")
                self.db.add_audit("job.failed", {
                    "job_id": job["id"], "task": job["task"], "error_type": type(exc).__name__,
                })

    def stop(self) -> None:
        self._stop.set()

    async def _process(self, task: str, payload: dict[str, Any]) -> dict[str, Any]:
        if task == "analyze_proposal":
            timeframe = str(payload.get("timeframe", "intraday"))
            if timeframe != "intraday":
                raise ValueError(
                    "This build accepts intraday proposal jobs only; discard this old queued job "
                    "and submit a new five-minute analysis."
                )
            bars = payload.get("bars", [])
            ensure_fresh_bars(bars, timeframe, self.max_stale_data_seconds)
            result = await self.agent.run(task, payload)
            structured = result.get("structured")
            if isinstance(structured, dict) and not structured.get("no_setup"):
                # The request decides the horizon: a model that omits `timeframe` inherits it,
                # and one that names a different horizon is rejected by the check below.
                proposal = TradeProposal.model_validate({"timeframe": timeframe, **structured})
                check_proposal_consistency(
                    proposal,
                    symbol=payload.get("symbol"),
                    timeframe=timeframe,
                    latest_close=bars[-1].get("close"),
                    max_entry_deviation_pct=self.max_entry_deviation_pct,
                )
                proposal_body = proposal.model_dump()
                proposal_body["data_asof"] = bars[-1].get("timestamp")
                # Server-owned: approval freshness must never depend on the model's own label.
                proposal_body["data_timeframe"] = timeframe
                proposal_body["data_source"] = "configured_market_data_provider"
                saved = self.db.add_proposal(proposal_body)
                result["proposal"] = saved
            elif isinstance(structured, dict) and structured.get("no_setup"):
                result["no_setup_reason"] = str(structured.get("reason", "No setup proposed."))[:1000]
            return result
        result = await self.agent.run(task, payload)
        if task == "market_scan" and payload.get("autonomous_mode") is True:
            self._execute_autonomous_scan(result, payload)
        return result

    def _execute_autonomous_scan(
        self, result: dict[str, Any], payload: dict[str, Any]
    ) -> None:
        """Validate each model decision independently, then route eligible paper entries through PaperBroker."""
        structured = result.get("structured")
        if not isinstance(structured, dict) or not isinstance(structured.get("decisions"), list):
            self.db.add_audit("autonomous_scan.invalid_output", {
                "reason": str(result.get("parse_warning", "Expected JSON object with a decisions array."))[:300],
            })
            result["autonomous_trading"] = {
                "status": "invalid_model_output", "paper_trades_opened": 0,
                "decision_outcomes": [],
            }
            return

        bars_by_symbol = payload.get("bars_by_symbol")
        if not isinstance(bars_by_symbol, dict):
            self.db.add_audit("autonomous_scan.invalid_payload", {"reason": "bars_by_symbol_missing"})
            result["autonomous_trading"] = {
                "status": "invalid_payload", "paper_trades_opened": 0,
                "decision_outcomes": [],
            }
            return

        allowed_symbols = set(self.settings.autonomous_symbol_list)
        supplied_symbols = {
            str(symbol).strip().upper()
            for symbol in payload.get("symbols", [])
            if str(symbol).strip().upper() in allowed_symbols
        }
        decisions = [
            item for item in structured["decisions"] if isinstance(item, dict)
        ]
        counts = Counter(str(item.get("symbol", "")).strip().upper() for item in decisions)
        opened = 0
        skipped = 0
        explicit_no_trade = 0
        decision_outcomes: list[dict[str, Any]] = []

        for decision in decisions:
            symbol = str(decision.get("symbol", "")).strip().upper()
            if symbol not in allowed_symbols or symbol not in supplied_symbols:
                self.db.add_audit("autonomous_scan.symbol_rejected", {
                    "symbol": symbol[:12], "reason": "symbol_not_in_current_autonomous_universe",
                })
                decision_outcomes.append({
                    "symbol": symbol or "UNKNOWN", "decision": "unknown",
                    "status": "skipped", "reason": "Symbol is outside the configured universe or current scan.",
                })
                skipped += 1
                continue
            if counts[symbol] != 1:
                # Ambiguous duplicate decisions for a symbol always fail closed.
                self.db.add_audit("autonomous_scan.symbol_rejected", {
                    "symbol": symbol, "reason": "duplicate_model_decisions",
                })
                decision_outcomes.append({
                    "symbol": symbol, "decision": "ambiguous",
                    "status": "skipped", "reason": "The model returned multiple decisions for this symbol.",
                })
                skipped += 1
                continue

            action = str(decision.get("decision", "")).strip().lower()
            if action == "no_trade":
                reason = str(decision.get("reason", "Model found no defensible setup."))[:300]
                self.db.add_audit("autonomous_scan.no_trade", {
                    "symbol": symbol, "reason": reason,
                })
                decision_outcomes.append({
                    "symbol": symbol, "decision": "no_trade",
                    "status": "no_trade", "reason": reason,
                })
                explicit_no_trade += 1
                continue
            if action != "trade":
                self.db.add_audit("autonomous_scan.symbol_rejected", {
                    "symbol": symbol, "reason": "decision_must_be_trade_or_no_trade",
                })
                decision_outcomes.append({
                    "symbol": symbol, "decision": action[:24],
                    "status": "skipped", "reason": "Decision must be trade or no_trade.",
                })
                skipped += 1
                continue

            saved_proposal: dict[str, Any] | None = None
            try:
                bars = bars_by_symbol.get(symbol)
                if not isinstance(bars, list) or len(bars) < 10:
                    raise ValueError("At least 10 completed candles are required for automated entry.")
                ensure_fresh_bars(bars, "intraday", self.max_stale_data_seconds)
                latest_bar = bars[-1]
                trade_fields = {
                    key: value for key, value in decision.items()
                    if key not in {"decision", "reason"}
                }
                trade_fields["timeframe"] = "intraday"
                proposal = TradeProposal.model_validate(trade_fields)
                check_proposal_consistency(
                    proposal,
                    symbol=symbol,
                    timeframe="intraday",
                    latest_close=latest_bar.get("close"),
                    max_entry_deviation_pct=self.max_entry_deviation_pct,
                )

                portfolio = self.db.portfolio_snapshot(self.settings.paper_initial_equity)
                if any(
                    p.get("symbol") == symbol for p in portfolio.get("open_positions", [])
                ):
                    raise ValueError("An open paper position already exists for this symbol.")
                proposal_body = proposal.model_dump()
                proposal_body["data_asof"] = latest_bar.get("timestamp")
                proposal_body["data_timeframe"] = "intraday"
                proposal_body["data_source"] = "configured_market_data_provider"
                saved_proposal = self.db.add_proposal(proposal_body)
                execution = self.paper_broker.approve(
                    saved_proposal["id"], saved_proposal["version"], automated=True
                )
                self.db.add_audit("autonomous_trade.opened", {
                    "symbol": symbol,
                    "proposal_id": saved_proposal["id"],
                    "position_id": execution["position_id"],
                    "side": proposal.side,
                    "quantity": execution["quantity"],
                    "simulated_price": execution["simulated_price"],
                    "mode": "paper",
                })
                decision_outcomes.append({
                    "symbol": symbol, "decision": "trade", "status": "opened",
                    "side": proposal.side,
                    "quantity": execution["quantity"],
                    "simulated_price": execution["simulated_price"],
                    "execution_mode": execution.get("execution_mode", "local_simulation"),
                    "broker_order_id": execution.get("broker_order_id"),
                    "reason": str(proposal.thesis)[:240],
                })
                opened += 1
            except (ValidationError, ValueError, TypeError, KeyError, RuntimeError) as exc:
                if saved_proposal is not None:
                    try:
                        self.db.reject_proposal(
                            saved_proposal["id"], int(saved_proposal["version"])
                        )
                    except (KeyError, ValueError):
                        pass
                reason = str(exc).replace("\\n", " ")[:300]
                self.db.add_audit("autonomous_trade.skipped", {
                    "symbol": symbol,
                    "proposal_id": saved_proposal.get("id") if saved_proposal else None,
                    "error_type": type(exc).__name__,
                    "reason": reason,
                })
                decision_outcomes.append({
                    "symbol": symbol, "decision": "trade",
                    "status": "skipped", "reason": reason,
                })
                skipped += 1
            except Exception as exc:
                # Isolate failures per symbol so one malformed decision cannot prevent all
                # other valid decisions from being risk-checked.
                if saved_proposal is not None:
                    try:
                        self.db.reject_proposal(
                            saved_proposal["id"], int(saved_proposal["version"])
                        )
                    except Exception:
                        pass
                self.db.add_audit("autonomous_trade.skipped", {
                    "symbol": symbol,
                    "proposal_id": saved_proposal.get("id") if saved_proposal else None,
                    "error_type": type(exc).__name__,
                })
                decision_outcomes.append({
                    "symbol": symbol, "decision": "trade",
                    "status": "skipped", "reason": f"Internal validation/execution error: {type(exc).__name__}.",
                })
                skipped += 1

        missing_symbols = supplied_symbols - set(counts)
        for symbol in sorted(missing_symbols):
            self.db.add_audit("autonomous_scan.no_trade", {
                "symbol": symbol, "reason": "No unambiguous decision returned; failed closed.",
            })
            decision_outcomes.append({
                "symbol": symbol, "decision": "no_trade",
                "status": "no_trade", "reason": "No unambiguous model decision was returned; failed closed.",
            })
            explicit_no_trade += 1

        result["autonomous_trading"] = {
            "status": "processed",
            "symbols_evaluated": len(supplied_symbols),
            "explicit_no_trade": explicit_no_trade,
            "paper_trades_opened": opened,
            "decisions_skipped": skipped,
            "decision_outcomes": decision_outcomes,
        }
