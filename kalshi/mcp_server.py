"""MCP server exposing Kalshi market data as Copilot tools."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from kalshi.formatting import to_float
from kalshi.service import MarketDataService
from kalshi.stats import MarketStatsCache
from kalshi.taxonomy import TaxonomyCache
from kalshi.transforms import market_row, orderbook_rows, trade_row

mcp = FastMCP(
    "Kalshi Markets",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
mcp.settings.streamable_http_path = "/"

_ctx: dict[str, Any] = {}
_selection_subscribers: set[asyncio.Queue] = set()


def subscribe_selection() -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue()
    _selection_subscribers.add(queue)
    return queue


def unsubscribe_selection(queue: asyncio.Queue) -> None:
    _selection_subscribers.discard(queue)


def set_context(service: MarketDataService, stats: MarketStatsCache, taxonomy: TaxonomyCache) -> None:
    _ctx["service"] = service
    _ctx["stats"] = stats
    _ctx["taxonomy"] = taxonomy


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def set_selection(market_key: str) -> None:
    """Record the current market and push it to selection subscribers."""
    selection = (market_key or "").strip()
    if not selection or selection == _ctx.get("selection"):
        return
    _ctx["selection"] = selection
    for queue in list(_selection_subscribers):
        queue.put_nowait(selection)


def current_selection() -> str:
    return _ctx.get("selection", "")


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def select_market(market_key: str) -> str:
    """Set the market shown by the Market Rules widget."""
    set_selection(market_key)
    return json.dumps({"selected": current_selection()})


@mcp.tool()
async def browse_kalshi_markets(
    category: str = "All", tag: str = "All", search: str = "", limit: int = 20
) -> str:
    """Top active Kalshi events filtered by category, tag, and free-text search."""
    stats: MarketStatsCache = _ctx["stats"]
    events = (await stats.browse_events(category=category, tag=tag, search=search))[
        : _clamp(limit, 1, 100)
    ]
    rows = []
    for event in events:
        top = event["outcomes"][0] if event.get("outcomes") else {}
        rows.append({
            "title": event.get("title", ""),
            "category": event.get("category", ""),
            "tags": ", ".join(event.get("tags") or []),
            "event_ticker": event.get("event_ticker", ""),
            "leading_outcome": top.get("name", ""),
            "leading_pct": top.get("probability_pct"),
            "market_count": event.get("market_count", 0),
            "volume_24h": event.get("volume_24h", 0),
            "volume_total": event.get("volume_total", 0),
            "open_interest": event.get("open_interest", 0),
        })
    return json.dumps(rows, indent=2)


@mcp.tool()
async def kalshi_event_markets(event_ticker: str) -> str:
    """All outcome markets for a Kalshi event, with YES probability, bid/ask, volume, and open interest."""
    service: MarketDataService = _ctx["service"]
    resolved = await service.resolve_event(event_ticker=event_ticker)
    rows = [market_row(m, resolved["series_ticker"]) for m in resolved["markets"]]
    return json.dumps(rows, indent=2)


@mcp.tool()
async def list_kalshi_tags(limit: int = 40) -> str:
    """Active Kalshi tags ranked by 24h trading volume, with market counts."""
    stats: MarketStatsCache = _ctx["stats"]
    rows = await stats.by_group("tag")
    rows = sorted(rows, key=lambda r: r.get("volume_24h", 0), reverse=True)[: _clamp(limit, 1, 200)]
    return json.dumps([{"tag": r["category"], **{k: v for k, v in r.items() if k != "category"}} for r in rows], indent=2)


@mcp.tool()
async def kalshi_volume_by_category(
    group_by: str = "category", metric: str = "volume_24h", tag: str = "All"
) -> str:
    """Open-market activity aggregated by category or tag."""
    stats: MarketStatsCache = _ctx["stats"]
    metric = metric if metric in ("volume_24h", "volume_total", "open_interest") else "volume_24h"
    if tag and tag != "All":
        rows = await stats.by_event(tag=tag)
    else:
        rows = await stats.by_group("tag" if group_by == "tag" else "category")
    rows = sorted(rows, key=lambda r: r.get(metric, 0), reverse=True)
    return json.dumps([{"group": r["category"], **{k: v for k, v in r.items() if k != "category"}} for r in rows], indent=2)


@mcp.tool()
async def kalshi_series_catalog(category: str = "All", tag: str = "All", limit: int = 200) -> str:
    """Kalshi series filtered by category and tag, ranked by volume."""
    taxonomy: TaxonomyCache = _ctx["taxonomy"]
    series = await taxonomy.series(category=category, tag=tag)
    rows = [
        {
            "series_ticker": s.get("ticker", ""),
            "title": s.get("title", ""),
            "category": s.get("category", ""),
            "tags": ", ".join(t for t in (s.get("tags") or []) if isinstance(t, str)),
            "frequency": s.get("frequency", ""),
            "volume": to_float(s.get("volume_fp")),
        }
        for s in series[: _clamp(limit, 1, 500)]
    ]
    return json.dumps(rows, indent=2)


@mcp.tool()
async def kalshi_market_quote(market_key: str) -> str:
    """Current quote for one Kalshi market."""
    service: MarketDataService = _ctx["service"]
    resolved = await service.resolve_market(market_key)
    return json.dumps(market_row(resolved["market"], resolved["series_ticker"]), indent=2)


@mcp.tool()
async def kalshi_market_orderbook(market_key: str, depth: int = 10) -> str:
    """Live order book (YES/NO price levels with resting size) for a Kalshi market."""
    service: MarketDataService = _ctx["service"]
    resolved = await service.resolve_market(market_key)
    book = await service.fetch_orderbook(resolved["market_ticker"], _clamp(depth, 1, 100))
    return json.dumps(orderbook_rows(book), indent=2)


@mcp.tool()
async def kalshi_recent_trades(market_key: str, limit: int = 50) -> str:
    """Recent trades (price, size, side, timestamp) for a Kalshi market."""
    service: MarketDataService = _ctx["service"]
    resolved = await service.resolve_market(market_key)
    trades = await service.fetch_trades(resolved["market_ticker"], _clamp(limit, 1, 1000))
    return json.dumps([trade_row(t) for t in trades], indent=2)
