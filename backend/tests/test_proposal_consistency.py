import pytest

from app.agent.schemas import TradeProposal
from app.risk.consistency import ProposalMismatch, check_proposal_consistency


def _proposal(**overrides):
    body = {
        "symbol": "AAPL", "side": "long", "entry_price": 100.0, "stop_loss": 98.0,
        "take_profit": 104.0, "thesis": "Trend and momentum align from supplied candles.",
        "evidence": ["latest close: 100"], "strategy_id": "unit", "timeframe": "intraday",
    }
    return TradeProposal.model_validate({**body, **overrides})


def _check(proposal, **overrides):
    kwargs = {
        "symbol": "AAPL", "timeframe": "intraday", "latest_close": 100.0,
        "max_entry_deviation_pct": 2.0,
    }
    check_proposal_consistency(proposal, **{**kwargs, **overrides})


def test_matching_proposal_passes():
    _check(_proposal())


def test_request_symbol_is_normalized_before_comparison():
    _check(_proposal(), symbol=" aapl ")


def test_proposal_for_a_different_symbol_is_rejected():
    with pytest.raises(ProposalMismatch, match="MSFT but AAPL was requested"):
        _check(_proposal(symbol="MSFT"))


@pytest.mark.parametrize("requested", [None, "", "   "])
def test_missing_request_symbol_fails_closed(requested):
    with pytest.raises(ProposalMismatch, match="no symbol"):
        _check(_proposal(), symbol=requested)


def test_proposal_for_a_different_timeframe_is_rejected():
    with pytest.raises(ProposalMismatch, match="setup intraday but the request was for swing"):
        _check(_proposal(timeframe="intraday"), timeframe="swing")


def test_invented_price_far_from_the_latest_close_is_rejected():
    far = _proposal(entry_price=250.0, stop_loss=245.0, take_profit=260.0)
    with pytest.raises(ProposalMismatch, match="invented prices"):
        _check(far, latest_close=339.66)


def test_entry_deviation_limit_applies_on_both_sides_of_the_close():
    _check(_proposal(entry_price=101.9, stop_loss=99.0, take_profit=105.0))
    _check(_proposal(side="short", entry_price=98.1, stop_loss=101.0, take_profit=95.0))
    with pytest.raises(ProposalMismatch):
        _check(_proposal(entry_price=102.1, stop_loss=99.0, take_profit=105.0))
    with pytest.raises(ProposalMismatch):
        _check(_proposal(side="short", entry_price=97.9, stop_loss=101.0, take_profit=95.0))


@pytest.mark.parametrize("close", [None, 0, -5, "abc", float("nan"), float("inf")])
def test_unusable_latest_close_fails_closed(close):
    with pytest.raises(ProposalMismatch, match="No valid latest close"):
        _check(_proposal(), latest_close=close)
