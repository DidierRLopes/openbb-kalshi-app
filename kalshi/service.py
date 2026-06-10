"""Fetch and resolution logic with live, volume-based fallbacks."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException

from kalshi.client import KalshiClient
from kalshi.config import Settings
from kalshi.formatting import build_market_key, clamp_limit, parse_market_key, to_float
from kalshi.taxonomy import TaxonomyCache

MAX_CANDLES = 4900
VALID_CANDLE_INTERVALS = (1, 60, 1440)


def _candle_points(candles: list[dict[str, Any]]) -> list[tuple[int, float]]:
    """(end_ts, YES close %) for each candle that has both."""
    out: list[tuple[int, float]] = []
    for candle in candles:
        ts = candle.get("end_period_ts")
        close = (candle.get("price") or {}).get("close_dollars")
        if ts is not None and close is not None:
            out.append((int(ts), round(to_float(close) * 100, 2)))
    return out


class MarketDataService:
    def __init__(
        self,
        client: KalshiClient,
        taxonomy: TaxonomyCache,
        settings: Settings,
    ) -> None:
        self._client = client
        self._taxonomy = taxonomy
        self._settings = settings

    async def fetch_events_for_series(
        self,
        series_ticker: str,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        data = await self._client.get(
            "/events",
            {
                "series_ticker": series_ticker,
                "status": status,
                "limit": clamp_limit(limit, maximum=200),
                "with_nested_markets": "false",
            },
        )
        events = data.get("events")
        return events if isinstance(events, list) else []

    async def fetch_event(self, event_ticker: str) -> dict[str, Any]:
        data = await self._client.get(f"/events/{event_ticker}")
        event = data.get("event") or {}
        if not event:
            raise HTTPException(status_code=404, detail=f"Event not found: {event_ticker}")
        markets = data.get("markets")
        return {"event": event, "markets": markets if isinstance(markets, list) else []}

    def _bff_cards_url(self) -> str:
        parts = urlsplit(self._settings.api_base_url)
        return f"{parts.scheme}://{parts.netloc}/v1/bff/cards"

    async def card_images(
        self,
        event_tickers: list[str],
        light: bool = False,
    ) -> dict[str, dict[str, str]]:
        """Per-market image and accent colour, keyed by market ticker."""
        tickers = [t for t in dict.fromkeys(event_tickers) if t]
        if not tickers:
            return {}
        url = self._bff_cards_url()
        chunks = [tickers[i : i + 25] for i in range(0, len(tickers), 25)]

        async def fetch(chunk: list[str]) -> dict[str, Any]:
            try:
                return await self._client.get(url, {"event_tickers": ",".join(chunk)}, ttl=600)
            except HTTPException:
                return {}

        pages = await asyncio.gather(*[fetch(chunk) for chunk in chunks])
        images: dict[str, dict[str, str]] = {}
        for page in pages:
            for card in page.get("cards") or []:
                for market in card.get("markets") or []:
                    ticker = market.get("ticker")
                    if not ticker:
                        continue
                    image = (
                        market.get("image_url_light_mode" if light else "image_url_dark_mode")
                        or market.get("image_url")
                        or ""
                    )
                    color = (
                        market.get("background_color_light_mode" if light else "background_color_dark_mode")
                        or market.get("background_color")
                        or ""
                    )
                    images[ticker] = {"image_url": image, "color": color}
        return images

    async def fetch_document(self, market_key: str | None, doc: str) -> tuple[bytes, str]:
        """Download a market's rules / contract-terms PDF."""
        selected = await self.resolve_market(market_key)
        series = await self._taxonomy.get_series(selected["series_ticker"]) or {}
        key = "contract_terms_url" if doc == "terms" else "contract_url"
        url = series.get(key)
        if not url:
            raise HTTPException(status_code=404, detail="Document not available for this market.")
        return await self._client.download(str(url))

    async def fetch_market(self, market_ticker: str) -> dict[str, Any]:
        data = await self._client.get(f"/markets/{market_ticker}")
        market = data.get("market") or {}
        if not market:
            raise HTTPException(status_code=404, detail=f"Market not found: {market_ticker}")
        return market

    async def fetch_trades(
        self,
        market_ticker: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": clamp_limit(limit, maximum=1000)}
        if market_ticker:
            params["ticker"] = market_ticker
        data = await self._client.get("/markets/trades", params, ttl=self._settings.realtime_ttl)
        trades = data.get("trades")
        return trades if isinstance(trades, list) else []

    async def fetch_orderbook(self, market_ticker: str, depth: int = 10) -> dict[str, Any]:
        data = await self._client.get(
            f"/markets/{market_ticker}/orderbook",
            {"depth": clamp_limit(depth, maximum=100)},
            ttl=self._settings.realtime_ttl,
        )
        return data.get("orderbook_fp") or {}

    async def fetch_live_data(self, event_ticker: str) -> dict[str, Any]:
        """Kalshi's live underlying-asset timeseries for an event."""
        ticker = (event_ticker or "").strip()
        if not ticker:
            return {}
        parts = urlsplit(self._settings.api_base_url)
        url = f"{parts.scheme}://{parts.netloc}/v1/live_data/events/{ticker}"
        try:
            data = await self._client.get(url, ttl=4)
        except HTTPException:
            return {}
        return (data.get("live_data") or {}).get("details") or {}

    async def fetch_candlesticks(
        self,
        series_ticker: str,
        market_ticker: str,
        days: int,
        period_interval: int,
    ) -> list[dict[str, Any]]:
        interval = period_interval if period_interval in VALID_CANDLE_INTERVALS else 1440
        max_days = max(1, (MAX_CANDLES * interval) // 1440)
        bounded_days = max(1, min(int(days), max_days))
        bucket = interval * 60
        now = int(datetime.now(timezone.utc).timestamp())
        end_ts = (now // bucket) * bucket
        start_ts = end_ts - bounded_days * 86400
        try:
            data = await self._client.get(
                f"/series/{series_ticker}/markets/{market_ticker}/candlesticks",
                {
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                    "period_interval": interval,
                    "include_latest_before_start": "true",
                },
                ttl=30,
            )
        except HTTPException as exc:
            if exc.status_code in (400, 404):
                return []
            raise
        candles = data.get("candlesticks")
        return candles if isinstance(candles, list) else []

    async def effective_series_ticker(
        self,
        category: str | None = None,
        tag: str | None = None,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        market_key: str | None = None,
    ) -> str:
        """Resolve the series in scope from the most specific selection available."""
        candidates = [parse_market_key(market_key)["event_ticker"], (event_ticker or "")]
        for candidate in candidates:
            prefix = candidate.strip().split("-")[0]
            if prefix and await self._taxonomy.get_series(prefix):
                return prefix
        series = (series_ticker or "").strip()
        if series and series != "All":
            return series
        cat = (category or "").strip()
        tg = (tag or "").strip()
        if (cat and cat != "All") or (tg and tg != "All"):
            return await self._taxonomy.default_series_ticker(category, tag)
        return ""

    async def outcome_histories(
        self,
        series_ticker: str,
        markets: list[dict[str, Any]],
        top_n: int = 3,
        pinned_ticker: str = "",
    ) -> list[dict[str, Any]]:
        """YES-probability series for the top markets of an event."""
        ranked = sorted(
            markets,
            key=lambda m: to_float(m.get("last_price_dollars")) or to_float(m.get("yes_bid_dollars")),
            reverse=True,
        )
        chosen = ranked[:top_n]
        if pinned_ticker and pinned_ticker not in {m.get("ticker") for m in chosen}:
            pinned = next((m for m in markets if m.get("ticker") == pinned_ticker), None)
            if pinned:
                chosen = [*chosen, pinned]
        limiter = asyncio.Semaphore(2)
        cutoff = datetime.now(timezone.utc).timestamp() - 86400

        async def history(market: dict[str, Any]) -> dict[str, Any]:
            ticker = market.get("ticker", "")
            async with limiter:
                try:
                    minute = await self.fetch_candlesticks(series_ticker, ticker, 1, 1)
                except HTTPException:
                    minute = []
                try:
                    hourly = await self.fetch_candlesticks(series_ticker, ticker, 30, 60)
                except HTTPException:
                    hourly = []
            by_ts: dict[int, float] = {ts: val for ts, val in _candle_points(hourly) if ts < cutoff}
            by_ts.update(dict(_candle_points(minute)))
            return {
                "ticker": ticker,
                "name": market.get("yes_sub_title") or market.get("subtitle") or ticker,
                "points": [[ts, val] for ts, val in sorted(by_ts.items())],
            }

        return list(await asyncio.gather(*[history(m) for m in chosen]))

    async def _pick_event_ticker(
        self,
        series_ticker: str | None,
        category: str | None,
        tag: str | None,
    ) -> str:
        series = (series_ticker or "").strip()
        if not series:
            series = await self._taxonomy.default_series_ticker(category, tag)
        if not series:
            return ""
        for status in ("open", None):
            events = await self.fetch_events_for_series(series, status=status, limit=1)
            if events:
                return events[0].get("event_ticker", "")
        return ""

    async def default_market_key(self, event_ticker: str) -> str:
        """The top market (by volume) of an event, as a market_key."""
        if not (event_ticker or "").strip():
            return ""
        try:
            resolved = await self.resolve_event(event_ticker=event_ticker)
        except HTTPException:
            return ""
        markets = sorted(resolved["markets"], key=lambda m: to_float(m.get("volume_fp")), reverse=True)
        if not markets:
            return ""
        return build_market_key(
            resolved["series_ticker"], markets[0].get("ticker", ""), resolved["event_ticker"]
        )

    async def resolve_event(
        self,
        event_ticker: str | None = None,
        series_ticker: str | None = None,
        category: str | None = None,
        tag: str | None = None,
    ) -> dict[str, Any]:
        ticker = (event_ticker or "").strip()
        if not ticker:
            ticker = await self._pick_event_ticker(series_ticker, category, tag)
        if not ticker:
            raise HTTPException(
                status_code=404,
                detail="No Kalshi events available for the current selection.",
            )
        resolved = await self.fetch_event(ticker)
        event = resolved["event"]
        return {
            "event_ticker": ticker,
            "event": event,
            "markets": resolved["markets"],
            "series_ticker": event.get("series_ticker", series_ticker or ""),
        }

    async def resolve_market(self, market_key: str | None) -> dict[str, Any]:
        parsed = parse_market_key(market_key)
        market_ticker = parsed["market_ticker"]
        event_ticker = parsed["event_ticker"]
        series_ticker = parsed["series_ticker"]
        market: dict[str, Any] = {}

        if market_ticker:
            try:
                market = await self.fetch_market(market_ticker)
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                market_ticker = ""

        if not market_ticker:
            try:
                resolved = await self.resolve_event(event_ticker=event_ticker or None)
            except HTTPException:
                resolved = await self.resolve_event(event_ticker=None)
            markets = sorted(
                resolved["markets"],
                key=lambda m: to_float(m.get("volume_24h_fp")),
                reverse=True,
            )
            if not markets:
                raise HTTPException(
                    status_code=404,
                    detail=f"No markets available for event {resolved['event_ticker']}.",
                )
            market = markets[0]
            market_ticker = market.get("ticker", "")
            event_ticker = resolved["event_ticker"]
            series_ticker = resolved["series_ticker"]

        event_ticker = event_ticker or market.get("event_ticker", "")
        event: dict[str, Any] = {}
        if event_ticker:
            try:
                resolved = await self.fetch_event(event_ticker)
                event = resolved["event"]
                series_ticker = series_ticker or event.get("series_ticker", "")
            except HTTPException:
                event = {}

        return {
            "series_ticker": series_ticker,
            "market_ticker": market_ticker,
            "event_ticker": event_ticker,
            "market": market,
            "event": event,
            "market_key": build_market_key(series_ticker, market_ticker, event_ticker),
        }
