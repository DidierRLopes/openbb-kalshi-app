from __future__ import annotations

import asyncio
import time
import unittest
from dataclasses import replace

from kalshi.config import Settings
from kalshi.stats import MarketStatsCache


class _Client:
    pass


class _Taxonomy:
    pass


def _market(event_ticker: str = "EVT1") -> dict[str, object]:
    return {
        "market_key": f"SER|MKT|{event_ticker}",
        "ticker": "MKT",
        "event_ticker": event_ticker,
        "series_ticker": "SER",
        "category": "Economics",
        "frequency": "daily",
        "tags": ["Inflation"],
        "title": "Test market",
        "subtitle": "Yes",
        "last_price_pct": 51.0,
        "yes_bid_pct": 50.0,
        "yes_ask_pct": 52.0,
        "volume_24h": 100.0,
        "volume_total": 500.0,
        "open_interest": 25.0,
        "volatility": 1.0,
        "close_time": "",
        "close_ts": None,
        "open_ts": None,
        "live_feed": False,
    }


class MarketStatsCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        task = getattr(self, "refresh_task", None)
        if task and not task.done():
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_markets_returns_empty_while_initial_refresh_runs(self) -> None:
        stats = MarketStatsCache(_Client(), _Taxonomy(), Settings())
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_scan() -> None:
            started.set()
            await release.wait()
            stats._markets = [_market()]
            stats._loaded = True
            stats._fetched_at = time.monotonic()

        stats._scan = slow_scan  # type: ignore[method-assign]
        self.refresh_task = asyncio.create_task(stats.ensure_fresh())
        await started.wait()

        start = time.perf_counter()
        rows = await stats.markets()
        elapsed = time.perf_counter() - start

        self.assertEqual(rows, [])
        self.assertLess(elapsed, 0.05)

        release.set()
        await self.refresh_task

    async def test_markets_serves_stale_data_and_refreshes_in_background(self) -> None:
        settings = replace(Settings(), stats_ttl=1)
        stats = MarketStatsCache(_Client(), _Taxonomy(), settings)
        stats._markets = [_market()]
        stats._loaded = True
        stats._fetched_at = time.monotonic() - 60
        scanned = asyncio.Event()

        async def scan() -> None:
            scanned.set()
            stats._markets = [_market("EVT2")]
            stats._loaded = True
            stats._fetched_at = time.monotonic()

        stats._scan = scan  # type: ignore[method-assign]

        rows = await stats.markets()

        self.assertEqual([row["event_ticker"] for row in rows], ["EVT1"])
        self.assertTrue(stats.refreshing)
        await asyncio.wait_for(scanned.wait(), timeout=1)
        if stats._refresh_task:
            await stats._refresh_task

        fresh_rows = await stats.markets()
        self.assertEqual([row["event_ticker"] for row in fresh_rows], ["EVT2"])

    def test_default_stats_scan_is_bounded_for_dashboard(self) -> None:
        self.assertEqual(Settings().stats_scan_max_pages, 10)
        self.assertEqual(Settings().stats_ttl, 1800)
