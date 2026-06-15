"""FastAPI accessors for the shared singletons on app.state."""

from __future__ import annotations

from fastapi import Request

from kalshi.client import KalshiClient
from kalshi.service import MarketDataService
from kalshi.stats import MarketStatsCache
from kalshi.taxonomy import TaxonomyCache


def get_client(request: Request) -> KalshiClient:
    return request.app.state.client


def get_taxonomy(request: Request) -> TaxonomyCache:
    return request.app.state.taxonomy


def get_service(request: Request) -> MarketDataService:
    return request.app.state.service


def get_stats(request: Request) -> MarketStatsCache:
    return request.app.state.stats


def resolve_base_url(request: Request) -> str:
    """Return the browser-facing base URL."""
    settings = request.app.state.settings
    return (settings.public_base_url or str(request.base_url)).rstrip("/")
