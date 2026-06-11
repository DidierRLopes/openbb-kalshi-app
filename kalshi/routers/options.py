"""Dropdown-options endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from kalshi.constants import TOP_HISTORY_MARKET_LABEL, TOP_HISTORY_MARKET_KEY
from kalshi.dependencies import get_service, get_stats, get_taxonomy
from kalshi.formatting import build_market_key, compact_number, parse_market_key, to_float
from kalshi.service import MarketDataService
from kalshi.stats import MarketStatsCache
from kalshi.taxonomy import ALL, TaxonomyCache

router = APIRouter()

Option = dict[str, str]


def _truncate(text: str, length: int) -> str:
    text = text or ""
    return text if len(text) <= length else text[: length - 1] + "…"


async def _category_options(taxonomy: TaxonomyCache) -> list[Option]:
    options: list[Option] = [{"label": "All categories", "value": ALL}]
    for entry in await taxonomy.categories():
        options.append({"label": f"{entry['category']} ({entry['series_count']})", "value": entry["category"]})
    return options


async def _tag_options(taxonomy: TaxonomyCache, category: str) -> list[Option]:
    options: list[Option] = [{"label": "All tags", "value": ALL}]
    for entry in await taxonomy.tags(category=category):
        options.append({"label": f"{entry['tag']} ({entry['series_count']})", "value": entry["tag"]})
    return options


async def _series_options(taxonomy: TaxonomyCache, category: str, tag: str) -> list[Option]:
    options: list[Option] = []
    for series in (await taxonomy.series(category=category, tag=tag))[:200]:
        ticker = series.get("ticker", "")
        title = _truncate(series.get("title") or ticker, 70)
        options.append({"label": f"{title} ({ticker}) · {compact_number(series.get('volume_fp'))} vol", "value": ticker})
    return options


async def _event_options(stats: MarketStatsCache, category: str, tag: str, series_ticker: str) -> list[Option]:
    events = await stats.discover_events(category=category, tag=tag, series_ticker=series_ticker, limit=250)
    options: list[Option] = []
    for event in events:
        ticker = event.get("event_ticker", "")
        if not ticker:
            continue
        title = _truncate(event.get("title") or ticker, 70)
        leading = event.get("leading_outcome")
        label = f"{title} · {leading} {event.get('leading_pct', 0):.0f}%" if leading else title
        options.append({"label": _truncate(label, 100), "value": ticker})
    return options


async def _market_options(
    service: MarketDataService,
    event_ticker: str,
    market_key: str,
    include_all: bool = False,
    include_top: bool = False,
) -> list[Option]:
    options: list[Option] = [{"label": "All outcomes", "value": "All"}] if include_all else []
    if include_top:
        options.append({"label": TOP_HISTORY_MARKET_LABEL, "value": TOP_HISTORY_MARKET_KEY})
    ticker = (event_ticker or "").strip()
    if not ticker and market_key and market_key not in ("All", TOP_HISTORY_MARKET_KEY):
        ticker = parse_market_key(market_key)["event_ticker"]
    if not ticker:
        return options
    resolved = await service.resolve_event(event_ticker=ticker)
    series_ticker = resolved["series_ticker"]

    def probability_sort(market: dict[str, object]) -> float:
        return to_float(market.get("last_price_dollars")) or to_float(
            market.get("yes_bid_dollars")
        )

    def volume_sort(market: dict[str, object]) -> float:
        return to_float(market.get("volume_fp"))

    sort_key = probability_sort if include_top else volume_sort
    markets = sorted(resolved["markets"], key=sort_key, reverse=True)
    seen: set[str] = set()
    for market in markets[:200]:
        market_ticker = market.get("ticker", "")
        if not market_ticker or market_ticker in seen:
            continue
        seen.add(market_ticker)
        label_text = market.get("yes_sub_title") or market.get("title") or market_ticker
        options.append({
            "label": f"{_truncate(label_text, 80)} ({market_ticker})",
            "value": build_market_key(series_ticker, market_ticker, market.get("event_ticker", "")),
        })
    return options


@router.get("/options")
async def options(
    field: str = Query("category"),
    category: str = Query(ALL),
    tag: str = Query(ALL),
    series_ticker: str = Query(""),
    event_ticker: str = Query(""),
    market_key: str = Query(""),
    include_all: bool = Query(False),
    include_top: bool = Query(False),
    stats: MarketStatsCache = Depends(get_stats),
    taxonomy: TaxonomyCache = Depends(get_taxonomy),
    service: MarketDataService = Depends(get_service),
) -> list[Option]:
    """Return dropdown options for the requested `field`."""
    if field == "tag":
        return await _tag_options(taxonomy, category)
    if field == "series_ticker":
        return await _series_options(taxonomy, category, tag)
    if field == "event_ticker":
        return await _event_options(stats, category, tag, series_ticker)
    if field == "market_key":
        return await _market_options(service, event_ticker, market_key, include_all, include_top)
    return await _category_options(taxonomy)
