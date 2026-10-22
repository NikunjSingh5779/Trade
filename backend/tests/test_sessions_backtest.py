from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.backtesting.engine import evaluate_simple_moving_average
from app.market_data.sessions import is_regular_us_bar, is_regular_us_session, us_session_bounds


def test_us_session_weekday_and_weekend():
    ny = ZoneInfo("America/New_York")
    assert is_regular_us_session(datetime(2026, 10, 20, 10, 0, tzinfo=ny))
    assert not is_regular_us_session(datetime(2026, 10, 24, 10, 0, tzinfo=ny))
    assert not is_regular_us_session(datetime(2026, 10, 20, 8, 0, tzinfo=ny))


def test_backtest_requires_enough_bars():
    bars = [{"symbol": "AAPL", "timestamp": str(i), "close": 100 + i} for i in range(5)]
    with pytest.raises(ValueError):
        evaluate_simple_moving_average(bars, fast_window=2, slow_window=5)



def test_us_session_calendar_excludes_exchange_holidays_and_early_close_hours():
    ny = ZoneInfo("America/New_York")
    # Thanksgiving 2026 is an exchange holiday.
    assert not is_regular_us_session(datetime(2026, 11, 26, 11, 0, tzinfo=ny))

    # NYSE closes at 13:00 ET on the Friday after Thanksgiving.
    assert is_regular_us_session(datetime(2026, 11, 27, 12, 55, tzinfo=ny))
    assert not is_regular_us_session(datetime(2026, 11, 27, 13, 0, tzinfo=ny))

    bounds = us_session_bounds(datetime(2026, 11, 27, 12, 0, tzinfo=ny).date())
    assert bounds is not None
    assert bounds[1].astimezone(ny).hour == 13
    assert bounds[1].astimezone(ny).minute == 0


def test_regular_bar_calendar_excludes_holiday_and_early_close_after_close():
    assert not is_regular_us_bar({
        "timestamp": "2026-11-26T16:00:00Z",
    })
    assert is_regular_us_bar({
        "timestamp": "2026-11-27T17:55:00Z",
    })
    assert not is_regular_us_bar({
        "timestamp": "2026-11-27T18:00:00Z",
    })
