"""Fetch and resolution logic with live, volume-based fallbacks."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException

from kalshi.cache import DiskCache
from kalshi.client import KalshiClient
from kalshi.config import Settings
from kalshi.formatting import build_market_key, clamp_limit, parse_market_key, to_float
from kalshi.taxonomy import TaxonomyCache

MAX_CANDLES = 4900
VALID_CANDLE_INTERVALS = (1, 60, 1440)
BATCH_CANDLESTICK_MARKETS = 10


def _candlestick_params(days: int, period_interval: int) -> dict[str, Any]:
    interval = period_interval if period_interval in VALID_CANDLE_INTERVALS else 1440
    max_days = max(1, (MAX_CANDLES * interval) // 1440)
    bounded_days = max(1, min(int(days), max_days))
    bucket = interval * 60
    now = int(datetime.now(timezone.utc).timestamp())
    end_ts = (now // bucket) * bucket
    return {
        "start_ts": end_ts - bounded_days * 86400,
        "end_ts": end_ts,
        "period_interval": interval,
        "include_latest_before_start": "true",
    }


def _candle_points(candles: list[dict[str, Any]]) -> list[tuple[int, float]]:
    """(end_ts, YES close %) for each candle that has both."""
    out: list[tuple[int, float]] = []
    for candle in candles:
        ts = candle.get("end_period_ts")
        price = candle.get("price") or {}
        close = price.get("close_dollars") or price.get("close") or price.get("previous_dollars")
        if close is None:
            bid = (candle.get("yes_bid") or {}).get("close_dollars")
            ask = (candle.get("yes_ask") or {}).get("close_dollars")
            bid_value = to_float(bid) if bid is not None else None
            ask_value = to_float(ask) if ask is not None else None
            if bid_value is not None and ask_value is not None:
                close = (bid_value + ask_value) / 2
            else:
                close = bid_value if bid_value is not None else ask_value
        if ts is not None and close is not None:
            out.append((int(ts), round(to_float(close) * 100, 2)))
    return out


CARD_IMAGES_KEY = "cards:images"
EVENT_META_KEY = "cards:event_meta"
BFF_CARDS_CHUNK = 25


class MarketDataService:
    def __init__(
        self,
        client: KalshiClient,
        taxonomy: TaxonomyCache,
        settings: Settings,
        cache: DiskCache | None = None,
    ) -> None:
        self._client = client
        self._taxonomy = taxonomy
        self._settings = settings
        self._cache = cache

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

    @staticmethod
    def _card_entry(market: dict[str, Any]) -> dict[str, str]:
        """Both light and dark image + accent colour for one market card."""
        return {
            "il": market.get("image_url_light_mode") or market.get("image_url") or "",
            "cl": market.get("background_color_light_mode") or market.get("background_color") or "",
            "id": market.get("image_url_dark_mode") or market.get("image_url") or "",
            "cd": market.get("background_color_dark_mode") or market.get("background_color") or "",
        }

    async def card_images(
        self,
        event_tickers: list[str],
        light: bool = False,
    ) -> dict[str, dict[str, str]]:
        """Per-market image and accent colour, keyed by market ticker. Served from
        the warmed cache; any events not yet warmed are fetched live."""
        tickers = [t for t in dict.fromkeys(event_tickers) if t]
        if not tickers:
            return {}
        blob = (self._cache and await self._cache.get(CARD_IMAGES_KEY)) or {}
        image_key, color_key = ("il", "cl") if light else ("id", "cd")
        images: dict[str, dict[str, str]] = {}
        missing: list[str] = []
        for event_ticker in tickers:
            cards = blob.get(event_ticker)
            if cards is None:
                missing.append(event_ticker)
                continue
            for market_ticker, entry in cards.items():
                images[market_ticker] = {
                    "image_url": entry.get(image_key, ""),
                    "color": entry.get(color_key, ""),
                }
        if missing:
            fetched_cards, _ = await self._fetch_cards(missing)
            for event_ticker, cards in fetched_cards.items():
                for market_ticker, entry in cards.items():
                    images[market_ticker] = {
                        "image_url": entry.get(image_key, ""),
                        "color": entry.get(color_key, ""),
                    }
        return images

    async def _fetch_cards(
        self, event_tickers: list[str], *, no_store: bool = False
    ) -> tuple[dict[str, dict[str, dict[str, str]]], dict[str, dict[str, str]]]:
        """Card images `{event_ticker: {market_ticker: entry}}` (both themes) and
        event metadata `{event_ticker: {title, subtitle}}` from the BFF cards API."""
        url = self._bff_cards_url()
        chunks = [
            event_tickers[i : i + BFF_CARDS_CHUNK]
            for i in range(0, len(event_tickers), BFF_CARDS_CHUNK)
        ]

        async def fetch(chunk: list[str]) -> dict[str, Any]:
            try:
                return await self._client.get(
                    url, {"event_tickers": ",".join(chunk)}, ttl=600, no_store=no_store
                )
            except HTTPException:
                return {}

        pages = await asyncio.gather(*[fetch(chunk) for chunk in chunks])
        cards: dict[str, dict[str, dict[str, str]]] = {}
        meta: dict[str, dict[str, str]] = {}
        for page in pages:
            for card in page.get("cards") or []:
                event_ticker = card.get("event_ticker") or ""
                if not event_ticker:
                    continue
                meta[event_ticker] = {
                    "title": card.get("event_title") or "",
                    "subtitle": card.get("event_subtitle") or "",
                }
                markets = cards.setdefault(event_ticker, {})
                for market in card.get("markets") or []:
                    market_ticker = market.get("ticker")
                    if market_ticker:
                        markets[market_ticker] = self._card_entry(market)
        return cards, meta

    async def warm_card_images(self, event_tickers: list[str]) -> int:
        """Fetch and persist card images (both themes) and event titles for every
        event, paced so the ingest never spikes. Returns the number warmed."""
        if self._cache is None:
            return 0
        tickers = [t for t in dict.fromkeys(event_tickers) if t]
        blob: dict[str, dict[str, dict[str, str]]] = {}
        meta: dict[str, dict[str, str]] = {}
        step = BFF_CARDS_CHUNK * 8
        for start in range(0, len(tickers), step):
            batch = tickers[start : start + step]
            cards, batch_meta = await self._fetch_cards(batch, no_store=True)
            blob.update(cards)
            meta.update(batch_meta)
            await asyncio.sleep(self._settings.stats_page_pause)
        await self._cache.set(CARD_IMAGES_KEY, blob)
        await self._cache.set(EVENT_META_KEY, meta)
        return len(blob)

    async def event_meta(self) -> dict[str, dict[str, str]]:
        """`{event_ticker: {title, subtitle}}` warmed from the BFF cards API."""
        if self._cache is None:
            return {}
        return await self._cache.get(EVENT_META_KEY) or {}

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
        try:
            data = await self._client.get(
                f"/series/{series_ticker}/markets/{market_ticker}/candlesticks",
                _candlestick_params(days, period_interval),
                ttl=30,
            )
        except HTTPException as exc:
            if exc.status_code in (400, 404):
                return []
            raise
        candles = data.get("candlesticks")
        return candles if isinstance(candles, list) else []

    async def fetch_batch_candlesticks(
        self,
        market_tickers: list[str],
        days: int,
        period_interval: int,
    ) -> dict[str, list[dict[str, Any]]]:
        """Candlesticks for many market tickers, keyed by market ticker."""
        tickers = [ticker for ticker in dict.fromkeys(market_tickers) if ticker]
        if not tickers:
            return {}
        chunks = [
            tickers[i : i + BATCH_CANDLESTICK_MARKETS]
            for i in range(0, len(tickers), BATCH_CANDLESTICK_MARKETS)
        ]
        base_params = _candlestick_params(days, period_interval)

        async def fetch(chunk: list[str]) -> dict[str, list[dict[str, Any]]]:
            try:
                data = await self._client.get(
                    "/markets/candlesticks",
                    {**base_params, "market_tickers": ",".join(chunk)},
                    ttl=30,
                )
            except HTTPException as exc:
                if exc.status_code == 400 and len(chunk) > 1:
                    midpoint = max(1, len(chunk) // 2)
                    left = await fetch(chunk[:midpoint])
                    right = await fetch(chunk[midpoint:])
                    return {**left, **right}
                if exc.status_code in (400, 404):
                    return {}
                raise
            output: dict[str, list[dict[str, Any]]] = {}
            for requested_ticker, entry in zip(chunk, data.get("markets") or []):
                ticker = entry.get("market_ticker") or entry.get("ticker") or requested_ticker
                candles = entry.get("candlesticks")
                if ticker and isinstance(candles, list):
                    output[ticker] = candles
            return output

        pages = []
        for chunk in chunks:
            pages.append(await fetch(chunk))
        merged: dict[str, list[dict[str, Any]]] = {}
        for page in pages:
            merged.update(page)
        return merged

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
        *,
        include_intraday: bool = True,
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
        max_concurrent_requests = max(2, min(16, int(self._settings.rate_limit_per_sec * 2)))
        limiter = asyncio.Semaphore(max_concurrent_requests)
        cutoff = datetime.now(timezone.utc).timestamp() - 86400
        batch_hourly: dict[str, list[dict[str, Any]]] = {}
        if not include_intraday:
            batch_hourly = await self.fetch_batch_candlesticks(
                [m.get("ticker", "") for m in chosen],
                30,
                60,
            )

        async def candles(ticker: str, days: int, period_interval: int) -> list[dict[str, Any]]:
            async with limiter:
                try:
                    return await self.fetch_candlesticks(series_ticker, ticker, days, period_interval)
                except HTTPException:
                    return []

        async def history(market: dict[str, Any]) -> dict[str, Any]:
            ticker = market.get("ticker", "")
            if include_intraday:
                hourly_task = candles(ticker, 30, 60)
                hourly, minute = await asyncio.gather(hourly_task, candles(ticker, 1, 1))
                by_ts: dict[int, float] = {
                    ts: val for ts, val in _candle_points(hourly) if ts < cutoff
                }
                by_ts.update(dict(_candle_points(minute)))
            else:
                hourly = batch_hourly.get(ticker, [])
                by_ts = dict(_candle_points(hourly))
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
