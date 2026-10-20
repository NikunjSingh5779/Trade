from math import floor
from typing import Any

from app.agent.schemas import TradeProposal


class RiskRejected(ValueError):
    pass


def validate_and_size(
    raw_proposal: dict[str, Any],
    equity: float,
    open_position_count: int,
    total_gross_exposure: float,
    *,
    risk_per_trade_pct: float,
    max_open_positions: int,
    max_position_value_pct: float,
    min_reward_risk: float,
) -> tuple[TradeProposal, int, float]:
    # Database records include lifecycle metadata; validate only the model's proposal fields.
    candidate = {key: raw_proposal[key] for key in TradeProposal.model_fields if key in raw_proposal}
    proposal = TradeProposal.model_validate(candidate)
    if equity <= 0:
        raise RiskRejected("Paper equity must be positive.")
    if open_position_count >= max_open_positions:
        raise RiskRejected("Maximum open positions reached.")

    risk_per_share = abs(proposal.entry_price - proposal.stop_loss)
    reward_per_share = abs(proposal.take_profit - proposal.entry_price)
    if risk_per_share <= 0:
        raise RiskRejected("Stop distance must be positive.")
    reward_risk = reward_per_share / risk_per_share
    if reward_risk < min_reward_risk:
        raise RiskRejected(
            f"Reward/risk {reward_risk:.2f} is below the configured minimum {min_reward_risk:.2f}."
        )

    risk_budget = equity * risk_per_trade_pct / 100
    quantity_by_risk = floor(risk_budget / risk_per_share)
    max_gross_exposure = equity * max_position_value_pct / 100
    exposure_remaining = max(0.0, max_gross_exposure - total_gross_exposure)
    quantity_by_exposure = floor(exposure_remaining / proposal.entry_price)
    quantity = min(quantity_by_risk, quantity_by_exposure)
    if quantity < 1:
        raise RiskRejected("Risk budget or gross-exposure limit is too small for one share.")
    estimated_risk = quantity * risk_per_share
    return proposal, quantity, estimated_risk
