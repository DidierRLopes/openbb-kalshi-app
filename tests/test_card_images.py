from __future__ import annotations

import shutil
import tempfile
import unittest
from dataclasses import replace

from diskcache import Cache

from kalshi.cache import DiskCache
from kalshi.config import Settings
from kalshi.service import MarketDataService


class _FakeClient:
    def __init__(self) -> None:
        self.bff_calls = 0

    async def get(self, path, params=None, ttl=None, no_store=False):
        self.bff_calls += 1
        event_tickers = [t for t in (params or {}).get("event_tickers", "").split(",") if t]
        return {
            "cards": [
                {
                    "event_ticker": et,
                    "event_title": f"{et} Question?",
                    "event_subtitle": f"{et} sub",
                    "markets": [
                        {
                            "ticker": f"{et}-M1",
                            "image_url_light_mode": f"{et}-light.png",
                            "image_url_dark_mode": f"{et}-dark.png",
                            "background_color_light_mode": "#ffffff",
                            "background_color_dark_mode": "#000000",
                        }
                    ],
                }
                for et in event_tickers
            ]
        }


class CardImageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._dir = tempfile.mkdtemp()
        self.cache = DiskCache(Cache(self._dir))
        self.client = _FakeClient()
        self.service = MarketDataService(
            self.client, None, replace(Settings(), stats_page_pause=0.0), self.cache
        )

    async def asyncTearDown(self) -> None:
        await self.cache.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    async def test_warm_persists_both_themes_and_serves_without_live_calls(self) -> None:
        warmed = await self.service.warm_card_images(["EVT1", "EVT2"])
        self.assertEqual(warmed, 2)

        calls_after_warm = self.client.bff_calls
        dark = await self.service.card_images(["EVT1"], light=False)
        light = await self.service.card_images(["EVT1"], light=True)

        self.assertEqual(dark["EVT1-M1"], {"image_url": "EVT1-dark.png", "color": "#000000"})
        self.assertEqual(light["EVT1-M1"], {"image_url": "EVT1-light.png", "color": "#ffffff"})
        self.assertEqual(self.client.bff_calls, calls_after_warm, "warmed events must not refetch")

    async def test_warm_persists_event_titles(self) -> None:
        await self.service.warm_card_images(["EVT1", "EVT2"])
        meta = await self.service.event_meta()
        self.assertEqual(meta["EVT1"], {"title": "EVT1 Question?", "subtitle": "EVT1 sub"})
        self.assertEqual(meta["EVT2"]["title"], "EVT2 Question?")

    async def test_unwarmed_event_falls_back_to_live_fetch(self) -> None:
        await self.service.warm_card_images(["EVT1"])
        before = self.client.bff_calls
        images = await self.service.card_images(["EVT3"])
        self.assertEqual(images["EVT3-M1"]["image_url"], "EVT3-dark.png")
        self.assertGreater(self.client.bff_calls, before)

    async def test_no_cache_still_serves_live(self) -> None:
        service = MarketDataService(self.client, None, Settings(), None)
        images = await service.card_images(["EVT1"], light=True)
        self.assertEqual(images["EVT1-M1"]["image_url"], "EVT1-light.png")


if __name__ == "__main__":
    unittest.main()
