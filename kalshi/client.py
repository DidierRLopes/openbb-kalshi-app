"""Cached async HTTP client for the Kalshi public market-data API."""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from typing import Any

import httpx
from fastapi import HTTPException

from kalshi.config import Settings


def _clean_params(params: dict[str, Any] | None) -> dict[str, Any]:
    return {
        key: value
        for key, value in (params or {}).items()
        if value is not None and value != ""
    }


class RateLimiter:
    """Token-bucket limiter shared across every request to keep us under the
    upstream per-second cap (and avoid 429s). Allows a short burst up to the
    rate, then paces to it."""

    def __init__(self, rate_per_sec: float) -> None:
        self._rate = max(1.0, rate_per_sec)
        self._tokens = self._rate
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(self._rate, self._tokens + (now - self._updated) * self._rate)
                self._updated = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1 - self._tokens) / self._rate
            await asyncio.sleep(wait)


class KalshiClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cache: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()
        self._max_entries = settings.cache_max_entries
        self._limiter = RateLimiter(settings.rate_limit_per_sec)
        self._client = httpx.AsyncClient(
            base_url=settings.api_base_url,
            headers={"User-Agent": settings.user_agent},
            timeout=settings.http_timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _cache_key(path: str, params: dict[str, Any]) -> str:
        return json.dumps([path, params], sort_keys=True)

    def _store(self, key: str, value: Any, now: float) -> None:
        self._cache[key] = (now, value)
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)

    async def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        ttl: int | None = None,
    ) -> dict[str, Any]:
        ttl = self._settings.quote_ttl if ttl is None else ttl
        clean = _clean_params(params)
        key = self._cache_key(path, clean)
        now = time.monotonic()

        cached = self._cache.get(key)
        if cached is not None and now - cached[0] < ttl:
            self._cache.move_to_end(key)
            return cached[1]

        response = await self._request(path, clean)
        data = response.json()
        self._store(key, data, time.monotonic())
        return data

    async def download(self, url: str, ttl: int = 3600) -> tuple[bytes, str]:
        """Raw bytes + content-type for an arbitrary document URL (e.g. a rules
        PDF), fetched through the shared client so it shares the rate limiter,
        timeout, and LRU cache."""
        key = self._cache_key("DOWNLOAD", {"url": url})
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None and now - cached[0] < ttl:
            self._cache.move_to_end(key)
            return cached[1]
        response = await self._request(url, {})
        result = (response.content, response.headers.get("content-type", "application/octet-stream"))
        self._store(key, result, time.monotonic())
        return result

    async def _request(self, path: str, params: dict[str, Any]) -> httpx.Response:
        for attempt in range(3):
            await self._limiter.acquire()
            try:
                response = await self._client.get(path, params=params)
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                if status_code == 429 and attempt < 2:
                    retry_after = exc.response.headers.get("Retry-After")
                    delay = float(retry_after) if (retry_after or "").isdigit() else 1.0 * (attempt + 1)
                    await asyncio.sleep(delay)
                    continue
                raise HTTPException(
                    status_code=502 if status_code >= 500 else status_code,
                    detail=f"Kalshi API error for {path}: {exc.response.text[:300]}",
                ) from exc
            except httpx.HTTPError as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"Kalshi API request failed for {path}: {exc}",
                ) from exc
        raise HTTPException(status_code=502, detail=f"Kalshi API rate limited for {path}")
