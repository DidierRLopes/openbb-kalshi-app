"""Application factory."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from kalshi.client import KalshiClient
from kalshi.config import Settings
from kalshi.mcp_server import mcp as kalshi_mcp, set_context as set_mcp_context
from kalshi.routers import discover, events, markets, meta, options
from kalshi.service import MarketDataService
from kalshi.stats import MarketStatsCache
from kalshi.taxonomy import TaxonomyCache


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    mcp_app = kalshi_mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        client = KalshiClient(settings)
        taxonomy = TaxonomyCache(client, settings)
        stats = MarketStatsCache(client, taxonomy, settings)
        app.state.settings = settings
        app.state.client = client
        app.state.taxonomy = taxonomy
        app.state.service = MarketDataService(client, taxonomy, settings)
        app.state.stats = stats
        set_mcp_context(app.state.service, stats, taxonomy)
        warmer = asyncio.create_task(_warm(stats))
        try:
            async with kalshi_mcp.session_manager.run():
                yield
        finally:
            warmer.cancel()
            await client.aclose()

    app = FastAPI(
        title="Kalshi Market Dashboard",
        description="OpenBB Workspace backend for public Kalshi prediction-market data.",
        version="2.0.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_origin_regex=".*",
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    app.include_router(meta.router)
    app.include_router(discover.router)
    app.include_router(options.router)
    app.include_router(events.router)
    app.include_router(markets.router)
    app.mount("/mcp", mcp_app)
    return app


async def _warm(stats: MarketStatsCache) -> None:
    try:
        await stats.ensure_fresh()
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
