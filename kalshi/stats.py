"""Background-refreshed scan of open markets for category and top-market stats."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from kalshi.client import KalshiClient
from kalshi.config import Settings
from kalshi.formatting import build_market_key, iso_to_display, parse_iso_time, pct, quantity, to_float
from kalshi.taxonomy import TaxonomyCache

FREQ_GROUPS = {
    "hourly": {"hourly", "fifteen_min"},
    "daily": {"daily"},
    "weekly": {"weekly"},
    "monthly": {"monthly"},
    "quarterly": {"quarterly"},
    "annual": {"annual"},
}

SORT_FIELDS = {
    "trending": ("volume_24h", True),
    "volume": ("volume_total", True),
    "open_interest": ("open_interest", True),
    "volatile": ("volatility", True),
    "new": ("open_ts", True),
    "closing_soon": ("close_ts", False),
    "fifty_fifty": ("fifty", False),
}


def _series_of(event_ticker: str, fallback: str) -> str:
    return (event_ticker or fallback or "").split("-")[0]


def _freq_allowed(frequency: str | None, market_freq: str) -> bool:
    if not frequency or frequency == "all":
        return True
    return market_freq in FREQ_GROUPS.get(frequency, {frequency})


def _live(open_ts: float | None, close_ts: float | None, has_feed: bool, now: float) -> bool:
    """True when the event has a real-time price feed and is inside its trading window."""
    if not has_feed:
        return False
    if open_ts is not None and open_ts > now:
        return False
    if close_ts is not None and now >= close_ts:
        return False
    return True


def _terms(search: str) -> list[str]:
    return [t for t in (search or "").lower().split() if t]


def _haystack(market: dict[str, Any]) -> str:
    return " ".join(
        str(market.get(field, ""))
        for field in ("title", "subtitle", "category", "ticker", "event_ticker")
    ).lower()


class MarketStatsCache:
    def __init__(self, client: KalshiClient, taxonomy: TaxonomyCache, settings: Settings) -> None:
        self._client = client
        self._taxonomy = taxonomy
        self._settings = settings
        self._lock = asyncio.Lock()
        self._loaded = False
        self._fetched_at = 0.0
        self._markets: list[dict[str, Any]] = []

    def _fresh(self) -> bool:
        return self._loaded and (time.monotonic() - self._fetched_at < self._settings.stats_ttl)

    async def ensure_fresh(self) -> None:
        if self._fresh():
            return
        async with self._lock:
            if self._fresh():
                return
            await self._scan()

    async def _scan(self) -> None:
        category_by_series = await self._taxonomy.category_by_series()
        frequency_by_series = await self._taxonomy.frequency_by_series()
        tags_by_series = await self._taxonomy.tags_by_series()
        live_feed_by_series = await self._taxonomy.live_feed_by_series()
        markets: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(self._settings.stats_scan_max_pages):
            params: dict[str, Any] = {
                "status": "open",
                "limit": 1000,
                "mve_filter": "exclude",
            }
            if cursor:
                params["cursor"] = cursor
            data = await self._client.get("/markets", params, ttl=self._settings.stats_ttl)
            page = data.get("markets") or []
            for raw in page:
                volume_24h = quantity(raw.get("volume_24h_fp"))
                open_interest = quantity(raw.get("open_interest_fp"))
                if volume_24h <= 0 and open_interest <= 0:
                    continue
                event_ticker = raw.get("event_ticker", "")
                ticker = raw.get("ticker", "")
                series_ticker = _series_of(event_ticker, ticker)
                close_dt = parse_iso_time(raw.get("close_time"))
                open_dt = parse_iso_time(raw.get("open_time"))
                last_price_pct = pct(raw.get("last_price_dollars"))
                prev_pct = pct(raw.get("previous_price_dollars"))
                volatility = round(abs(last_price_pct - prev_pct), 2) if prev_pct > 0 else 0.0
                markets.append(
                    {
                        "market_key": build_market_key(series_ticker, ticker, event_ticker),
                        "ticker": ticker,
                        "event_ticker": event_ticker,
                        "series_ticker": series_ticker,
                        "category": category_by_series.get(series_ticker, "Other"),
                        "frequency": frequency_by_series.get(series_ticker, ""),
                        "tags": tags_by_series.get(series_ticker, []),
                        "title": raw.get("title", ""),
                        "subtitle": (
                            raw.get("yes_sub_title")
                            or raw.get("subtitle")
                            or raw.get("no_sub_title")
                            or ""
                        ),
                        "last_price_pct": last_price_pct,
                        "yes_bid_pct": pct(raw.get("yes_bid_dollars")),
                        "yes_ask_pct": pct(raw.get("yes_ask_dollars")),
                        "volume_24h": volume_24h,
                        "volume_total": quantity(raw.get("volume_fp")),
                        "open_interest": open_interest,
                        "volatility": volatility,
                        "close_time": iso_to_display(raw.get("close_time")),
                        "close_ts": close_dt.timestamp() if close_dt else None,
                        "open_ts": open_dt.timestamp() if open_dt else None,
                        "live_feed": live_feed_by_series.get(series_ticker, False),
                    }
                )
            cursor = data.get("cursor")
            if not cursor:
                break
        self._markets = markets
        self._fetched_at = time.monotonic()
        self._loaded = True

    def _within_window(self, market: dict[str, Any], cutoff: float | None) -> bool:
        if cutoff is None:
            return True
        close_ts = market.get("close_ts")
        return close_ts is not None and close_ts <= cutoff

    async def markets(
        self,
        category: str | None = None,
        close_within_days: int | None = None,
        frequency: str | None = None,
        tag: str | None = None,
        series_ticker: str | None = None,
    ) -> list[dict[str, Any]]:
        await self.ensure_fresh()
        cutoff = None if close_within_days is None else time.time() + close_within_days * 86400
        rows = self._markets
        if category and category != "All":
            rows = [m for m in rows if m["category"] == category]
        if tag and tag != "All":
            rows = [m for m in rows if tag in (m.get("tags") or [])]
        if series_ticker:
            rows = [m for m in rows if m["series_ticker"] == series_ticker]
        if frequency and frequency != "all":
            rows = [m for m in rows if _freq_allowed(frequency, m.get("frequency", ""))]
        return [m for m in rows if self._within_window(m, cutoff)]

    async def by_group(
        self,
        group_by: str = "category",
        close_within_days: int | None = None,
        tag: str | None = None,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        """Aggregate metrics by category or by tag."""
        by_tag = group_by == "tag"
        rows = await self.markets(
            category=category, close_within_days=close_within_days, tag=None if by_tag else tag
        )
        stats: dict[str, dict[str, float]] = {}
        for market in rows:
            if by_tag:
                keys = [t for t in (market.get("tags") or []) if t]
            else:
                keys = [market["category"]] if market.get("category") else []
            for key in keys:
                bucket = stats.setdefault(
                    key,
                    {"volume_24h": 0.0, "volume_total": 0.0, "open_interest": 0.0, "market_count": 0.0},
                )
                bucket["volume_24h"] += market["volume_24h"]
                bucket["volume_total"] += market["volume_total"]
                bucket["open_interest"] += market["open_interest"]
                bucket["market_count"] += 1
        return [
            {"category": key, **{k: round(v, 2) for k, v in values.items()}}
            for key, values in stats.items()
        ]

    async def by_event(
        self,
        category: str | None = None,
        tag: str | None = None,
        close_within_days: int | None = None,
    ) -> list[dict[str, Any]]:
        """Aggregate the events under a selected category or tag from their markets."""
        markets = await self.markets(
            category=category, tag=tag, close_within_days=close_within_days
        )
        events: dict[str, dict[str, Any]] = {}
        for market in markets:
            bucket = events.setdefault(
                market["event_ticker"],
                {"title": market["title"], "volume_24h": 0.0, "volume_total": 0.0,
                 "open_interest": 0.0, "market_count": 0.0},
            )
            bucket["volume_24h"] += market["volume_24h"]
            bucket["volume_total"] += market["volume_total"]
            bucket["open_interest"] += market["open_interest"]
            bucket["market_count"] += 1
        rows = []
        for event_ticker, bucket in events.items():
            title = bucket.pop("title").replace("**", "").strip()
            rows.append({
                "category": title,
                "event_ticker": event_ticker,
                **{k: round(v, 2) for k, v in bucket.items()},
            })
        return rows

    async def default_event_ticker(
        self,
        category: str | None = None,
        tag: str | None = None,
        series_ticker: str | None = None,
    ) -> str:
        events = await self.discover_events(
            category=category, tag=tag, series_ticker=series_ticker, limit=1
        )
        return events[0]["event_ticker"] if events else ""

    async def discover_events(
        self,
        category: str | None = None,
        tag: str | None = None,
        series_ticker: str | None = None,
        search: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Top active events as table rows with the metrics that matter."""
        markets = await self.markets(category=category, tag=tag, series_ticker=series_ticker or None)
        terms = _terms(search)
        if terms:
            matched = {m["event_ticker"] for m in markets if all(t in _haystack(m) for t in terms)}
            markets = [m for m in markets if m["event_ticker"] in matched]

        events: dict[str, dict[str, Any]] = {}
        for market in markets:
            event = events.setdefault(
                market["event_ticker"],
                {
                    "title": market["title"],
                    "category": market["category"],
                    "series_ticker": market["series_ticker"],
                    "event_ticker": market["event_ticker"],
                    "tags": ", ".join(market.get("tags") or []),
                    "market_count": 0,
                    "volume_24h": 0.0,
                    "volume_total": 0.0,
                    "open_interest": 0.0,
                    "close_time": market["close_time"],
                    "open_ts": market.get("open_ts"),
                    "close_ts": market.get("close_ts"),
                    "_priced": False,
                    "_top_vol": -1.0,
                    "leading_outcome": "",
                    "leading_pct": 0.0,
                },
            )
            event["market_count"] += 1
            event["volume_24h"] += market["volume_24h"]
            event["volume_total"] += market["volume_total"]
            event["open_interest"] += market["open_interest"]
            event["_priced"] = event["_priced"] or bool(market.get("live_feed"))
            if market["volume_total"] > event["_top_vol"]:
                event["_top_vol"] = market["volume_total"]
                event["leading_outcome"] = market["subtitle"] or market["ticker"]
                event["leading_pct"] = market["last_price_pct"]

        rows = sorted(events.values(), key=lambda e: e["volume_total"], reverse=True)[:limit]
        now = time.time()
        for row in rows:
            row.pop("_top_vol", None)
            row["live"] = _live(row.get("open_ts"), row.get("close_ts"), row.pop("_priced", False), now)
        return rows

    async def browse_events(
        self,
        category: str | None = None,
        tag: str | None = None,
        search: str = "",
        close_within_days: int | None = None,
        frequency: str | None = None,
        sort: str = "trending",
        reverse: bool = False,
        outcomes_per_event: int = 4,
        limit: int = 40,
    ) -> list[dict[str, Any]]:
        """Markets grouped into event cards for the HTML browser."""
        markets = await self.markets(
            category=category, tag=tag, close_within_days=close_within_days, frequency=frequency
        )
        terms = _terms(search)
        if terms:
            matched = {m["event_ticker"] for m in markets if all(t in _haystack(m) for t in terms)}
            markets = [m for m in markets if m["event_ticker"] in matched]

        events: dict[str, dict[str, Any]] = {}
        for market in markets:
            event = events.setdefault(
                market["event_ticker"],
                {
                    "event_ticker": market["event_ticker"],
                    "series_ticker": market["series_ticker"],
                    "title": market["title"],
                    "category": market["category"],
                    "tags": market.get("tags") or [],
                    "volume_24h": 0.0,
                    "volume_total": 0.0,
                    "open_interest": 0.0,
                    "volatility": 0.0,
                    "fifty": 50.0,
                    "market_count": 0,
                    "close_time": "",
                    "close_ts": None,
                    "open_ts": None,
                    "_priced": False,
                    "outcomes": [],
                },
            )
            event["volume_24h"] += market["volume_24h"]
            event["volume_total"] += market["volume_total"]
            event["open_interest"] += market["open_interest"]
            event["market_count"] += 1
            event["_priced"] = event["_priced"] or bool(market.get("live_feed"))
            event["volatility"] = max(event["volatility"], market.get("volatility", 0.0))
            event["fifty"] = min(event["fifty"], abs(market["last_price_pct"] - 50))
            close_ts = market.get("close_ts")
            if close_ts is not None and (event["close_ts"] is None or close_ts < event["close_ts"]):
                event["close_ts"] = close_ts
                event["close_time"] = market["close_time"]
            open_ts = market.get("open_ts")
            if open_ts is not None and (event["open_ts"] is None or open_ts > event["open_ts"]):
                event["open_ts"] = open_ts
            event["outcomes"].append(
                {
                    "name": market["subtitle"] or market["ticker"],
                    "probability_pct": market["last_price_pct"],
                    "yes_bid_pct": market["yes_bid_pct"],
                    "volume_total": market["volume_total"],
                    "market_key": market["market_key"],
                }
            )

        field, descending = SORT_FIELDS.get(sort, SORT_FIELDS["trending"])
        if reverse:
            descending = not descending

        def sort_key(event: dict[str, Any]) -> float:
            value = event.get(field)
            if value is None:
                return float("-inf") if descending else float("inf")
            return value

        cards = list(events.values())
        for event in cards:
            event["outcomes"].sort(key=lambda o: to_float(o["volume_total"]), reverse=True)
            event["outcomes"] = event["outcomes"][:outcomes_per_event]
        cards.sort(key=sort_key, reverse=descending)
        result = cards[:limit]
        now = time.time()
        for event in result:
            event["live"] = _live(event.get("open_ts"), event.get("close_ts"), event.pop("_priced", False), now)
            for internal in ("volatility", "fifty"):
                event.pop(internal, None)
        return result
