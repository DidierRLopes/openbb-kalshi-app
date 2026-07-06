"""Event discovery table, metric strip, brief, and markets."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from kalshi import charts
from kalshi.constants import LEGACY_TOP_HISTORY_MARKET_KEY, TOP_HISTORY_MARKET_COUNT
from kalshi.dependencies import get_service, get_stats, get_taxonomy, resolve_base_url
from kalshi.event_page import render_event_page
from kalshi.formatting import compact_number, parse_market_key, parse_selection, timestamp_to_iso, to_float
from kalshi.service import MarketDataService
from kalshi.stats import MarketStatsCache
from kalshi.taxonomy import ALL, TaxonomyCache
from kalshi.transforms import market_row

router = APIRouter()

_HISTORY_FIELD_RE = re.compile(r"[^0-9A-Za-z]+")

_EVENT_MARKETS_FIELDS = (
    "subtitle",
    "last_price_pct",
    "yes_bid_pct",
    "yes_ask_pct",
    "spread_points",
    "volume_total",
    "open_interest",
    "ticker",
    "close_time",
    "market_key",
)


async def effective_event_ticker(
    event_ticker: str = Query(""),
    category: str = Query(ALL),
    tag: str = Query(ALL),
    selection: str = Query(""),
    stats: MarketStatsCache = Depends(get_stats),
) -> str:
    """The selected event, or the most active one in the selected category/topic."""
    ticker = (event_ticker or "").strip()
    if ticker:
        return ticker
    if (selection or "").strip():
        sel = parse_selection(selection)
        category = sel["category"] or ALL
        tag = sel["tag"] or ALL
    return await stats.default_event_ticker(category=category, tag=tag)


@router.get("/event_metrics")
async def event_metrics(
    event_ticker: str = Depends(effective_event_ticker),
    service: MarketDataService = Depends(get_service),
) -> list[dict[str, str]]:
    if not event_ticker:
        return [{"label": "No active events", "value": "—", "subvalue": "Try another category"}]
    selected = await service.resolve_event(event_ticker=event_ticker)
    event = selected["event"]
    markets = [market_row(m, selected["series_ticker"]) for m in selected["markets"]]
    total_volume = sum(row["volume_total"] for row in markets)
    total_open_interest = sum(row["open_interest"] for row in markets)
    active_markets = [row for row in markets if row["status"] in {"active", "open"}]
    top_market = max(markets, key=lambda row: row["last_price_pct"], default=None)

    return [
        {
            "label": "Question",
            "value": event.get("title") or selected["event_ticker"],
            "subvalue": f"{event.get('category', 'Kalshi')} · {selected['event_ticker']}",
        },
        {
            "label": "Markets",
            "value": str(len(markets)),
            "subvalue": f"{len(active_markets)} active",
        },
        {
            "label": "Event Volume",
            "value": compact_number(total_volume),
            "subvalue": "Contracts across outcomes",
        },
        {
            "label": "Open Interest",
            "value": compact_number(total_open_interest),
            "subvalue": "Across event markets",
        },
        {
            "label": "Top Outcome",
            "value": f"{top_market['last_price_pct']:.1f}%" if top_market else "N/A",
            "subvalue": top_market["subtitle"][:48] if top_market else "No markets returned",
        },
    ]


@router.get("/event_markets")
async def event_markets(
    event_ticker: str = Depends(effective_event_ticker),
    service: MarketDataService = Depends(get_service),
) -> list[dict[str, Any]]:
    if not event_ticker:
        return []
    selected = await service.resolve_event(event_ticker=event_ticker)
    rows = [market_row(m, selected["series_ticker"]) for m in selected["markets"]]
    sorted_rows = sorted(
        rows,
        key=lambda row: (row["volume_24h"], row["volume_total"], row["last_price_pct"]),
        reverse=True,
    )
    return [
        {field: row.get(field) for field in _EVENT_MARKETS_FIELDS}
        for row in sorted_rows
    ]


@router.get("/event_details")
async def event_details(
    request: Request,
    event_ticker: str = Query(""),
    market_key: str = Query(""),
    theme: str = Query("dark"),
    back: str = Query(""),
    service: MarketDataService = Depends(get_service),
    taxonomy: TaxonomyCache = Depends(get_taxonomy),
) -> HTMLResponse:
    ticker = (event_ticker or "").strip() or parse_market_key(market_key)["event_ticker"]
    selected = await service.resolve_event(event_ticker=ticker or None)
    series = await taxonomy.get_series(selected["series_ticker"]) or {}
    images = await service.card_images([selected["event_ticker"]], light=theme == "light")
    history_figure, has_feed = await _event_figure(service, selected, images, theme)
    base_url = resolve_base_url(request)
    back_url = f"{base_url}/browse_markets?{back}" if back else f"{base_url}/browse_markets?theme={theme}"
    poll_url = (
        f"/event_chart?event_ticker={quote(selected['event_ticker'])}&theme={quote(theme)}"
        if has_feed else ""
    )
    doc_base = f"{base_url}/market_document?event_ticker={quote(selected['event_ticker'])}"
    html = render_event_page(
        event=selected["event"],
        markets=selected["markets"],
        series=series,
        images=images,
        event_ticker=selected["event_ticker"],
        series_ticker=selected["series_ticker"],
        theme=theme,
        back_url=back_url,
        history_figure=history_figure,
        poll_url=poll_url,
        doc_base=doc_base,
    )
    return HTMLResponse(content=html)


async def _event_figure(
    service: MarketDataService, selected: dict[str, Any], images: dict[str, Any], theme: str
) -> tuple[dict[str, Any] | None, bool]:
    """The event price-history figure and whether it is a real-time feed."""
    details = await service.fetch_live_data(selected["event_ticker"])
    points = details.get("timeseries") if isinstance(details, dict) else None
    if points:
        current = points[-1].get("v")
        priced = []
        for market in selected["markets"]:
            strike = market.get("floor_strike")
            if strike is None:
                strike = market.get("cap_strike")
            if strike is not None:
                priced.append((market, strike))
        thresholds = []
        if priced and current is not None:
            market, strike = min(priced, key=lambda ms: abs(ms[1] - current))
            color = images.get(market.get("ticker", ""), {}).get("color") or "#21c891"
            thresholds = [{"value": strike, "label": market.get("yes_sub_title") or f"above {strike:g}", "color": color}]
        return charts.live_asset_price(points, thresholds, details.get("asset", ""), theme), True
    histories = await service.outcome_histories(selected["series_ticker"], selected["markets"])
    lines = [
        {"name": h["name"], "color": images.get(h["ticker"], {}).get("color"), "points": h["points"]}
        for h in histories
        if h["points"]
    ]
    return (charts.outcome_history(lines, theme) if lines else None), False


@router.get("/event_chart")
async def event_chart(
    event_ticker: str = Query(""),
    theme: str = Query("dark"),
    service: MarketDataService = Depends(get_service),
) -> Any:
    ticker = (event_ticker or "").strip()
    if not ticker:
        return JSONResponse(content=charts.empty_figure("No event selected", theme))
    selected = await service.resolve_event(event_ticker=ticker)
    images = await service.card_images([selected["event_ticker"]], light=theme == "light")
    figure, _ = await _event_figure(service, selected, images, theme)
    return JSONResponse(content=figure or charts.empty_figure("No price history available", theme))


def _history_market_selection(history_market_key: Any) -> tuple[str, set[str]]:
    raw_values = history_market_key if isinstance(history_market_key, list) else [history_market_key]
    values = [
        part.strip()
        for value in raw_values
        for part in str(value or "").split(",")
        if part and part.strip() and part.strip() != LEGACY_TOP_HISTORY_MARKET_KEY
    ]
    if any(value == ALL for value in values):
        return "all", set()

    tickers = {
        parsed["market_ticker"]
        for parsed in (parse_market_key(value) for value in values)
        if parsed["market_ticker"]
    }
    if tickers:
        return "selected", tickers

    return "top", set()


def _market_probability(market: dict[str, Any]) -> float:
    return to_float(market.get("last_price_dollars")) or to_float(
        market.get("yes_bid_dollars")
    )


def _top_markets_plus_selected(
    markets: list[dict[str, Any]],
    selected_market_tickers: set[str],
) -> list[dict[str, Any]]:
    top_markets = sorted(markets, key=_market_probability, reverse=True)[
        :TOP_HISTORY_MARKET_COUNT
    ]
    seen = {market.get("ticker") for market in top_markets}
    selected_markets = [
        market
        for market in markets
        if market.get("ticker") in selected_market_tickers
        and market.get("ticker") not in seen
    ]
    return [*top_markets, *selected_markets]


def _history_outcome_field(name: Any, ticker: Any, used: set[str]) -> str:
    label = str(name or ticker or "outcome").strip()
    base = _HISTORY_FIELD_RE.sub("_", label.lower()).strip("_") or "outcome"
    if base[0].isdigit():
        base = f"outcome_{base}"

    field = base
    counter = 2
    ticker_slug = _HISTORY_FIELD_RE.sub("_", str(ticker or "").lower()).strip("_")
    while field in used:
        if ticker_slug:
            candidate = f"{base}_{ticker_slug}"
            if candidate not in used:
                field = candidate
                break
        field = f"{base}_{counter}"
        counter += 1
    used.add(field)
    return field


@router.get("/event_history_chart")
async def event_history_chart(
    event_ticker: str = Depends(effective_event_ticker),
    history_market_key: list[str] | None = Query(None),
    theme: str = Query("dark"),
    raw: bool = Query(False),
    service: MarketDataService = Depends(get_service),
) -> Any:
    if not event_ticker:
        return []
    selected = await service.resolve_event(event_ticker=event_ticker)
    history_mode, selected_market_tickers = _history_market_selection(history_market_key)
    history_markets = selected["markets"]
    if history_mode == "selected":
        history_markets = _top_markets_plus_selected(
            selected["markets"],
            selected_market_tickers,
        )
        selected_matches = [
            market
            for market in selected["markets"]
            if market.get("ticker") in selected_market_tickers
        ]
        if not selected_matches:
            history_mode = "top"
            history_markets = selected["markets"]
    top_n = (
        len(history_markets)
        if history_mode in {"all", "selected"}
        else TOP_HISTORY_MARKET_COUNT
    )
    histories = await service.outcome_histories(
        selected["series_ticker"],
        history_markets,
        top_n=top_n,
        include_intraday=history_mode in {"selected", "top"} or len(history_markets) <= 12,
    )
    lines = [
        {"name": h["name"], "ticker": h["ticker"], "points": h["points"]}
        for h in histories
        if h["points"]
    ]
    used_fields: set[str] = set()
    outcome_fields = [
        _history_outcome_field(line["name"], line["ticker"], used_fields)
        for line in lines
    ]
    rows_by_time: dict[str, dict[str, Any]] = {}
    for line, field in zip(lines, outcome_fields, strict=True):
        for ts, value in line["points"]:
            time = timestamp_to_iso(ts)
            if not time:
                continue
            row = rows_by_time.setdefault(time, {"time": time})
            row[field] = value

    rows: list[dict[str, Any]] = []
    for time in sorted(rows_by_time):
        row = rows_by_time[time]
        for field in outcome_fields:
            row.setdefault(field, None)
        rows.append(row)
    return rows
