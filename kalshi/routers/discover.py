"""Discovery widgets: 24h volume by category and top markets."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from kalshi import charts
from kalshi.browse import render_browse
from kalshi.dependencies import get_service, get_stats, get_taxonomy, resolve_base_url
from kalshi.formatting import clamp_limit, parse_market_key
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
) -> list[dict[str, Any]]:
    """Toolbar param definitions for the browse iframe."""
    categories = [{"label": "All categories", "value": "All"}]
    categories += [{"label": c["category"], "value": c["category"]} for c in await taxonomy.categories()]
    tags = [{"label": "All tags", "value": "All"}]
    tags += [{"label": t["tag"], "value": t["tag"]} for t in await taxonomy.tags()]

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
        {"paramName": "search", "label": "Search", "type": "text", "value": "",
         "description": "Filter events by title, tag, or ticker"},
        {"paramName": "category", "label": "Category", "type": "text", "value": "All", "options": categories},
        {"paramName": "tag", "label": "Tag", "type": "text", "value": "All", "options": tags},
        {"paramName": "event_ticker", "label": "Event", "type": "text", "value": event_ticker,
         "description": "Selected event shared with the dashboard", "options": event_options},
        {"paramName": "market_key", "label": "Market", "type": "text", "value": market_key,
         "description": "Selected market shared with market widgets", "options": market_options},
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
    group_by: str,
    category: str,
    tag: str,
) -> list[dict[str, Any]]:
    selected_category = (category or "").strip()
    selected_tag = (tag or "").strip()
    category_value = selected_category if selected_category not in ("", "All") else "All"
    tag_value = selected_tag if selected_tag not in ("", "All") else "All"

    table_rows: list[dict[str, Any]] = []
    for row in rows:
        label = str(row.get("category") or "")
        if tag_value != "All":
            row_category = category_value
            row_tag = tag_value
        elif category_value != "All":
            row_category = category_value
            row_tag = label
        elif group_by == "tag":
            row_category = "All"
            row_tag = label
        else:
            row_category = label
            row_tag = "All"

        table_rows.append({
            **row,
            "label": label,
            "category": row_category,
            "tag": row_tag,
        })
    return table_rows


@router.get("/volume_by_category")
async def volume_by_category(
    group_by: str = Query("category"),
    metric: str = Query("volume_24h"),
    category: str = Query("All"),
    tag: str = Query("All"),
    close_within: str = Query(""),
    theme: str = Query("dark"),
    raw: bool = Query(False),
    stats: MarketStatsCache = Depends(get_stats),
) -> Any:
    metric = metric if metric in _METRIC_LABELS else "volume_24h"
    group_by = "tag" if group_by == "tag" else "category"
    days = _days(close_within)
    cat = category if (category or "").strip() not in ("", "All") else None
    selected = (tag or "").strip()
    tag_drill = selected not in ("", "All")

    if tag_drill:
        rows = await stats.by_event(category=cat, tag=selected, close_within_days=days)
        scope = f" in {category}" if cat else ""
        label = f"{_METRIC_LABELS[metric]} — events tagged {selected}{scope}"
        rows = sorted(rows, key=lambda r: r.get(metric, 0), reverse=True)[:25]
    elif cat:
        rows = await stats.by_group("tag", close_within_days=days, category=cat)
        label = f"{_METRIC_LABELS[metric]} — tags in {category}"
        rows = sorted(rows, key=lambda r: r.get(metric, 0), reverse=True)[:20]
    else:
        rows = await stats.by_group(group_by, close_within_days=days)
        label = _METRIC_LABELS[metric]
        rows = sorted(rows, key=lambda r: r.get(metric, 0), reverse=True)
        if group_by == "tag":
            rows = rows[:20]

    if raw:
        return _volume_table_rows(rows, group_by=group_by, category=category, tag=tag)
    if not rows:
        return JSONResponse(content=charts.empty_figure("No open markets in range", theme))
    return JSONResponse(content=charts.category_volume(rows, metric, label, theme))


@router.get("/browse_markets")
async def browse_markets(
    request: Request,
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
        "search": search, "category": category, "tag": tag, "sort": sort, "frequency": frequency,
        "close_within": close_within, "reverse": "true" if reverse else "", "theme": theme,
    }
    back_qs = urlencode({k: v for k, v in filters.items() if v})
    html = render_browse(
        events, rows=[_event_row(event) for event in events],
        param_defs=await _browse_param_defs(taxonomy, events, event_ticker, market_key),
        total=total, search=search, theme=theme, base_url=base_url, back_qs=back_qs,
        selected_event_ticker=event_ticker or parse_market_key(market_key)["event_ticker"],
        selected_market_key=market_key,
    )
    return HTMLResponse(content=html)
