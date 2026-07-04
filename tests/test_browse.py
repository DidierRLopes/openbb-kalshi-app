from __future__ import annotations

import unittest

from kalshi.browse import render_browse


class BrowseRenderTests(unittest.TestCase):
    def test_caption_can_show_bounded_active_events_without_total(self) -> None:
        html = render_browse(
            [
                {
                    "event_ticker": "EVT1",
                    "series_ticker": "SER",
                    "title": "Event",
                    "category": "Economics",
                    "tags": [],
                    "outcomes": [],
                    "volume_total": 100,
                    "market_count": 1,
                }
            ],
            rows=[],
            param_defs=[],
            total=None,
            search="",
            theme="dark",
        )

        self.assertIn("Showing 1 active events", html)
        self.assertNotIn("1 of 1 active events", html)
