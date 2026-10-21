from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo
from typing import Any

NEW_YORK = ZoneInfo("America/New_York")


def is_regular_us_session(now: datetime | None = None) -> bool:
    """Weekday and regular-hours gate. Public holidays are not excluded in Phase 1."""
    local = (now or datetime.now(NEW_YORK)).astimezone(NEW_YORK)
    if local.weekday() >= 5:
        return False
    return time(9, 30) <= local.time().replace(tzinfo=None) < time(16, 0)


def is_regular_us_bar(bar: dict[str, Any]) -> bool:
    """Return true only for a candle timestamped during the US regular equity session."""
    raw = bar.get("timestamp")
    if not isinstance(raw, str) or not raw.strip():
        return False
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    local = stamp.astimezone(NEW_YORK)
    if local.weekday() >= 5:
        return False
    clock = local.time().replace(tzinfo=None)
    return time(9, 30) <= clock < time(16, 0)


def current_scan_window(now: datetime | None = None) -> str:
    local = (now or datetime.now(NEW_YORK)).astimezone(NEW_YORK)
    return local.strftime("%Y-%m-%dT%H:%M")
