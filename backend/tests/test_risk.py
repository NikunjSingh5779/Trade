import pytest

from app.risk.validator import RiskRejected, validate_and_size

GOOD = {
    "symbol": "AAPL", "side": "long", "entry_price": 100.0, "stop_loss": 98.0,
    "take_profit": 104.0, "thesis": "Trend and momentum align based on supplied candles.",
    "evidence": ["Latest close: 100"], "strategy_id": "test", "timeframe": "intraday",
}


def validate(raw=GOOD):
    return validate_and_size(
        raw, 100000, 0, 0, risk_per_trade_pct=0.25,
        max_open_positions=5, max_position_value_pct=20, min_reward_risk=1,
    )


def test_position_sizing_is_deterministic():
    proposal, quantity, risk = validate()
    assert proposal.symbol == "AAPL"
    assert quantity == 125
    assert risk == 250


def test_wrong_stop_direction_is_rejected():
    bad = {**GOOD, "stop_loss": 102.0}
    with pytest.raises(ValueError):
        validate(bad)


def test_minimum_reward_risk_is_enforced():
    bad = {**GOOD, "take_profit": 101.0}
    with pytest.raises(RiskRejected):
        validate(bad)



def test_per_position_and_gross_exposure_caps_are_independent():
    proposal, quantity, risk = validate_and_size(
        GOOD, 100000, 0, 0, risk_per_trade_pct=0.25,
        max_open_positions=5, max_position_value_pct=10,
        max_gross_exposure_pct=20, min_reward_risk=1,
    )
    assert quantity == 100
    assert quantity * proposal.entry_price <= 10000
    assert risk == 200


def test_remaining_gross_exposure_caps_new_position_separately():
    _, quantity, _ = validate_and_size(
        GOOD, 100000, 1, 19000, risk_per_trade_pct=0.25,
        max_open_positions=5, max_position_value_pct=20,
        max_gross_exposure_pct=20, min_reward_risk=1,
    )
    assert quantity == 10
