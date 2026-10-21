import math
from typing import Any

from app.agent.schemas import TradeProposal


class ProposalMismatch(ValueError):
    """The model's proposal contradicts the request or the market data it was given."""


def check_proposal_consistency(
    proposal: TradeProposal,
    *,
    symbol: Any,
    timeframe: str,
    latest_close: Any,
    max_entry_deviation_pct: float,
) -> None:
    """Reject a model proposal that does not describe what was requested.

    Model output is untrusted. The request, not the model, decides which symbol and horizon
    are analyzed. The paper broker also fills at the proposed entry price, so an entry the
    market has not reached would book fictitious P&L; the entry must stay near the latest close.
    """
    expected = str(symbol or "").strip().upper()
    if not expected or proposal.symbol != expected:
        raise ProposalMismatch(
            f"Model proposed {proposal.symbol} but {expected or 'no symbol'} was requested."
        )
    if proposal.timeframe != timeframe:
        raise ProposalMismatch(
            f"Model labelled its setup {proposal.timeframe} but the request was for {timeframe}."
        )
    try:
        close = float(latest_close)
    except (TypeError, ValueError):
        close = math.nan
    if not math.isfinite(close) or close <= 0:
        raise ProposalMismatch(
            "No valid latest close is available to check the proposed entry against."
        )
    deviation_pct = abs(proposal.entry_price - close) / close * 100
    if deviation_pct > max_entry_deviation_pct:
        raise ProposalMismatch(
            f"Proposed entry {proposal.entry_price:.2f} is {deviation_pct:.1f}% from the latest "
            f"close {close:.2f} (limit {max_entry_deviation_pct:.1f}%); the model may have "
            "invented prices."
        )
