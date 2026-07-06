from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from dataclasses import replace

from diskcache import Cache

from kalshi.cache import DiskCache
from kalshi.config import Settings
from kalshi.stats import MarketStatsCache


def _market(
    event_ticker: str = "EVT1",
    ticker: str = "MKT1",
    *,
    volume_total: float = 500.0,
    volume_24h: float = 100.0,
    open_interest: float = 25.0,
    last_price_pct: float = 51.0,
    category: str = "Economics",
    tags: tuple[str, ...] = ("Inflation",),
    subtitle: str = "Yes",
    title: str = "Test market",
    series_ticker: str = "SER",
    close_ts: float | None = None,
    live_feed: bool = False,
    frequency: str = "daily",
) -> dict[str, object]:
    return {
        "market_key": f"{series_ticker}|{ticker}|{event_ticker}",
        "ticker": ticker,
        "event_ticker": event_ticker,
        "series_ticker": series_ticker,
        "category": category,
        "frequency": frequency,
        "tags": list(tags),
        "title": title,
        "subtitle": subtitle,
        "last_price_pct": last_price_pct,
        "yes_bid_pct": 50.0,
        "yes_ask_pct": 52.0,
        "volume_24h": volume_24h,
        "volume_total": volume_total,
        "open_interest": open_interest,
        "volatility": 1.0,
        "close_time": "",
        "close_ts": close_ts,
        "open_ts": None,
        "live_feed": live_feed,
    }


def _raw(
    ticker: str,
    event: str = "KXTEST-25",
    *,
    vol24: object = 100,
    oi: object = 10,
    vol: object = 500,
) -> dict[str, object]:
    return {
        "ticker": ticker,
        "event_ticker": event,
        "title": "Test",
        "yes_sub_title": "Yes",
        "volume_24h_fp": vol24,
        "open_interest_fp": oi,
        "volume_fp": vol,
        "last_price_dollars": "0.51",
        "previous_price_dollars": "0.50",
        "yes_bid_dollars": "0.50",
        "yes_ask_dollars": "0.52",
        "close_time": "2026-08-01T00:00:00Z",
        "open_time": "2026-07-01T00:00:00Z",
    }


class _FakeClient:
    def __init__(self, pages: list[tuple[list, str | None]]) -> None:
        self._pages = pages
        self.calls: list[tuple[str, dict, bool]] = []

    async def get(self, path, params=None, ttl=None, no_store=False):
        self.calls.append((path, dict(params or {}), no_store))
        index = sum(1 for c in self.calls if c[0] == "/markets") - 1
        markets, cursor = self._pages[index]
        return {"markets": markets, "cursor": cursor}


class _FakeTaxonomy:
    def __init__(self) -> None:
        self.refreshed = False

    async def is_stale(self) -> bool:
        return True

    async def refresh(self) -> None:
        self.refreshed = True

    async def category_by_series(self):
        return {"KXTEST": "Economics"}

    async def frequency_by_series(self):
        return {"KXTEST": "daily"}

    async def tags_by_series(self):
        return {"KXTEST": ["Inflation"]}

    async def live_feed_by_series(self):
        return {"KXTEST": False}


class _StatsTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._dir = tempfile.mkdtemp()
        self.cache = DiskCache(Cache(self._dir))
        self.stats = MarketStatsCache(
            client=None, taxonomy=None, cache=self.cache, settings=Settings()
        )

    async def asyncTearDown(self) -> None:
        await self.cache.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    async def _seed(self, chunks: list[list[dict]], generation: int = 1) -> None:
        for index, chunk in enumerate(chunks):
            await self.cache.set(f"stats:gen:{generation}:chunk:{index}", chunk)
        await self.cache.set(
            "stats:meta",
            {"generation": generation, "chunk_count": len(chunks), "fetched_at": time.time()},
        )
        await self.cache.set(
            "stats:generations", [{"gen": generation, "chunk_count": len(chunks)}]
        )


class ReadPathTests(_StatsTestBase):
    async def test_cold_meta_returns_empty(self) -> None:
        self.assertEqual(await self.stats.markets(), [])
        self.assertEqual(await self.stats.by_group(), [])
        self.assertEqual(await self.stats.discover_events(), [])
        self.assertEqual(await self.stats.browse_events(), [])

    async def test_markets_reads_published_snapshot(self) -> None:
        await self._seed([[_market("EVT1"), _market("EVT2", series_ticker="POL", category="Politics")]])
        rows = await self.stats.markets()
        self.assertEqual({r["event_ticker"] for r in rows}, {"EVT1", "EVT2"})

    async def test_markets_streams_across_chunks(self) -> None:
        await self._seed([[_market("EVT1")], [_market("EVT2")], [_market("EVT3")]])
        rows = await self.stats.markets()
        self.assertEqual({r["event_ticker"] for r in rows}, {"EVT1", "EVT2", "EVT3"})

    async def test_markets_filters(self) -> None:
        await self._seed([[
            _market("EVT1", category="Economics", tags=("Inflation",), series_ticker="SER"),
            _market("EVT2", category="Politics", tags=("Election",), series_ticker="POL"),
        ]])
        self.assertEqual(
            [r["event_ticker"] for r in await self.stats.markets(category="Economics")], ["EVT1"]
        )
        self.assertEqual([r["event_ticker"] for r in await self.stats.markets(tag="Election")], ["EVT2"])
        self.assertEqual(
            [r["event_ticker"] for r in await self.stats.markets(series_ticker="POL")], ["EVT2"]
        )

    async def test_close_within_filter(self) -> None:
        soon = time.time() + 3600
        later = time.time() + 30 * 86400
        await self._seed([[_market("EVT1", close_ts=soon), _market("EVT2", close_ts=later)]])
        rows = await self.stats.markets(close_within_days=1)
        self.assertEqual([r["event_ticker"] for r in rows], ["EVT1"])


class AggregatorTests(_StatsTestBase):
    async def test_by_group_category(self) -> None:
        await self._seed([[
            _market("EVT1", category="Economics", volume_24h=100, volume_total=500, open_interest=25),
            _market("EVT2", category="Economics", volume_24h=50, volume_total=200, open_interest=10),
            _market("EVT3", category="Politics", volume_24h=30, volume_total=90, open_interest=5,
                    series_ticker="POL"),
        ]])
        rows = {r["category"]: r for r in await self.stats.by_group("category")}
        self.assertEqual(rows["Economics"]["market_count"], 2)
        self.assertEqual(rows["Economics"]["volume_24h"], 150)
        self.assertEqual(rows["Economics"]["volume_total"], 700)
        self.assertEqual(rows["Politics"]["market_count"], 1)

    async def test_by_group_tag(self) -> None:
        await self._seed([[
            _market("EVT1", tags=("Inflation", "CPI")),
            _market("EVT2", tags=("Inflation",)),
        ]])
        rows = {r["category"]: r for r in await self.stats.by_group("tag")}
        self.assertEqual(rows["Inflation"]["market_count"], 2)
        self.assertEqual(rows["CPI"]["market_count"], 1)

    async def test_discover_events_leading_outcome_and_search(self) -> None:
        await self._seed([[
            _market("EVT1", ticker="A", subtitle="Alpha", volume_total=100, last_price_pct=40),
            _market("EVT1", ticker="B", subtitle="Beta", volume_total=300, last_price_pct=60),
            _market("EVT2", ticker="C", subtitle="Gamma", volume_total=500, title="Other",
                    category="Politics", series_ticker="POL", tags=()),
        ]])
        events = {e["event_ticker"]: e for e in await self.stats.discover_events()}
        self.assertEqual(events["EVT1"]["market_count"], 2)
        self.assertEqual(events["EVT1"]["leading_outcome"], "Beta")
        self.assertEqual(events["EVT1"]["leading_pct"], 60)

        matched = await self.stats.discover_events(search="Gamma")
        self.assertEqual([e["event_ticker"] for e in matched], ["EVT2"])

    async def test_discover_events_no_limit_returns_all(self) -> None:
        await self._seed([[
            _market(f"EVT{i}", ticker=f"M{i}", volume_total=float(i)) for i in range(30)
        ]])
        self.assertEqual(len(await self.stats.discover_events(limit=None)), 30)
        self.assertEqual(len(await self.stats.discover_events(limit=5)), 5)

    async def test_browse_events_returns_all_for_paging(self) -> None:
        await self._seed([[
            _market(f"EVT{i}", ticker=f"M{i}", volume_total=float(i)) for i in range(30)
        ]])
        # browse_events returns the full ordered list; the caller pages it.
        events = await self.stats.browse_events(sort="volume")
        self.assertEqual(len(events), 30)
        self.assertEqual(
            [e["event_ticker"] for e in events[:3]], ["EVT29", "EVT28", "EVT27"]
        )

    async def test_browse_events_bounds_outcomes(self) -> None:
        markets = [
            _market("EVT1", ticker=f"M{i}", subtitle=f"S{i}", volume_total=float(i))
            for i in range(10)
        ]
        await self._seed([markets])
        events = await self.stats.browse_events(outcomes_per_event=4)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["market_count"], 10)
        self.assertEqual(len(event["outcomes"]), 4)
        self.assertEqual([o["volume_total"] for o in event["outcomes"]], [9, 8, 7, 6])
        self.assertEqual(event["volume_total"], sum(range(10)))


class IngestTests(_StatsTestBase):
    def _stats(self, pages: list[tuple[list, str | None]]):
        client = _FakeClient(pages)
        taxonomy = _FakeTaxonomy()
        stats = MarketStatsCache(
            client=client,
            taxonomy=taxonomy,
            cache=self.cache,
            settings=replace(Settings(), stats_page_pause=0.0),
        )
        return stats, client

    async def test_scan_publishes_full_universe_across_pages(self) -> None:
        stats, client = self._stats([
            ([_raw("A"), _raw("B", vol24=0, oi=0)], "cur1"),  # B fails the activity filter
            ([_raw("C")], None),
        ])
        await stats._scan_and_publish()

        rows = await stats.markets()
        self.assertEqual({r["ticker"] for r in rows}, {"A", "C"})
        market_calls = [c for c in client.calls if c[0] == "/markets"]
        self.assertEqual(len(market_calls), 2)
        self.assertTrue(all(c[2] is True for c in market_calls), "scan pages must not be stored")
        meta = await self.cache.get("stats:meta")
        self.assertEqual(meta["generation"], 1)

    async def test_activity_filter_writes_no_chunk_for_empty_page(self) -> None:
        stats, _ = self._stats([([_raw("A", vol24=0, oi=0)], None)])
        await stats._scan_and_publish()
        self.assertEqual(await stats.markets(), [])
        meta = await self.cache.get("stats:meta")
        self.assertEqual(meta["chunk_count"], 0)

    async def test_generation_swap_defers_deletion(self) -> None:
        for _ in range(3):
            stats, _ = self._stats([([_raw("A")], None)])
            await stats._scan_and_publish()

        meta = await self.cache.get("stats:meta")
        self.assertEqual(meta["generation"], 3)
        self.assertIsNone(await self.cache.get("stats:gen:1:chunk:0"))
        self.assertIsNotNone(await self.cache.get("stats:gen:2:chunk:0"))
        self.assertIsNotNone(await self.cache.get("stats:gen:3:chunk:0"))

    async def test_warmup_loads_snapshot_before_returning(self) -> None:
        stats, _ = self._stats([([_raw("A"), _raw("C")], None)])
        await stats.warmup()
        rows = await stats.markets()
        self.assertEqual({r["ticker"] for r in rows}, {"A", "C"})
        self.assertEqual((await self.cache.get("stats:meta"))["generation"], 1)

    async def test_scan_publishes_active_scope(self) -> None:
        stats, _ = self._stats([([_raw("A"), _raw("C")], None)])
        await stats._scan_and_publish()
        scope = await stats.active_scope()
        self.assertEqual(scope["categories"], {"Economics"})
        self.assertEqual(scope["tags"], {"Inflation"})
        self.assertEqual(scope["series"], {"KXTEST"})

    async def test_active_scope_empty_when_cold(self) -> None:
        self.assertEqual(
            await self.stats.active_scope(), {"categories": set(), "tags": set(), "series": set()}
        )

    async def test_sweep_orphans_removes_stale_generations(self) -> None:
        await self.cache.set("stats:gen:2:chunk:0", [_market("OLD")])
        await self.cache.set("stats:gen:4:chunk:0", [_market("KEEP4")])
        await self.cache.set("stats:gen:5:chunk:0", [_market("KEEP5")])
        await self.cache.set(
            "stats:meta", {"generation": 5, "chunk_count": 1, "fetched_at": time.time()}
        )
        await self.stats._sweep_orphans()
        self.assertIsNone(await self.cache.get("stats:gen:2:chunk:0"))
        self.assertIsNotNone(await self.cache.get("stats:gen:4:chunk:0"))
        self.assertIsNotNone(await self.cache.get("stats:gen:5:chunk:0"))


class SettingsTests(unittest.TestCase):
    def test_default_stats_settings(self) -> None:
        self.assertEqual(Settings().stats_scan_max_pages, 200)
        self.assertEqual(Settings().stats_ttl, 1800)
        self.assertEqual(Settings().stats_page_pause, 0.25)


if __name__ == "__main__":
    unittest.main()
