"""Dropdown-options endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from kalshi.constants import LEGACY_TOP_HISTORY_MARKET_KEY, TOP_HISTORY_MARKET_COUNT
from kalshi.dependencies import get_service, get_stats, get_taxonomy
from kalshi.formatting import (
    build_market_key,
    compact_number,
    compose_selection,
    parse_market_key,
    parse_selection,
    to_float,
)
from kalshi.service import MarketDataService
from kalshi.stats import MarketStatsCache
from kalshi.taxonomy import ALL, TaxonomyCache

router = APIRouter()

Option = dict[str, Any]


def _truncate(text: str, length: int) -> str:
    text = text or ""
    return text if len(text) <= length else text[: length - 1] + "…"


async def _category_options(taxonomy: TaxonomyCache) -> list[Option]:
    options: list[Option] = [{"label": "All categories", "value": ALL}]
    for entry in await taxonomy.categories():
        options.append({"label": f"{entry['category']} ({entry['series_count']})", "value": entry["category"]})
    return options


async def _selection_options(taxonomy: TaxonomyCache, selection: str) -> list[Option]:
    """`Category > Topic` drill options; topics are listed for the active category."""
    options: list[Option] = [{"label": "All categories", "value": ALL}]
    current = parse_selection(selection)
    for entry in await taxonomy.categories():
        options.append({"label": f"{entry['category']} ({entry['series_count']})", "value": entry["category"]})
    if current["category"]:
        for entry in await taxonomy.tags(category=current["category"]):
            options.append({
                "label": f"{current['category']} › {entry['tag']} ({entry['series_count']})",
                "value": compose_selection(current["category"], entry["tag"]),
            })
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
    sort: str = "",
) -> list[Option]:
    options: list[Option] = [{"label": "All outcomes", "value": "All"}] if include_all else []
    ticker = (event_ticker or "").strip()
    selected_market_key = (market_key or "").split(",", 1)[0].strip()
    if not ticker and selected_market_key not in ("", "All", LEGACY_TOP_HISTORY_MARKET_KEY):
        ticker = parse_market_key(selected_market_key)["event_ticker"]
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

    probability_mode = include_top or sort == "probability"
    sort_key = probability_sort if probability_mode else volume_sort
    markets = sorted(resolved["markets"], key=sort_key, reverse=True)
    # Only history-style selectors ask for default outcomes. Ordinary market
    # selectors should update their option list without choosing a market.
    selected_limit = TOP_HISTORY_MARKET_COUNT if probability_mode else 0
    seen: set[str] = set()
    selected_count = 0
    for market in markets[:200]:
        market_ticker = market.get("ticker", "")
        if not market_ticker or market_ticker in seen:
            continue
        seen.add(market_ticker)
        label_text = market.get("yes_sub_title") or market.get("title") or market_ticker
        option: Option = {
            "label": f"{_truncate(label_text, 80)} ({market_ticker})",
            "value": build_market_key(series_ticker, market_ticker, market.get("event_ticker", "")),
        }
        if selected_count < selected_limit:
            option["selected"] = True
            selected_count += 1
        options.append(option)
    return options


@router.get("/options")
@router.get("/options/{group_id}")
async def options(
    group_id: str = "",
    field: str = Query(""),
    category: str = Query(ALL),
    tag: str = Query(ALL),
    selection: str = Query(""),
    series_ticker: str = Query(""),
    event_ticker: str = Query(""),
    market_key: str = Query(""),
    include_all: bool = Query(False),
    include_top: bool = Query(False),
    sort: str = Query(""),
    stats: MarketStatsCache = Depends(get_stats),
    taxonomy: TaxonomyCache = Depends(get_taxonomy),
    service: MarketDataService = Depends(get_service),
) -> list[Option]:
    """Return dropdown options for the requested `field`.

    The `/options/{group_id}` form serves the same data; the path segment exists
    because the workspace derives a widget param's `groupById` from the last
    segment of its options endpoint, so params that must share a group across
    widgets need a stable, distinct URL (e.g. `/options/selection-options`).
    """
    field = field or group_id.removesuffix("-options") or "category"
    if (selection or "").strip():
        sel = parse_selection(selection)
        category = sel["category"] or ALL
        tag = sel["tag"] or ALL
    if field == "selection":
        return await _selection_options(taxonomy, selection)
    if field == "tag":
        return await _tag_options(taxonomy, category)
    if field == "series_ticker":
        return await _series_options(taxonomy, category, tag)
    if field == "event_ticker":
        return await _event_options(stats, category, tag, series_ticker)
    if field == "market_key":
        return await _market_options(service, event_ticker, market_key, include_all, include_top, sort)
    return await _category_options(taxonomy)
