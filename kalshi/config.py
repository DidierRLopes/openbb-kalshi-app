"""Runtime configuration resolved from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT_PATH = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT_PATH / ".env")
except ImportError:
    pass

DEFAULT_CORS_ORIGINS = (
    "https://pro.openbb.co",
    "https://pro.openbb.dev",
    "https://excel.openbb.co",
    "https://excel.openbb.dev",
    "tauri://localhost",
    "http://localhost:1420",
    "http://localhost:7779",
    "https://127.0.0.1:7779",
)


@dataclass(frozen=True)
class Settings:
    api_base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    user_agent: str = "OpenBB-Kalshi-Market-Dashboard/2.0"
    public_base_url: str = ""
    http_timeout: float = 20.0
    rate_limit_per_sec: float = 8.0

    quote_ttl: int = 30
    realtime_ttl: int = 10
    taxonomy_ttl: int = 600

    # On-disk diskcache lives here (a mounted volume in production). The `http`
    # response cache and the durable `data` snapshot are subdirectories of it.
    cache_dir: str = str(ROOT_PATH / ".cache")
    http_cache_size_limit_mb: int = 256

    stats_ttl: int = 1800
    stats_scan_max_pages: int = 200
    stats_page_pause: float = 0.25

    cors_origins: tuple[str, ...] = field(default=DEFAULT_CORS_ORIGINS)

    @classmethod
    def from_env(cls) -> "Settings":
        def _int(name: str, default: int) -> int:
            try:
                return int(os.getenv(name, str(default)))
            except (TypeError, ValueError):
                return default

        return cls(
            api_base_url=os.getenv("KALSHI_API_BASE_URL", cls.api_base_url).rstrip("/"),
            public_base_url=os.getenv("KALSHI_PUBLIC_BASE_URL", cls.public_base_url).rstrip("/"),
            http_timeout=float(os.getenv("KALSHI_HTTP_TIMEOUT", cls.http_timeout)),
            rate_limit_per_sec=float(os.getenv("KALSHI_RATE_LIMIT_PER_SEC", cls.rate_limit_per_sec)),
            quote_ttl=_int("KALSHI_QUOTE_TTL_SECONDS", cls.quote_ttl),
            realtime_ttl=_int("KALSHI_REALTIME_TTL_SECONDS", cls.realtime_ttl),
            taxonomy_ttl=_int("KALSHI_TAXONOMY_TTL_SECONDS", cls.taxonomy_ttl),
            cache_dir=os.getenv("KALSHI_CACHE_DIR", cls.cache_dir),
            http_cache_size_limit_mb=_int(
                "KALSHI_HTTP_CACHE_SIZE_LIMIT_MB", cls.http_cache_size_limit_mb
            ),
            stats_ttl=_int("KALSHI_STATS_TTL_SECONDS", cls.stats_ttl),
            stats_scan_max_pages=_int("KALSHI_STATS_SCAN_MAX_PAGES", cls.stats_scan_max_pages),
            stats_page_pause=float(os.getenv("KALSHI_STATS_PAGE_PAUSE_SECONDS", cls.stats_page_pause)),
        )
