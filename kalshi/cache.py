"""Async wrapper over `diskcache`, the single on-disk cache backend.

Two caches live under `settings.cache_dir` (a mounted volume in production):

- `http` — a size-limited `FanoutCache` of upstream HTTP responses. Volatile:
  entries carry a TTL and are evicted under size pressure.
- `data` — a `Cache` holding the durable taxonomy and market snapshot. Never
  evicted; bounded instead by the stats generation swap.

Every diskcache call is blocking SQLite I/O, so we run it in a worker thread to
keep the event loop responsive while the ingestor writes large chunks.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from diskcache import Cache, FanoutCache

from kalshi.config import Settings


class DiskCache:
    def __init__(self, backend: Cache | FanoutCache, *, retry: bool = False) -> None:
        self._backend = backend
        self._retry = retry

    async def get(self, key: str, default: Any = None) -> Any:
        return await asyncio.to_thread(self._backend.get, key, default, retry=self._retry)

    async def set(self, key: str, value: Any, expire: float | None = None) -> bool:
        return await asyncio.to_thread(
            self._backend.set, key, value, expire=expire, retry=self._retry
        )

    async def add(self, key: str, value: Any, expire: float | None = None) -> bool:
        """Set only if the key is absent (returns False if it already exists)."""
        return await asyncio.to_thread(
            self._backend.add, key, value, expire=expire, retry=self._retry
        )

    async def delete(self, key: str) -> bool:
        return await asyncio.to_thread(self._backend.delete, key, retry=self._retry)

    async def iter_keys(self) -> list[str]:
        """Snapshot of every key. Full enumeration — startup/maintenance only."""
        return await asyncio.to_thread(lambda: list(self._backend))

    async def close(self) -> None:
        await asyncio.to_thread(self._backend.close)


def open_caches(settings: Settings) -> tuple[DiskCache, DiskCache]:
    """Open the `http` and `data` caches under `settings.cache_dir`."""
    root = Path(settings.cache_dir)
    http = FanoutCache(
        str(root / "http"),
        shards=8,
        timeout=1.0,
        size_limit=settings.http_cache_size_limit_mb * 1024 * 1024,
        eviction_policy="least-recently-stored",
        sqlite_journal_mode="wal",
    )
    data = Cache(
        str(root / "data"),
        timeout=60,
        eviction_policy="none",
        sqlite_journal_mode="wal",
    )
    return DiskCache(http), DiskCache(data, retry=True)
