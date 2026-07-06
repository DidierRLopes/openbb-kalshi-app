"""Disk-backed, paced ingest of the open-market universe for category, event,
and top-market stats.

A single background task (`run_ingest_loop`) refreshes the taxonomy and pages
the full open-market book, writing the snapshot to the `data` cache one page at
a time under an incrementing generation id. Publishing is atomic: `stats:meta`
points at a generation only after all its chunks are written, and the previous
generation is kept for one cycle so in-flight readers never see a torn snapshot.

Readers stream the published snapshot chunk-by-chunk (`_iter_markets`), so peak
memory is one chunk plus the (bounded) aggregate — never the whole universe.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from kalshi.cache import DiskCache
from kalshi.client import KalshiClient
from kalshi.config import Settings
from kalshi.formatting import build_market_key, iso_to_display, parse_iso_time, pct, quantity, to_float
from kalshi.taxonomy import TaxonomyCache

if TYPE_CHECKING:
    from kalshi.service import MarketDataService

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

LOGGER = logging.getLogger(__name__)


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


def _project(
    raw: dict[str, Any],
    category_by_series: dict[str, str],
    frequency_by_series: dict[str, str],
    tags_by_series: dict[str, list[str]],
    live_feed_by_series: dict[str, bool],
) -> dict[str, Any] | None:
    """Slim widget row for one raw market, or None if it fails the activity filter."""
    volume_24h = quantity(raw.get("volume_24h_fp"))
    open_interest = quantity(raw.get("open_interest_fp"))
    if volume_24h <= 0 and open_interest <= 0:
        return None
    event_ticker = raw.get("event_ticker", "")
    ticker = raw.get("ticker", "")
    series_ticker = _series_of(event_ticker, ticker)
    close_dt = parse_iso_time(raw.get("close_time"))
    open_dt = parse_iso_time(raw.get("open_time"))
    last_price_pct = pct(raw.get("last_price_dollars"))
    prev_pct = pct(raw.get("previous_price_dollars"))
    volatility = round(abs(last_price_pct - prev_pct), 2) if prev_pct > 0 else 0.0
    return {
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


class MarketStatsCache:
    def __init__(
        self,
        client: KalshiClient,
        taxonomy: TaxonomyCache,
        cache: DiskCache,
        settings: Settings,
        service: "MarketDataService | None" = None,
    ) -> None:
        self._client = client
        self._taxonomy = taxonomy
        self._cache = cache
        self._settings = settings
        self._service = service
        self._token = uuid.uuid4().hex

    async def _iter_markets(
        self,
        *,
        category: str | None = None,
        tag: str | None = None,
        series_ticker: str | None = None,
        frequency: str | None = None,
        cutoff: float | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream the published snapshot one chunk at a time, applying row filters."""
        meta = await self._cache.get("stats:meta")
        if not meta:
            return
        generation = meta.get("generation")
        chunk_count = int(meta.get("chunk_count", 0))
        for index in range(chunk_count):
            key = f"stats:gen:{generation}:chunk:{index}"
            try:
                chunk = await self._cache.get(key)
            except Exception:
                LOGGER.warning("Kalshi stats chunk %s unreadable; skipping", key)
                continue
            if chunk is None:
                latest = await self._cache.get("stats:meta") or {}
                if latest.get("generation") != generation:
                    LOGGER.warning("Kalshi stats gen %s rotated mid-read", generation)
                else:
                    LOGGER.warning("Kalshi stats gen %s missing chunk %s", generation, index)
                return
            for market in chunk:
                if category and category != "All" and market.get("category") != category:
                    continue
                if tag and tag != "All" and tag not in (market.get("tags") or []):
                    continue
                if series_ticker and market.get("series_ticker") != series_ticker:
                    continue
                if frequency and frequency != "all" and not _freq_allowed(
                    frequency, market.get("frequency", "")
                ):
                    continue
                if cutoff is not None:
                    close_ts = market.get("close_ts")
                    if close_ts is None or close_ts > cutoff:
                        continue
                yield market

    async def markets(
        self,
        category: str | None = None,
        close_within_days: int | None = None,
        frequency: str | None = None,
        tag: str | None = None,
        series_ticker: str | None = None,
    ) -> list[dict[str, Any]]:
        cutoff = None if close_within_days is None else time.time() + close_within_days * 86400
        return [
            m
            async for m in self._iter_markets(
                category=category,
                tag=tag,
                series_ticker=series_ticker,
                frequency=frequency,
                cutoff=cutoff,
            )
        ]

    async def active_scope(self) -> dict[str, set[str]]:
        """Categories, tags, and series tickers that currently have live markets.
        Drives the drilldown dropdowns so every pickable option has events."""
        scope = await self._cache.get("stats:active_scope") or {}
        return {
            "categories": set(scope.get("categories") or []),
            "tags": set(scope.get("tags") or []),
            "series": set(scope.get("series") or []),
        }

    async def by_group(
        self,
        group_by: str = "category",
        close_within_days: int | None = None,
        tag: str | None = None,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        """Aggregate metrics by category or by tag."""
        by_tag = group_by == "tag"
        cutoff = None if close_within_days is None else time.time() + close_within_days * 86400
        stats: dict[str, dict[str, float]] = {}
        async for market in self._iter_markets(
            category=category, tag=None if by_tag else tag, cutoff=cutoff
        ):
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
        cutoff = None if close_within_days is None else time.time() + close_within_days * 86400
        events: dict[str, dict[str, Any]] = {}
        async for market in self._iter_markets(category=category, tag=tag, cutoff=cutoff):
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
        limit: int | None = 100,
    ) -> list[dict[str, Any]]:
        """Active events as table rows with the metrics that matter. `limit=None`
        returns every event in scope (no truncation)."""
        terms = _terms(search)
        events: dict[str, dict[str, Any]] = {}
        async for market in self._iter_markets(
            category=category, tag=tag, series_ticker=series_ticker or None
        ):
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
                    "_matched": False,
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
            if terms and not event["_matched"]:
                event["_matched"] = all(t in _haystack(market) for t in terms)
            if market["volume_total"] > event["_top_vol"]:
                event["_top_vol"] = market["volume_total"]
                event["leading_outcome"] = market["subtitle"] or market["ticker"]
                event["leading_pct"] = market["last_price_pct"]

        candidates = [e for e in events.values() if not terms or e["_matched"]]
        ranked = sorted(candidates, key=lambda e: e["volume_total"], reverse=True)
        rows = ranked if limit is None else ranked[:limit]
        now = time.time()
        for row in rows:
            row.pop("_top_vol", None)
            row.pop("_matched", None)
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
    ) -> list[dict[str, Any]]:
        """Every matching event as an ordered card. The caller paginates with
        limit/offset; the full list is returned so callers can report the total."""
        cutoff = None if close_within_days is None else time.time() + close_within_days * 86400
        terms = _terms(search)
        events: dict[str, dict[str, Any]] = {}
        async for market in self._iter_markets(
            category=category, tag=tag, frequency=frequency, cutoff=cutoff
        ):
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
                    "_matched": False,
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
            if terms and not event["_matched"]:
                event["_matched"] = all(t in _haystack(market) for t in terms)
            close_ts = market.get("close_ts")
            if close_ts is not None and (event["close_ts"] is None or close_ts < event["close_ts"]):
                event["close_ts"] = close_ts
                event["close_time"] = market["close_time"]
            open_ts = market.get("open_ts")
            if open_ts is not None and (event["open_ts"] is None or open_ts > event["open_ts"]):
                event["open_ts"] = open_ts
            outcomes = event["outcomes"]
            outcomes.append(
                {
                    "name": market["subtitle"] or market["ticker"],
                    "probability_pct": market["last_price_pct"],
                    "yes_bid_pct": market["yes_bid_pct"],
                    "volume_total": market["volume_total"],
                    "market_key": market["market_key"],
                }
            )
            if len(outcomes) > outcomes_per_event:
                outcomes.sort(key=lambda o: to_float(o["volume_total"]), reverse=True)
                del outcomes[outcomes_per_event:]

        field, descending = SORT_FIELDS.get(sort, SORT_FIELDS["trending"])
        if reverse:
            descending = not descending

        def sort_key(event: dict[str, Any]) -> float:
            value = event.get(field)
            if value is None:
                return float("-inf") if descending else float("inf")
            return value

        cards = [e for e in events.values() if not terms or e["_matched"]]
        for event in cards:
            event["outcomes"].sort(key=lambda o: to_float(o["volume_total"]), reverse=True)
        cards.sort(key=sort_key, reverse=descending)
        now = time.time()
        for event in cards:
            event["live"] = _live(event.get("open_ts"), event.get("close_ts"), event.pop("_priced", False), now)
            for internal in ("volatility", "fifty", "_matched"):
                event.pop(internal, None)
        return cards

    async def warmup(self) -> None:
        """Blocking initial load: run one full ingest cycle (taxonomy + market
        scan + card images) before the app starts serving. Fast on a warm volume
        — a fresh snapshot skips the scan; only a cold or stale cache does work."""
        await self._sweep_orphans()
        await self._run_cycle()

    async def run_ingest_loop(self) -> None:
        """Periodic background refresh, started after warmup completes."""
        while True:
            await asyncio.sleep(self._tick_seconds())
            await self._run_cycle()

    async def _run_cycle(self) -> None:
        try:
            if await self._acquire_leader():
                if await self._taxonomy.is_stale():
                    await self._taxonomy.refresh()
                if await self._stats_stale():
                    await self._scan_and_publish()
                    await self._warm_cards()
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Kalshi stats ingest cycle failed")

    def _tick_seconds(self) -> int:
        return max(30, min(self._settings.taxonomy_ttl, self._settings.stats_ttl))

    async def _acquire_leader(self) -> bool:
        """Best-effort single-ingestor lease so extra web workers stay read-only."""
        lease_ttl = self._tick_seconds() * 3 + 60
        if await self._cache.add("stats:ingestor", self._token, expire=lease_ttl):
            return True
        if await self._cache.get("stats:ingestor") == self._token:
            await self._cache.set("stats:ingestor", self._token, expire=lease_ttl)
            return True
        return False

    async def _stats_stale(self) -> bool:
        meta = await self._cache.get("stats:meta")
        if not meta:
            return True
        return (time.time() - float(meta.get("fetched_at", 0))) >= self._settings.stats_ttl

    async def _scan_and_publish(self) -> None:
        started = time.monotonic()
        category_by_series = await self._taxonomy.category_by_series()
        frequency_by_series = await self._taxonomy.frequency_by_series()
        tags_by_series = await self._taxonomy.tags_by_series()
        live_feed_by_series = await self._taxonomy.live_feed_by_series()

        old = await self._cache.get("stats:meta") or {}
        old_gen = old.get("generation")
        new_gen = (int(old_gen) + 1) if isinstance(old_gen, int) else 1

        cursor: str | None = None
        seen_cursors: set[str] = set()
        chunk_index = 0
        pages = 0
        raw_markets = 0
        retained = 0
        active_categories: set[str] = set()
        active_tags: set[str] = set()
        active_series: set[str] = set()
        active_events: set[str] = set()
        hit_ceiling = True
        try:
            for _ in range(self._settings.stats_scan_max_pages):
                params: dict[str, Any] = {"status": "open", "limit": 1000, "mve_filter": "exclude"}
                if cursor:
                    params["cursor"] = cursor
                data = await self._client.get("/markets", params, no_store=True)
                page = data.get("markets") or []
                pages += 1
                raw_markets += len(page)
                chunk = [
                    row
                    for raw in page
                    if (row := _project(
                        raw, category_by_series, frequency_by_series, tags_by_series, live_feed_by_series
                    )) is not None
                ]
                if chunk:
                    await self._cache.set(f"stats:gen:{new_gen}:chunk:{chunk_index}", chunk)
                    chunk_index += 1
                    retained += len(chunk)
                    for row in chunk:
                        if row["category"]:
                            active_categories.add(row["category"])
                        if row["series_ticker"]:
                            active_series.add(row["series_ticker"])
                        if row["event_ticker"]:
                            active_events.add(row["event_ticker"])
                        active_tags.update(row["tags"])
                cursor = data.get("cursor")
                if not cursor or cursor in seen_cursors:
                    hit_ceiling = False
                    break
                seen_cursors.add(cursor)
                await asyncio.sleep(self._settings.stats_page_pause)

            await self._cache.set(
                "stats:active_scope",
                {
                    "categories": sorted(active_categories),
                    "tags": sorted(active_tags),
                    "series": sorted(active_series),
                },
            )
            await self._cache.set("stats:active_events", sorted(active_events))
            await self._cache.set(
                "stats:meta",
                {"generation": new_gen, "chunk_count": chunk_index, "fetched_at": time.time()},
            )
            await self._delete_generation(new_gen - 2)
            await self._register_generation(new_gen, chunk_index)
        except asyncio.CancelledError:
            raise
        except Exception:
            for index in range(chunk_index):
                await self._cache.delete(f"stats:gen:{new_gen}:chunk:{index}")
            raise

        if hit_ceiling:
            LOGGER.warning(
                "Kalshi stats scan hit page ceiling %s", self._settings.stats_scan_max_pages
            )
        LOGGER.info(
            "Kalshi stats refreshed gen=%s pages=%s raw=%s retained=%s chunks=%s seconds=%.2f",
            new_gen, pages, raw_markets, retained, chunk_index, time.monotonic() - started,
        )

    async def _warm_cards(self) -> None:
        """Pre-fetch event-card images for every active event into the cache so
        the browser and event pages never trigger a burst of live card calls."""
        if self._service is None:
            return
        events = await self._cache.get("stats:active_events") or []
        if not events:
            return
        started = time.monotonic()
        warmed = await self._service.warm_card_images(list(events))
        LOGGER.info(
            "Kalshi card images warmed events=%s seconds=%.2f",
            warmed, time.monotonic() - started,
        )

    async def _register_generation(self, gen: int, chunk_count: int) -> None:
        gens = await self._cache.get("stats:generations", []) or []
        gens = [g for g in gens if g.get("gen") != gen]
        gens.append({"gen": gen, "chunk_count": chunk_count})
        gens = sorted(gens, key=lambda g: g["gen"])[-2:]
        await self._cache.set("stats:generations", gens)

    async def _delete_generation(self, gen: int) -> None:
        if gen is None or gen < 1:
            return
        gens = await self._cache.get("stats:generations", []) or []
        entry = next((g for g in gens if g.get("gen") == gen), None)
        if entry is None:
            return
        for index in range(int(entry.get("chunk_count", 0))):
            await self._cache.delete(f"stats:gen:{gen}:chunk:{index}")

    async def _sweep_orphans(self) -> None:
        """Drop chunks left by crashed or superseded generations (startup only)."""
        meta = await self._cache.get("stats:meta") or {}
        gen = meta.get("generation")
        keep = {gen, gen - 1} if isinstance(gen, int) else set()
        for key in await self._cache.iter_keys():
            if not isinstance(key, str) or not key.startswith("stats:gen:"):
                continue
            try:
                orphan_gen = int(key.split(":")[2])
            except (IndexError, ValueError):
                continue
            if orphan_gen not in keep:
                await self._cache.delete(key)
