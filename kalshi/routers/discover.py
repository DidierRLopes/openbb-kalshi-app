"""Discovery widgets: 24h volume by category and top markets."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from kalshi.browse import render_browse
from kalshi.dependencies import get_service, get_stats, get_taxonomy, resolve_base_url
from kalshi.formatting import clamp_limit, compose_selection, parse_market_key, parse_selection
from kalshi.service import MarketDataService
from kalshi.stats import MarketStatsCache
from kalshi.taxonomy import TaxonomyCache

router = APIRouter()

_METRIC_LABELS = {
    "volume_24h": "24h Volume (contracts)",
    "open_interest": "Open Interest (contracts)",
    "volume_total": "Total Volume (contracts)",
}

_VALID_SORTS = {
    "trending", "volume", "open_interest", "volatile", "new", "closing_soon", "fifty_fifty"
}


def _event_row(event: dict[str, Any]) -> dict[str, Any]:
    top = event["outcomes"][0] if event.get("outcomes") else {}
    return {
        "title": event.get("title", ""),
        "live": bool(event.get("live")),
        "category": event.get("category", ""),
        "tags": ", ".join(event.get("tags") or []),
        "series_ticker": event.get("series_ticker", ""),
        "event_ticker": event.get("event_ticker", ""),
        "market_count": event.get("market_count", 0),
        "leading_outcome": top.get("name", ""),
        "leading_pct": top.get("probability_pct"),
        "volume_24h": event.get("volume_24h", 0),
        "volume_total": event.get("volume_total", 0),
        "open_interest": event.get("open_interest", 0),
        "close_time": event.get("close_time", ""),
        "market_key": top.get("market_key", ""),
    }


def _short(value: Any, max_len: int = 88) -> str:
    text = str(value or "")
    return text if len(text) <= max_len else f"{text[: max_len - 3]}..."


async def _browse_param_defs(
    taxonomy: TaxonomyCache,
    events: list[dict[str, Any]],
    event_ticker: str,
    market_key: str,
    selection: str = "All",
) -> list[dict[str, Any]]:
    """Toolbar param definitions for the browse iframe."""
    sel = parse_selection(selection)
    selections = [{"label": "All categories", "value": "All"}]
    selections += [{"label": c["category"], "value": c["category"]} for c in await taxonomy.categories()]
    if sel["category"]:
        selections += [
            {
                "label": f"{sel['category']} › {t['tag']}",
                "value": compose_selection(sel["category"], t["tag"]),
            }
            for t in await taxonomy.tags(category=sel["category"])
        ]

    event_options = []
    market_options = []
    for event in events:
        ticker = str(event.get("event_ticker") or "")
        if not ticker:
            continue
        event_options.append({
            "label": f"{_short(event.get('title') or ticker)} ({ticker})",
            "value": ticker,
        })
        for outcome in event.get("outcomes") or []:
            outcome_market_key = str(outcome.get("market_key") or "")
            if not outcome_market_key:
                continue
            market_options.append({
                "label": f"{_short(outcome.get('name') or outcome_market_key, 64)} ({ticker})",
                "value": outcome_market_key,
            })

    if event_ticker and not any(option["value"] == event_ticker for option in event_options):
        event_options.insert(0, {"label": event_ticker, "value": event_ticker})

    return [
        {"paramName": "search", "label": "Search", "type": "text", "value": ""},
        {"paramName": "selection", "label": "Category / Topic", "type": "text", "value": selection or "All",
         "options": selections},
        {"paramName": "event_ticker", "label": "Event", "type": "text", "value": event_ticker,
         "options": event_options},
        {"paramName": "market_key", "label": "Market", "type": "text", "value": market_key,
         "options": market_options},
        {"paramName": "sort", "label": "Sort", "type": "text", "value": "trending", "options": [
            {"label": "Trending", "value": "trending"}, {"label": "Volatile", "value": "volatile"},
            {"label": "New", "value": "new"}, {"label": "Closing soon", "value": "closing_soon"},
            {"label": "Volume", "value": "volume"}, {"label": "Open Interest", "value": "open_interest"},
            {"label": "50-50", "value": "fifty_fifty"}]},
        {"paramName": "frequency", "label": "Frequency", "type": "text", "value": "all", "options": [
            {"label": "All", "value": "all"}, {"label": "Hourly", "value": "hourly"},
            {"label": "Daily", "value": "daily"}, {"label": "Weekly", "value": "weekly"},
            {"label": "Monthly", "value": "monthly"}, {"label": "Quarterly", "value": "quarterly"},
            {"label": "Annually", "value": "annual"}]},
        {"paramName": "close_within", "label": "Closes Within", "type": "text", "value": "", "options": [
            {"label": "Any time", "value": ""}, {"label": "24 hours", "value": "1"},
            {"label": "7 days", "value": "7"}, {"label": "30 days", "value": "30"},
            {"label": "90 days", "value": "90"}]},
        {"paramName": "reverse", "label": "Reverse sort", "type": "boolean", "value": "false"},
        {"paramName": "limit", "label": "Max events", "type": "number", "value": "40",
         "min": 1, "max": 150, "step": 10},
    ]


def _days(close_within: str) -> int | None:
    value = (close_within or "").strip()
    if not value or value in ("any", "all", "0"):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _volume_table_rows(
    rows: list[dict[str, Any]],
    *,
    category: str | None,
) -> list[dict[str, Any]]:
    """Attach the `Category > Topic` selection each row drills into."""
    table_rows: list[dict[str, Any]] = []
    for row in rows:
        label = str(row.get("category") or "")
        if category:
            # Rows are tags within the selected category.
            selection = compose_selection(category, label)
        else:
            selection = compose_selection(label)

        table_rows.append({**row, "label": label, "selection": selection})
    return table_rows


@router.get("/volume_by_category")
async def volume_by_category(
    metric: str = Query("volume_24h"),
    selection: str = Query("All"),
    close_within: str = Query(""),
    stats: MarketStatsCache = Depends(get_stats),
) -> Any:
    metric = metric if metric in _METRIC_LABELS else "volume_24h"
    days = _days(close_within)
    sel = parse_selection(selection)
    cat = sel["category"] or None

    if cat:
        rows = await stats.by_group("tag", close_within_days=days, category=cat)
        rows = sorted(rows, key=lambda r: r.get(metric, 0), reverse=True)[:20]
    else:
        rows = await stats.by_group("category", close_within_days=days)
        rows = sorted(rows, key=lambda r: r.get(metric, 0), reverse=True)

    return _volume_table_rows(rows, category=cat)


@router.get("/browse_markets")
async def browse_markets(
    request: Request,
    selection: str = Query(""),
    category: str = Query("All"),
    tag: str = Query("All"),
    search: str = Query("", max_length=120),
    sort: str = Query("trending"),
    frequency: str = Query("all"),
    close_within: str = Query(""),
    event_ticker: str = Query(""),
    market_key: str = Query(""),
    reverse: bool = Query(False),
    limit: int = Query(40, ge=1, le=150),
    theme: str = Query("dark"),
    raw: bool = Query(False),
    stats: MarketStatsCache = Depends(get_stats),
    service: MarketDataService = Depends(get_service),
    taxonomy: TaxonomyCache = Depends(get_taxonomy),
) -> Any:
    sort = sort if sort in _VALID_SORTS else "trending"
    sel = parse_selection(selection)
    if (selection or "").strip():
        category = sel["category"] or "All"
        tag = sel["tag"] or "All"

    selected_event = (event_ticker or "").strip() or parse_market_key(market_key)["event_ticker"]
    selected_market = market_key
    selected_market_event = parse_market_key(selected_market)["event_ticker"]
    if selected_event and selected_market_event and selected_market_event != selected_event:
        selected_market = ""

    events = await stats.browse_events(
        category=category,
        tag=tag,
        search=search,
        close_within_days=_days(close_within),
        frequency=frequency,
        sort=sort,
        reverse=reverse,
        limit=clamp_limit(limit, maximum=150),
    )
    if raw:
        return [_event_row(event) for event in events]
    images = await service.card_images(
        [e["event_ticker"] for e in events], light=theme == "light"
    )
    for event in events:
        for outcome in event["outcomes"]:
            ticker = parse_market_key(outcome["market_key"])["market_ticker"]
            info = images.get(ticker, {})
            outcome["image_url"] = info.get("image_url", "")
            outcome["color"] = info.get("color", "")

    total = len({m["event_ticker"] for m in await stats.markets()})
    base_url = resolve_base_url(request)
    filters = {
        "search": search, "selection": selection, "sort": sort, "frequency": frequency,
        "close_within": close_within, "reverse": "true" if reverse else "", "theme": theme,
    }
    back_qs = urlencode({k: v for k, v in filters.items() if v})
    html = render_browse(
        events, rows=[_event_row(event) for event in events],
        param_defs=await _browse_param_defs(taxonomy, events, selected_event, selected_market, selection),
        total=total, search=search, theme=theme, base_url=base_url, back_qs=back_qs,
        selected_event_ticker=selected_event,
        selected_market_key=selected_market,
        selection_prefix=f"{sel['category'] or 'All'} > {sel['tag'] or 'All'}",
        emit_on_load=False,
    )
    return HTMLResponse(content=html)
