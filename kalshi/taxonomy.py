"""Cached category/tag/series taxonomy assembled from /series and
/search/tags_by_categories. Every dropdown choice derives from this cache."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from kalshi.client import KalshiClient
from kalshi.config import Settings
from kalshi.formatting import to_float

ALL = "All"


def _norm(value: str | None) -> str:
    return (value or "").strip()


class TaxonomyCache:
    def __init__(self, client: KalshiClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings
        self._lock = asyncio.Lock()
        self._loaded = False
        self._fetched_at = 0.0
        self._series: list[dict[str, Any]] = []
        self._series_by_ticker: dict[str, dict[str, Any]] = {}
        self._curated_tags: dict[str, list[str]] = {}

    def _fresh(self) -> bool:
        return self._loaded and (time.monotonic() - self._fetched_at < self._settings.taxonomy_ttl)

    async def _ensure_fresh(self) -> None:
        if self._fresh():
            return
        async with self._lock:
            if self._fresh():
                return

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
            self._series = series
            self._series_by_ticker = {s["ticker"]: s for s in series}

            curated = tags_data.get("tags_by_categories") or {}
            self._curated_tags = {
                _norm(category): [t for t in (tags or []) if isinstance(t, str) and t]
                for category, tags in curated.items()
                if isinstance(category, str)
            }
            self._fetched_at = time.monotonic()
            self._loaded = True

    async def categories(self) -> list[dict[str, Any]]:
        await self._ensure_fresh()
        stats: dict[str, dict[str, float]] = {}
        for series in self._series:
            category = series.get("category") or ""
            if not category:
                continue
            bucket = stats.setdefault(category, {"count": 0.0, "volume": 0.0})
            bucket["count"] += 1
            bucket["volume"] += to_float(series.get("volume_fp"))
        return [
            {"category": category, "series_count": int(s["count"]), "volume": s["volume"]}
            for category, s in sorted(
                stats.items(), key=lambda kv: (-kv[1]["volume"], kv[0].casefold())
            )
        ]

    async def tags(self, category: str | None = None) -> list[dict[str, Any]]:
        await self._ensure_fresh()
        category = _norm(category)
        in_scope = await self.series(category=category)

        counts: dict[str, int] = {}
        for series in in_scope:
            for tag in series.get("tags") or []:
                if isinstance(tag, str) and tag:
                    counts[tag] = counts.get(tag, 0) + 1

        if category and category != ALL:
            curated_order = self._curated_tags.get(category, [])
        else:
            seen: set[str] = set()
            curated_order = []
            for tags in self._curated_tags.values():
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
        await self._ensure_fresh()
        category = _norm(category)
        tag = _norm(tag)
        rows = self._series
        if category and category != ALL:
            rows = [s for s in rows if (s.get("category") or "") == category]
        if tag and tag != ALL:
            rows = [s for s in rows if tag in (s.get("tags") or [])]
        return sorted(rows, key=lambda s: to_float(s.get("volume_fp")), reverse=True)

    async def get_series(self, ticker: str | None) -> dict[str, Any] | None:
        await self._ensure_fresh()
        return self._series_by_ticker.get(_norm(ticker))

    async def category_by_series(self) -> dict[str, str]:
        await self._ensure_fresh()
        return {ticker: (s.get("category") or "") for ticker, s in self._series_by_ticker.items()}

    async def frequency_by_series(self) -> dict[str, str]:
        await self._ensure_fresh()
        return {ticker: (s.get("frequency") or "") for ticker, s in self._series_by_ticker.items()}

    async def tags_by_series(self) -> dict[str, list[str]]:
        await self._ensure_fresh()
        return {
            ticker: [t for t in (s.get("tags") or []) if isinstance(t, str)]
            for ticker, s in self._series_by_ticker.items()
        }

    async def live_feed_by_series(self) -> dict[str, bool]:
        """True for series whose underlying settles on a real-time price feed (Pyth)
        — these are the events Kalshi exposes a continuous live_data timeseries for."""
        await self._ensure_fresh()
        result: dict[str, bool] = {}
        for ticker, series in self._series_by_ticker.items():
            names = " ".join(
                str(src.get("name", ""))
                for src in (series.get("settlement_sources") or [])
                if isinstance(src, dict)
            ).lower()
            result[ticker] = "pyth" in names
        return result

    async def default_series_ticker(
        self,
        category: str | None = None,
        tag: str | None = None,
    ) -> str:
        rows = await self.series(category=category, tag=tag)
        return rows[0]["ticker"] if rows else ""
