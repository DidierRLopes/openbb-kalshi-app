"""Market-level widgets: rules & documents, order-book ladder, and trade tape."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from kalshi.dependencies import get_service, get_stats, get_taxonomy, resolve_base_url
from kalshi.formatting import build_market_key, parse_market_key, pct, to_float
from kalshi.ladder import render_ladder
from kalshi.marketrules import render_market_rules
from kalshi.mcp_server import current_selection, set_selection, subscribe_selection, unsubscribe_selection
from kalshi.service import MarketDataService
from kalshi.stats import MarketStatsCache
from kalshi.taxonomy import TaxonomyCache
from kalshi.transforms import orderbook_rows, trade_row

router = APIRouter()

_NO_MARKET = "No active markets"
_NO_MARKET_HINT = "Try another category, or select an event."


def _event_ok(market_key: str, event_ticker: str) -> bool:
    """True if market_key belongs to the given event."""
    if not event_ticker:
        return True
    mk_event = parse_market_key(market_key).get("event_ticker", "")
    return not mk_event or mk_event == event_ticker


async def effective_market_key(
    market_key: str = Query(""),
    event_ticker: str = Query(""),
    category: str = Query("All"),
    service: MarketDataService = Depends(get_service),
    stats: MarketStatsCache = Depends(get_stats),
) -> str:
    """Selected market, else the top market of the event in scope."""
    et = (event_ticker or "").strip()
    mk = (market_key or "").strip()
    if mk and _event_ok(mk, et):
        return mk
    selected = current_selection()
    if selected and _event_ok(selected, et):
        return selected
    et = et or await stats.default_event_ticker(category=category)
    if not et:
        return ""
    try:
        resolved = await service.resolve_event(event_ticker=et)
    except HTTPException:
        return ""
    markets = sorted(
        resolved["markets"], key=lambda m: to_float(m.get("volume_fp")), reverse=True
    )
    if not markets:
        return ""
    return build_market_key(
        resolved["series_ticker"], markets[0].get("ticker", ""), resolved["event_ticker"]
    )


def _has(market_key: str) -> bool:
    return bool((market_key or "").strip())


def _prompt_html(theme: str) -> str:
    color = "#667085" if theme == "light" else "#9a9aa4"
    bg = "#ffffff" if theme == "light" else "#0f0f12"
    return (
        f'<html><body style="margin:0;height:100vh;display:flex;align-items:center;'
        f'justify-content:center;background:{bg};color:{color};'
        f'font-family:-apple-system,Segoe UI,sans-serif;font-size:13px;text-align:center;padding:24px">'
        f"<div><strong>{_NO_MARKET}</strong><br/>{_NO_MARKET_HINT}</div></body></html>"
    )


@router.get("/market_brief")
async def market_brief(
    request: Request,
    market_key: str = Depends(effective_market_key),
    theme: str = Query("dark"),
    service: MarketDataService = Depends(get_service),
    taxonomy: TaxonomyCache = Depends(get_taxonomy),
) -> HTMLResponse:
    if not _has(market_key):
        return HTMLResponse(content=_prompt_html(theme))
    selected = await service.resolve_market(market_key)
    series = await taxonomy.get_series(selected["series_ticker"]) or {}
    base_url = resolve_base_url(request)
    doc_base = f"{base_url}/market_document?market_key={quote(selected['market_key'], safe='')}"
    html = render_market_rules(
        market=selected["market"],
        event=selected["event"],
        series=series,
        market_ticker=selected["market_ticker"],
        series_ticker=selected["series_ticker"],
        theme=theme,
        doc_base=doc_base,
        param_defs=[],
        sync_url=f"{base_url}/selection_stream",
        current_market=selected["market_key"],
    )
    return HTMLResponse(content=html)


@router.get("/market_document")
async def market_document(
    market_key: str = Depends(effective_market_key),
    doc: str = Query("rules"),
    service: MarketDataService = Depends(get_service),
) -> Response:
    doc = "terms" if (doc or "").lower() == "terms" else "rules"
    content, content_type = await service.fetch_document(market_key, doc)
    return Response(
        content=content,
        media_type=content_type or "application/pdf",
        headers={"Content-Disposition": "inline"},
    )


@router.get("/selection_stream")
async def selection_stream() -> StreamingResponse:
    """Server-sent events of the selected market."""

    async def events():
        queue = subscribe_selection()
        try:
            yield f"data: {current_selection()}\n\n"
            while True:
                market_key = await queue.get()
                yield f"data: {market_key}\n\n"
        finally:
            unsubscribe_selection(queue)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _book_levels(raw_levels: Any) -> list[tuple[float, float]]:
    """Convert raw [price_dollars, size] pairs to (price_pct, contracts)."""
    out: list[tuple[float, float]] = []
    for lvl in raw_levels or []:
        if isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
            out.append((to_float(lvl[0]) * 100, to_float(lvl[1])))
    return out


@router.get("/orderbook_ladder")
async def orderbook_ladder(
    market_key: str = Depends(effective_market_key),
    depth: int = Query(10, ge=1, le=100),
    side: str = Query("yes"),
    raw: bool = Query(False),
    theme: str = Query("dark"),
    service: MarketDataService = Depends(get_service),
) -> Any:
    if not _has(market_key):
        return [] if raw else HTMLResponse(content=_prompt_html(theme))
    set_selection(market_key)
    selected = await service.resolve_market(market_key)
    orderbook = await service.fetch_orderbook(selected["market_ticker"], depth)
    if raw:
        return orderbook_rows(orderbook)

    market = selected["market"]
    side = "no" if (side or "").lower() == "no" else "yes"
    yes_bids = _book_levels(orderbook.get("yes_dollars"))
    no_bids = _book_levels(orderbook.get("no_dollars"))
    last_yes = pct(market.get("last_price_dollars"))
    if side == "no":
        bids = sorted(no_bids, key=lambda lvl: lvl[0], reverse=True)
        asks = sorted(((100 - p, s) for p, s in yes_bids), key=lambda lvl: lvl[0])
        last = round(100 - last_yes, 1) if last_yes else None
    else:
        bids = sorted(yes_bids, key=lambda lvl: lvl[0], reverse=True)
        asks = sorted(((100 - p, s) for p, s in no_bids), key=lambda lvl: lvl[0])
        last = last_yes
    html = render_ladder(
        title=market.get("title") or selected["market_ticker"],
        subtitle=market.get("yes_sub_title") or market.get("subtitle") or "",
        market_ticker=selected["market_ticker"],
        asks=asks,
        bids=bids,
        last_price=last,
        side=side,
        theme=theme,
    )
    return HTMLResponse(content=html)


@router.get("/selected_trades")
async def selected_trades(
    market_key: str = Depends(effective_market_key),
    limit: int = Query(100, ge=1, le=1000),
    service: MarketDataService = Depends(get_service),
) -> list[dict[str, Any]]:
    if not _has(market_key):
        return []
    set_selection(market_key)
    selected = await service.resolve_market(market_key)
    trades = await service.fetch_trades(selected["market_ticker"], limit=limit)
    return [trade_row(trade) for trade in trades]
