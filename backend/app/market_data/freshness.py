from datetime import UTC, datetime
from typing import Any


class MarketDataFreshnessError(ValueError):
    """Raised when stored bars cannot safely support the requested analysis."""


def ensure_fresh_bars(
    bars: list[dict[str, Any]],
    timeframe: str,
    max_stale_data_seconds: int,
    now: datetime | None = None,
) -> None:
    if not bars:
        raise MarketDataFreshnessError("No timestamped market bars are available; run a market scan first.")

    timestamps: list[datetime] = []
    for bar in bars:
        raw = bar.get("timestamp")
        if not isinstance(raw, str) or not raw.strip():
            raise MarketDataFreshnessError("Market data contains a missing or invalid timestamp.")
        try:
            timestamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise MarketDataFreshnessError("Market data contains a missing or invalid timestamp.") from exc
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        timestamps.append(timestamp.astimezone(UTC))

    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    age_seconds = (current.astimezone(UTC) - max(timestamps)).total_seconds()
    if age_seconds < -60:
        raise MarketDataFreshnessError("Market data timestamp is in the future; analysis was blocked.")

    max_age = max_stale_data_seconds if timeframe == "intraday" else 7 * 24 * 60 * 60
    if age_seconds > max_age:
        raise MarketDataFreshnessError(
            f"Stored market bars are stale ({int(age_seconds)} seconds old; limit is {max_age}). "
            "Refresh market data before running this analysis."
        )
