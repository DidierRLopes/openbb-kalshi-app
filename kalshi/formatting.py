"""Value coercion and formatting helpers, plus the market_key codec."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def to_float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def pct(value: Any) -> float:
    return round(to_float(value) * 100, 2)


def money(value: Any) -> float:
    return round(to_float(value), 4)


def quantity(value: Any) -> float:
    return round(to_float(value), 2)


def compact_number(value: Any) -> str:
    number = to_float(value)
    magnitude = abs(number)
    if magnitude >= 1_000_000:
        return f"{number / 1_000_000:.2f}M"
    if magnitude >= 1_000:
        return f"{number / 1_000:.1f}K"
    return f"{number:.0f}"


def iso_to_display(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    return value.replace("T", " ").replace("Z", " UTC")[:19]


def timestamp_to_iso(timestamp: Any) -> str:
    seconds = to_float(timestamp, None)
    if seconds is None:
        return ""
    try:
        return datetime.fromtimestamp(int(seconds), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def parse_iso_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def clamp_limit(limit: Any, minimum: int = 1, maximum: int = 200) -> int:
    return max(minimum, min(maximum, int(to_float(limit, minimum))))


MARKET_KEY_SEPARATOR = "|"


def build_market_key(series_ticker: str, market_ticker: str, event_ticker: str) -> str:
    return MARKET_KEY_SEPARATOR.join(
        (series_ticker or "", market_ticker or "", event_ticker or "")
    )


def parse_market_key(market_key: str | None) -> dict[str, str]:
    parts = (market_key or "").split(MARKET_KEY_SEPARATOR)
    if len(parts) >= 3:
        series_ticker, market_ticker, event_ticker = parts[0], parts[1], parts[2]
    elif len(parts) == 2:
        series_ticker, market_ticker, event_ticker = "", parts[0], parts[1]
    else:
        series_ticker, market_ticker, event_ticker = "", parts[0], ""
    return {
        "series_ticker": series_ticker.strip(),
        "market_ticker": market_ticker.strip(),
        "event_ticker": event_ticker.strip(),
    }
