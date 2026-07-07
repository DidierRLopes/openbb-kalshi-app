"""Application factory."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from kalshi.cache import open_caches
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
        http_cache, data_cache = open_caches(settings)
        client = KalshiClient(settings, http_cache)
        taxonomy = TaxonomyCache(client, data_cache, settings)
        service = MarketDataService(client, taxonomy, settings, data_cache)
        stats = MarketStatsCache(client, taxonomy, data_cache, settings, service)
        app.state.settings = settings
        app.state.http_cache = http_cache
        app.state.data_cache = data_cache
        app.state.client = client
        app.state.taxonomy = taxonomy
        app.state.service = service
        app.state.stats = stats
        set_mcp_context(service, stats, taxonomy)
        await stats.warmup()
        ingestor = asyncio.create_task(stats.run_ingest_loop())
        try:
            async with kalshi_mcp.session_manager.run():
                yield
        finally:
            ingestor.cancel()
            try:
                await asyncio.wait_for(ingestor, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
            await client.aclose()
            await http_cache.close()
            await data_cache.close()

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
        allow_private_network=True,
        expose_headers=["*"],
    )

    app.include_router(meta.router)
    app.include_router(discover.router)
    app.include_router(options.router)
    app.include_router(events.router)
    app.include_router(markets.router)
    app.mount("/mcp", mcp_app)
    return app
