from datetime import UTC, date, datetime
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

NEW_YORK = ZoneInfo("America/New_York")
_NYSE = mcal.get_calendar("NYSE")


@lru_cache(maxsize=4096)
def _session_bounds(session_day: date) -> tuple[datetime, datetime] | None:
    """Return NYSE regular open/close in UTC; None means holiday/non-trading day."""
    schedule = _NYSE.schedule(start_date=session_day, end_date=session_day)
    if schedule.empty:
        return None
    row = schedule.iloc[0]
    return row["market_open"].to_pydatetime(), row["market_close"].to_pydatetime()


def _utc_now(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        # Treat test/operator-supplied naive market times as New York wall time.
        current = current.replace(tzinfo=NEW_YORK)
    return current.astimezone(UTC)


def us_session_bounds(session_day: date | None = None) -> tuple[datetime, datetime] | None:
    """Return the day's scheduled NYSE open/close in UTC; no bounds means market closed."""
    return _session_bounds(session_day or datetime.now(NEW_YORK).date())


def is_regular_us_session(now: datetime | None = None) -> bool:
    """True only within the NYSE regular session, including holidays and early closes."""
    current = _utc_now(now)
    session_day = current.astimezone(NEW_YORK).date()
    bounds = _session_bounds(session_day)
    return bool(bounds and bounds[0] <= current < bounds[1])


def is_regular_us_bar(bar: dict[str, Any]) -> bool:
    """Return true only for a candle timestamped inside that date's NYSE regular session."""
    raw = bar.get("timestamp")
    if not isinstance(raw, str) or not raw.strip():
        return False
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=NEW_YORK)
    stamp = stamp.astimezone(UTC)
    session_day = stamp.astimezone(NEW_YORK).date()
    bounds = _session_bounds(session_day)
    return bool(bounds and bounds[0] <= stamp < bounds[1])


def current_scan_window(now: datetime | None = None) -> str:
    return _utc_now(now).astimezone(NEW_YORK).strftime("%Y-%m-%dT%H:%M")
