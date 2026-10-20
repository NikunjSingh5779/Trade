from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.backtesting.engine import evaluate_simple_moving_average
from app.market_data.sessions import is_regular_us_session


def test_us_session_weekday_and_weekend():
    ny = ZoneInfo("America/New_York")
    assert is_regular_us_session(datetime(2026, 10, 20, 10, 0, tzinfo=ny))
    assert not is_regular_us_session(datetime(2026, 10, 24, 10, 0, tzinfo=ny))
    assert not is_regular_us_session(datetime(2026, 10, 20, 8, 0, tzinfo=ny))


def test_backtest_requires_enough_bars():
    bars = [{"symbol": "AAPL", "timestamp": str(i), "close": 100 + i} for i in range(5)]
    with pytest.raises(ValueError):
        evaluate_simple_moving_average(bars, fast_window=2, slow_window=5)
