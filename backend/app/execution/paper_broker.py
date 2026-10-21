from datetime import UTC, datetime
from typing import Any

from app.config import Settings
from app.market_data.freshness import (
    MarketDataFreshnessError, ensure_fresh_bars, max_age_seconds,
  )
from app.risk.validator import RiskRejected, validate_and_size
from app.storage import Database


class PaperBroker:
    """Local simulated broker. Intentionally contains no live-order methods."""

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db

    def approve(self, proposal_id: str, version: int) -> dict[str, Any]:
        if not self.settings.paper_trading_only:
            raise RuntimeError("Safety invariant violated: only paper mode is supported.")
        if self.settings.live_trading_enabled or self.settings.allow_live_order_endpoints:
            raise RuntimeError("Live trading configuration is forbidden in this build.")
        if not self.settings.require_approval_for_new_positions:
            raise RuntimeError("Phase 1 requires user approval for every new position.")

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
        bar_timeframe = "1Day" if data_timeframe == "swing" else "5Min"
        latest_bars = self.db.get_bars(
            proposal_record["symbol"], limit=1, timeframe=bar_timeframe
        )
        if not latest_bars:
            raise RiskRejected("No stored market bar is available to price this paper fill.")
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
        with self.db.connect() as conn:
            day_start = datetime.now(UTC).date().isoformat()
            row = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl),0) FROM positions "
                "WHERE status='closed' AND substr(closed_at,1,10)=?",
                (day_start,),
            ).fetchone()
            daily_realized = float(row[0])
        daily_marked_pnl = daily_realized + float(portfolio["unrealized_pnl"])
        max_daily_loss = portfolio["initial_equity"] * self.settings.daily_loss_limit_pct / 100
        if daily_marked_pnl <= -max_daily_loss:
            raise RiskRejected(
                "Daily paper loss limit reached; new positions are blocked for the current UTC day."
            )

        current_positions = portfolio["open_positions"]
        validated, quantity, estimated_risk = validate_and_size(
            priced_proposal,
            portfolio["equity"],
            len(current_positions),
            portfolio["gross_exposure"],
            risk_per_trade_pct=self.settings.risk_per_trade_pct,
            max_open_positions=self.settings.max_open_positions,
            max_position_value_pct=self.settings.max_position_value_pct,
            max_gross_exposure_pct=self.settings.max_gross_exposure_pct,
            min_reward_risk=self.settings.min_reward_risk,
        )
        result = self.db.approve_paper_trade(
            proposal_id, version, quantity, self.settings.max_open_positions,
            portfolio["equity"] * self.settings.max_gross_exposure_pct / 100,
            simulated_fill_price=fill_price,
            slippage_bps=self.settings.simulated_slippage_bps,
        )
        self.db.add_audit("paper_trade.approved", {
            "proposal_id": proposal_id, "version": version, "symbol": validated.symbol,
            "quantity": quantity, "estimated_risk": estimated_risk, "order_id": result["order_id"],
            "market_mark": market_price, "simulated_fill_price": fill_price,
            "slippage_bps": self.settings.simulated_slippage_bps,
            "fill_bar_timestamp": latest_bar["timestamp"],
        })
        return {**result, "estimated_risk": estimated_risk, "risk_budget": portfolio["equity"] *
                self.settings.risk_per_trade_pct / 100}

    def reject(self, proposal_id: str, version: int) -> dict[str, Any]:
        record = self.db.reject_proposal(proposal_id, version)
        self.db.add_audit("proposal.rejected", {"proposal_id": proposal_id, "version": version})
        return record
