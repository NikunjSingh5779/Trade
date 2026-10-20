from datetime import datetime, time
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")


def is_regular_us_session(now: datetime | None = None) -> bool:
    """Weekday and regular-hours gate. Public holidays are not excluded in Phase 1."""
    local = (now or datetime.now(NEW_YORK)).astimezone(NEW_YORK)
    if local.weekday() >= 5:
        return False
    return time(9, 30) <= local.time().replace(tzinfo=None) < time(16, 0)


def current_scan_window(now: datetime | None = None) -> str:
    local = (now or datetime.now(NEW_YORK)).astimezone(NEW_YORK)
    return local.strftime("%Y-%m-%dT%H:%M")
