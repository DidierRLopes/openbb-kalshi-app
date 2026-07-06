"""Category/tag/series taxonomy assembled from /series and
/search/tags_by_categories, persisted to the on-disk `data` cache. Every
dropdown choice derives from it.

The ingestor calls `refresh()` to fetch, assemble, precompute the derived
lookups, and publish them. Read methods only read the published data — they
never fetch, so options endpoints stay cheap and there is no refresh stampede.
"""

from __future__ import annotations

import time
from typing import Any

from kalshi.cache import DiskCache
from kalshi.client import KalshiClient
from kalshi.config import Settings
from kalshi.formatting import to_float

ALL = "All"


def _norm(value: str | None) -> str:
    return (value or "").strip()


def _compute_categories(series: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stats: dict[str, dict[str, float]] = {}
    for entry in series:
        category = entry.get("category") or ""
        if not category:
            continue
        bucket = stats.setdefault(category, {"count": 0.0, "volume": 0.0})
        bucket["count"] += 1
        bucket["volume"] += to_float(entry.get("volume_fp"))
    return [
        {"category": category, "series_count": int(s["count"]), "volume": s["volume"]}
        for category, s in sorted(
            stats.items(), key=lambda kv: (-kv[1]["volume"], kv[0].casefold())
        )
    ]


def _compute_live_feed(series_by_ticker: dict[str, dict[str, Any]]) -> dict[str, bool]:
    """True for series whose underlying settles on a real-time price feed (Pyth)
    — the events Kalshi exposes a continuous live_data timeseries for."""
    result: dict[str, bool] = {}
    for ticker, series in series_by_ticker.items():
        names = " ".join(
            str(src.get("name", ""))
            for src in (series.get("settlement_sources") or [])
            if isinstance(src, dict)
        ).lower()
        result[ticker] = "pyth" in names
    return result


class TaxonomyCache:
    def __init__(self, client: KalshiClient, cache: DiskCache, settings: Settings) -> None:
        self._client = client
        self._cache = cache
        self._settings = settings

    async def is_stale(self) -> bool:
        """Whether the ingestor should refresh (missing or older than the TTL)."""
        meta = await self._cache.get("tax:meta")
        if not meta:
            return True
        return (time.time() - float(meta.get("fetched_at", 0))) >= self._settings.taxonomy_ttl

    async def refresh(self) -> None:
        """Fetch, assemble, precompute derived lookups, and publish to disk."""
        series_data = await self._client.get(
            "/series", {"include_volume": "true"}, ttl=self._settings.taxonomy_ttl
        )
        tags_data = await self._client.get(
            "/search/tags_by_categories", ttl=self._settings.taxonomy_ttl
        )

        series: list[dict[str, Any]] = []
        for entry in series_data.get("series") or []:
            if not (isinstance(entry, dict) and entry.get("ticker")):
                continue
            entry["category"] = _norm(entry.get("category"))
            series.append(entry)
        series_by_ticker = {s["ticker"]: s for s in series}

        curated_raw = tags_data.get("tags_by_categories") or {}
        curated_tags = {
            _norm(category): [t for t in (tags or []) if isinstance(t, str) and t]
            for category, tags in curated_raw.items()
            if isinstance(category, str)
        }

        await self._cache.set("tax:series", series)
        await self._cache.set("tax:series_by_ticker", series_by_ticker)
        await self._cache.set("tax:curated_tags", curated_tags)
        await self._cache.set("tax:categories", _compute_categories(series))
        await self._cache.set(
            "tax:category_by_series",
            {ticker: (s.get("category") or "") for ticker, s in series_by_ticker.items()},
        )
        await self._cache.set(
            "tax:frequency_by_series",
            {ticker: (s.get("frequency") or "") for ticker, s in series_by_ticker.items()},
        )
        await self._cache.set(
            "tax:tags_by_series",
            {
                ticker: [t for t in (s.get("tags") or []) if isinstance(t, str)]
                for ticker, s in series_by_ticker.items()
            },
        )
        await self._cache.set("tax:live_feed_by_series", _compute_live_feed(series_by_ticker))
        await self._cache.set("tax:meta", {"fetched_at": time.time()})

    async def categories(self) -> list[dict[str, Any]]:
        return await self._cache.get("tax:categories", []) or []

    async def tags(self, category: str | None = None) -> list[dict[str, Any]]:
        category = _norm(category)
        series = await self._cache.get("tax:series", []) or []
        curated_tags = await self._cache.get("tax:curated_tags", {}) or {}

        in_scope = series
        if category and category != ALL:
            in_scope = [s for s in series if (s.get("category") or "") == category]

        counts: dict[str, int] = {}
        for entry in in_scope:
            for tag in entry.get("tags") or []:
                if isinstance(tag, str) and tag:
                    counts[tag] = counts.get(tag, 0) + 1

        if category and category != ALL:
            curated_order = curated_tags.get(category, [])
        else:
            seen: set[str] = set()
            curated_order = []
            for tags in curated_tags.values():
                for tag in tags:
                    if tag not in seen:
                        seen.add(tag)
                        curated_order.append(tag)

        ordered = [tag for tag in curated_order if tag in counts]
        ordered_set = set(ordered)
        remainder = sorted(
            (tag for tag in counts if tag not in ordered_set),
            key=lambda t: (-counts[t], t.casefold()),
        )
        ordered.extend(remainder)
        return [{"tag": tag, "series_count": counts[tag]} for tag in ordered]

    async def series(
        self,
        category: str | None = None,
        tag: str | None = None,
    ) -> list[dict[str, Any]]:
        category = _norm(category)
        tag = _norm(tag)
        rows = await self._cache.get("tax:series", []) or []
        if category and category != ALL:
            rows = [s for s in rows if (s.get("category") or "") == category]
        if tag and tag != ALL:
            rows = [s for s in rows if tag in (s.get("tags") or [])]
        return sorted(rows, key=lambda s: to_float(s.get("volume_fp")), reverse=True)

    async def get_series(self, ticker: str | None) -> dict[str, Any] | None:
        by_ticker = await self._cache.get("tax:series_by_ticker", {}) or {}
        return by_ticker.get(_norm(ticker))

    async def category_by_series(self) -> dict[str, str]:
        return await self._cache.get("tax:category_by_series", {}) or {}

    async def frequency_by_series(self) -> dict[str, str]:
        return await self._cache.get("tax:frequency_by_series", {}) or {}

    async def tags_by_series(self) -> dict[str, list[str]]:
        return await self._cache.get("tax:tags_by_series", {}) or {}

    async def live_feed_by_series(self) -> dict[str, bool]:
        return await self._cache.get("tax:live_feed_by_series", {}) or {}

    async def default_series_ticker(
        self,
        category: str | None = None,
        tag: str | None = None,
    ) -> str:
        rows = await self.series(category=category, tag=tag)
        return rows[0]["ticker"] if rows else ""
