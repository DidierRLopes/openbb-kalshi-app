"""Event discovery table, metric strip, brief, and markets."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from kalshi import charts
from kalshi.dependencies import get_service, get_stats, get_taxonomy, resolve_base_url
from kalshi.event_page import render_event_page
from kalshi.formatting import compact_number, parse_market_key, timestamp_to_iso
from kalshi.service import MarketDataService
from kalshi.stats import MarketStatsCache
from kalshi.taxonomy import ALL, TaxonomyCache
from kalshi.transforms import market_row

router = APIRouter()


async def effective_event_ticker(
    event_ticker: str = Query(""),
    category: str = Query(ALL),
    stats: MarketStatsCache = Depends(get_stats),
) -> str:
    """The selected event, or the most active one in scope."""
    ticker = (event_ticker or "").strip()
    return ticker or await stats.default_event_ticker(category=category)


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
            "label": "Category",
            "value": event.get("category", "Kalshi"),
            "subvalue": selected["event_ticker"],
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
    return sorted(
        rows,
        key=lambda row: (row["volume_24h"], row["volume_total"], row["last_price_pct"]),
        reverse=True,
    )


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


@router.get("/event_history_chart")
async def event_history_chart(
    event_ticker: str = Depends(effective_event_ticker),
    market_key: str = Query(""),
    theme: str = Query("dark"),
    raw: bool = Query(False),
    service: MarketDataService = Depends(get_service),
) -> Any:
    if not event_ticker:
        return [] if raw else JSONResponse(content=charts.empty_figure("No active events", theme))
    selected = await service.resolve_event(event_ticker=event_ticker)
    pinned = parse_market_key(market_key)["market_ticker"] if (market_key or "").strip() else ""
    histories = await service.outcome_histories(
        selected["series_ticker"], selected["markets"], pinned_ticker=pinned
    )
    lines = [
        {"name": h["name"], "points": h["points"]}
        for h in histories
        if h["points"]
    ]
    if raw:
        return [
            {"time": timestamp_to_iso(ts), "outcome": line["name"], "probability_pct": value}
            for line in lines
            for ts, value in line["points"]
        ]
    if not lines:
        return JSONResponse(content=charts.empty_figure("No price history available", theme))
    return JSONResponse(content=charts.outcome_history(lines, theme))
