"""
Kalshi Market Dashboard - OpenBB Workspace backend.

Public market-data integration for Kalshi prediction markets.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any

import httpx
import plotly.graph_objects as go
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from plotly.subplots import make_subplots


ROOT_PATH = Path(__file__).parent.resolve()
KALSHI_API_BASE_URL = os.getenv(
    "KALSHI_API_BASE_URL",
    "https://external-api.kalshi.com/trade-api/v2",
).rstrip("/")
DEFAULT_MARKET_KEY = os.getenv(
    "KALSHI_DEFAULT_MARKET_KEY",
    "KXELONMARS|KXELONMARS-99|KXELONMARS-99",
)
DEFAULT_EVENT_TICKER = os.getenv("KALSHI_DEFAULT_EVENT_TICKER", "KXELONMARS-99")
CACHE_TTL_SECONDS = int(os.getenv("KALSHI_CACHE_TTL_SECONDS", "30"))

_CACHE: dict[str, tuple[float, Any]] = {}
EVENT_SEARCH_FIELDS = (
    "title",
    "subtitle",
    "category",
    "event_ticker",
    "series_ticker",
)


app = FastAPI(
    title="Kalshi Market Dashboard",
    description="OpenBB Workspace app for public Kalshi prediction-market data.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://pro.openbb.co",
        "https://pro.openbb.dev",
        "https://excel.openbb.co",
        "https://excel.openbb.dev",
        "http://localhost:1420",
        "http://localhost:5050",
        "tauri://localhost",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def read_json_file(name: str) -> Any:
    with (ROOT_PATH / name).open("r", encoding="utf-8") as file:
        return json.load(file)


def cache_key(path: str, params: dict[str, Any] | None) -> str:
    clean_params = {
        key: value
        for key, value in (params or {}).items()
        if value is not None and value != ""
    }
    return json.dumps([path, clean_params], sort_keys=True)


async def kalshi_get(
    path: str,
    params: dict[str, Any] | None = None,
    ttl: int = CACHE_TTL_SECONDS,
) -> dict[str, Any]:
    key = cache_key(path, params)
    cached = _CACHE.get(key)
    now = time.time()
    if cached and now - cached[0] < ttl:
        return cached[1]

    clean_params = {
        param_key: value
        for param_key, value in (params or {}).items()
        if value is not None and value != ""
    }
    url = f"{KALSHI_API_BASE_URL}{path}"

    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": "OpenBB-Kalshi-Market-Dashboard/1.0"},
            timeout=20.0,
        ) as client:
            response = await client.get(url, params=clean_params)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:300]
        status_code = exc.response.status_code
        if status_code >= 500:
            status_code = 502
        raise HTTPException(
            status_code=status_code,
            detail=f"Kalshi API error for {path}: {detail}",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Kalshi API request failed for {path}: {exc}",
        ) from exc

    data = response.json()
    _CACHE[key] = (now, data)
    return data


def to_float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def pct(value: Any) -> float:
    return round(to_float(value) * 100, 2)


def money(value: Any) -> float:
    return round(to_float(value), 4)


def quantity(value: Any) -> float:
    return round(to_float(value), 2)


def compact_number(value: Any) -> str:
    number = to_float(value)
    abs_number = abs(number)
    if abs_number >= 1_000_000:
        return f"{number / 1_000_000:.2f}M"
    if abs_number >= 1_000:
        return f"{number / 1_000:.1f}K"
    return f"{number:.0f}"


def iso_to_display(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    return value.replace("T", " ").replace("Z", " UTC")[:19]


def timestamp_to_iso(timestamp: Any) -> str:
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).isoformat()


def parse_iso_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def clamp_limit(limit: int, minimum: int = 1, maximum: int = 200) -> int:
    return max(minimum, min(maximum, int(limit)))


def build_market_key(series_ticker: str, market_ticker: str, event_ticker: str) -> str:
    return f"{series_ticker or ''}|{market_ticker}|{event_ticker or ''}"


def parse_market_key(market_key: str | None) -> dict[str, str]:
    key = market_key or DEFAULT_MARKET_KEY
    parts = key.split("|")
    if len(parts) == 3:
        series_ticker, market_ticker, event_ticker = parts
    elif len(parts) == 2:
        series_ticker, market_ticker = parts
        event_ticker = ""
    else:
        series_ticker = ""
        market_ticker = key
        event_ticker = ""
    return {
        "series_ticker": series_ticker,
        "market_ticker": market_ticker,
        "event_ticker": event_ticker,
    }


async def resolve_market(market_key: str | None) -> dict[str, Any]:
    parsed = parse_market_key(market_key)
    market_ticker = parsed["market_ticker"]
    if not market_ticker:
        parsed = parse_market_key(DEFAULT_MARKET_KEY)
        market_ticker = parsed["market_ticker"]

    market_response = await kalshi_get(f"/markets/{market_ticker}", ttl=20)
    market = market_response.get("market", {})
    event_ticker = parsed["event_ticker"] or market.get("event_ticker", "")
    series_ticker = parsed["series_ticker"]
    event: dict[str, Any] = {}

    if event_ticker:
        event_response = await kalshi_get(f"/events/{event_ticker}", ttl=120)
        event = event_response.get("event", {})
        series_ticker = series_ticker or event.get("series_ticker", "")

    if not series_ticker:
        raise HTTPException(
            status_code=404,
            detail=f"Could not resolve series ticker for market {market_ticker}",
        )

    return {
        "series_ticker": series_ticker,
        "market_ticker": market_ticker,
        "event_ticker": event_ticker,
        "market": market,
        "event": event,
        "market_key": build_market_key(series_ticker, market_ticker, event_ticker),
    }


async def resolve_event(event_ticker: str | None) -> dict[str, Any]:
    ticker = event_ticker or DEFAULT_EVENT_TICKER
    data = await kalshi_get(f"/events/{ticker}", ttl=60)
    event = data.get("event", {})
    markets = data.get("markets", [])
    if not event:
        raise HTTPException(status_code=404, detail=f"Event not found: {ticker}")
    return {
        "event_ticker": ticker,
        "event": event,
        "markets": markets if isinstance(markets, list) else [],
        "series_ticker": event.get("series_ticker", ""),
    }


def event_row(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_ticker": event.get("event_ticker", ""),
        "series_ticker": event.get("series_ticker", ""),
        "category": event.get("category", ""),
        "title": event.get("title", ""),
        "subtitle": event.get("sub_title", ""),
        "mutually_exclusive": bool(event.get("mutually_exclusive")),
        "available_on_brokers": bool(event.get("available_on_brokers")),
        "last_updated": iso_to_display(event.get("last_updated_ts")),
    }


def event_matches_search(row: dict[str, Any], search_regex: str) -> bool:
    pattern = search_regex.strip()
    if not pattern:
        return True

    haystack = " ".join(str(row.get(field, "")) for field in EVENT_SEARCH_FIELDS)
    try:
        return re.search(pattern, haystack, flags=re.IGNORECASE) is not None
    except re.error:
        return pattern.casefold() in haystack.casefold()


def event_matches_category(row: dict[str, Any], category: str) -> bool:
    selected_category = category.strip()
    if not selected_category or selected_category == "All":
        return True
    return row.get("category", "") == selected_category


def market_row(market: dict[str, Any], series_ticker: str = "") -> dict[str, Any]:
    yes_bid = pct(market.get("yes_bid_dollars"))
    yes_ask = pct(market.get("yes_ask_dollars"))
    last_price = pct(market.get("last_price_dollars"))
    previous_price = pct(market.get("previous_price_dollars"))
    price_change = None
    if previous_price > 0:
        price_change = round(last_price - previous_price, 2)
    event_ticker = market.get("event_ticker", "")
    market_ticker = market.get("ticker", "")

    return {
        "market_key": build_market_key(series_ticker, market_ticker, event_ticker),
        "ticker": market_ticker,
        "event_ticker": event_ticker,
        "series_ticker": series_ticker,
        "title": market.get("title", ""),
        "subtitle": market.get("yes_sub_title")
        or market.get("subtitle")
        or market.get("no_sub_title")
        or "",
        "status": market.get("status", ""),
        "yes_bid_pct": yes_bid,
        "yes_ask_pct": yes_ask,
        "last_price_pct": last_price,
        "price_change_points": price_change,
        "spread_points": round(max(yes_ask - yes_bid, 0), 2),
        "volume_24h": quantity(market.get("volume_24h_fp")),
        "volume_total": quantity(market.get("volume_fp")),
        "open_interest": quantity(market.get("open_interest_fp")),
        "liquidity": money(market.get("liquidity_dollars")),
        "close_time": iso_to_display(market.get("close_time")),
        "updated_time": iso_to_display(market.get("updated_time")),
    }


def trade_row(trade: dict[str, Any]) -> dict[str, Any]:
    count = quantity(trade.get("count_fp"))
    yes_price = money(trade.get("yes_price_dollars"))
    no_price = money(trade.get("no_price_dollars"))
    outcome_side = trade.get("taker_outcome_side") or trade.get("taker_side", "")
    trade_price = no_price if outcome_side == "no" else yes_price
    return {
        "created_time": iso_to_display(trade.get("created_time")),
        "ticker": trade.get("ticker", ""),
        "trade_id": trade.get("trade_id", ""),
        "count": count,
        "yes_price": yes_price,
        "no_price": no_price,
        "trade_price": trade_price,
        "notional": round(count * trade_price, 2),
        "taker_side": trade.get("taker_side", ""),
        "taker_outcome_side": outcome_side,
        "taker_book_side": trade.get("taker_book_side", ""),
    }


async def fetch_market_trades(market_ticker: str, limit: int = 100) -> list[dict[str, Any]]:
    data = await kalshi_get(
        "/markets/trades",
        {"limit": clamp_limit(limit, maximum=1000), "ticker": market_ticker},
        ttl=15,
    )
    trades = data.get("trades", [])
    return trades if isinstance(trades, list) else []


def empty_figure(message: str, theme: str) -> JSONResponse:
    fig = go.Figure()
    fig.add_annotation(
        text=message,
        xref="paper",
        yref="paper",
        x=0.5,
        y=0.5,
        showarrow=False,
    )
    fig.update_layout(
        template="plotly_dark" if theme == "dark" else "plotly_white",
        margin=dict(l=36, r=24, t=20, b=36),
    )
    return JSONResponse(content=json.loads(fig.to_json()))


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "status": "ok",
        "app": "Kalshi Market Dashboard",
        "kalshi_api": KALSHI_API_BASE_URL,
    }


@app.get("/widgets.json")
async def get_widgets() -> JSONResponse:
    return JSONResponse(content=read_json_file("widgets.json"))


@app.get("/apps.json")
async def get_apps() -> JSONResponse:
    return JSONResponse(content=read_json_file("apps.json"))


@app.get("/thumbnail.svg", include_in_schema=False)
async def thumbnail() -> Response:
    svg = """
<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="675" viewBox="0 0 1200 675" role="img" aria-labelledby="title desc">
  <title id="title">Kalshi</title>
  <desc id="desc">Kalshi wordmark on a brand green background.</desc>
  <rect width="1200" height="675" fill="#21c891"/>
  <text x="600" y="358" text-anchor="middle" dominant-baseline="middle" fill="#ffffff" font-family="Inter, Arial, sans-serif" font-size="184" font-weight="800">Kalshi</text>
</svg>
    """.strip()
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/market_options")
async def market_options(
    status: str = Query("open"),
    limit: int = Query(100, ge=1, le=200),
    event_ticker: str | None = Query(None),
) -> list[dict[str, str]]:
    if event_ticker:
        selected_event = await resolve_event(event_ticker)
        data = {"markets": selected_event["markets"]}
    else:
        selected_event = {"series_ticker": ""}
        data = await kalshi_get(
            "/markets",
            {
                "limit": clamp_limit(limit, maximum=200),
                "status": status if status != "all" else None,
                "mve_filter": "exclude",
            },
            ttl=20,
        )
    markets = sorted(
        data.get("markets", []),
        key=lambda item: (
            to_float(item.get("volume_24h_fp")),
            to_float(item.get("volume_fp")),
        ),
        reverse=True,
    )

    options = []
    seen: set[str] = set()
    for market in markets:
        market_ticker = market.get("ticker", "")
        if not market_ticker or market_ticker in seen:
            continue
        seen.add(market_ticker)
        label_text = market.get("yes_sub_title") or market.get("title") or market_ticker
        label = f"{label_text[:80]} ({market_ticker})"
        options.append(
            {
                "label": label,
                "value": build_market_key(
                    selected_event["series_ticker"],
                    market_ticker,
                    market.get("event_ticker", ""),
                ),
            }
        )
    return options


@app.get("/event_options")
async def event_options(
    status: str = Query("open"),
    limit: int = Query(200, ge=1, le=200),
) -> list[dict[str, str]]:
    data = await kalshi_get(
        "/events",
        {
            "limit": clamp_limit(limit, maximum=200),
            "status": status if status != "all" else None,
            "with_nested_markets": False,
        },
        ttl=60,
    )
    options = []
    for event in data.get("events", []):
        event_ticker = event.get("event_ticker", "")
        title = event.get("title") or event_ticker
        category = event.get("category") or "Kalshi"
        if event_ticker:
            options.append(
                {
                    "label": f"{title[:90]} ({category})",
                    "value": event_ticker,
                }
            )
    return options


@app.get("/event_category_options")
async def event_category_options(
    status: str = Query("open"),
    limit: int = Query(200, ge=1, le=1000),
) -> list[dict[str, str]]:
    max_events_to_scan = clamp_limit(limit, maximum=1000)
    cursor: str | None = None
    categories: set[str] = set()
    scanned = 0

    while scanned < max_events_to_scan:
        page_limit = min(200, max_events_to_scan - scanned)
        try:
            data = await kalshi_get(
                "/events",
                {
                    "cursor": cursor,
                    "limit": page_limit,
                    "status": status if status != "all" else None,
                    "with_nested_markets": False,
                },
                ttl=300,
            )
        except HTTPException:
            break
        events = data.get("events", [])
        if not isinstance(events, list) or not events:
            break

        for event in events:
            scanned += 1
            category = event.get("category")
            if isinstance(category, str) and category.strip():
                categories.add(category.strip())

        cursor = data.get("cursor")
        if not cursor:
            break

    return [{"label": "All", "value": "All"}] + [
        {"label": category, "value": category}
        for category in sorted(categories, key=str.casefold)
    ]


@app.get("/exchange_status")
async def exchange_status() -> list[dict[str, str]]:
    status_data = await kalshi_get("/exchange/status", ttl=15)
    markets_data = await kalshi_get(
        "/markets",
        {"limit": 100, "status": "open", "mve_filter": "exclude"},
        ttl=20,
    )
    trades_data = await kalshi_get("/markets/trades", {"limit": 100}, ttl=15)

    markets = markets_data.get("markets", [])
    trades = trades_data.get("trades", [])
    total_contracts = sum(to_float(trade.get("count_fp")) for trade in trades)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return [
        {
            "label": "Exchange",
            "value": "Active" if status_data.get("exchange_active") else "Paused",
            "subvalue": timestamp,
        },
        {
            "label": "Trading",
            "value": "Active" if status_data.get("trading_active") else "Paused",
            "subvalue": status_data.get("exchange_estimated_resume_time") or "Live status",
        },
        {
            "label": "Open Markets Sample",
            "value": str(len(markets)),
            "subvalue": "First page excluding multivariate markets",
        },
        {
            "label": "Recent Contracts",
            "value": compact_number(total_contracts),
            "subvalue": f"{len(trades)} latest public trades",
        },
    ]


@app.get("/featured_markets")
async def featured_markets(
    status: str = Query("open"),
    limit: int = Query(50, ge=1, le=200),
    market_key: str = Query(DEFAULT_MARKET_KEY),
) -> list[dict[str, Any]]:
    del market_key
    data = await kalshi_get(
        "/markets",
        {
            "limit": clamp_limit(limit, maximum=200),
            "status": status if status != "all" else None,
            "mve_filter": "exclude",
        },
        ttl=20,
    )
    rows = [market_row(market) for market in data.get("markets", [])]
    return sorted(
        rows,
        key=lambda row: (row["volume_24h"], row["volume_total"]),
        reverse=True,
    )


@app.get("/event_metrics")
async def event_metrics(
    event_ticker: str = Query(DEFAULT_EVENT_TICKER),
) -> list[dict[str, str]]:
    selected = await resolve_event(event_ticker)
    event = selected["event"]
    markets = [market_row(market, selected["series_ticker"]) for market in selected["markets"]]
    total_volume = sum(row["volume_total"] for row in markets)
    total_open_interest = sum(row["open_interest"] for row in markets)
    active_markets = [row for row in markets if row["status"] in {"active", "open"}]
    highest_market = max(markets, key=lambda row: row["last_price_pct"], default=None)

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
            "value": (
                f"{highest_market['last_price_pct']:.1f}%"
                if highest_market
                else "N/A"
            ),
            "subvalue": (
                highest_market["subtitle"][:48]
                if highest_market
                else "No markets returned"
            ),
        },
    ]


@app.get("/event_brief")
async def event_brief(
    event_ticker: str = Query(DEFAULT_EVENT_TICKER),
) -> str:
    selected = await resolve_event(event_ticker)
    event = selected["event"]
    markets = [market_row(market, selected["series_ticker"]) for market in selected["markets"]]
    top_markets = sorted(
        markets,
        key=lambda row: (row["last_price_pct"], row["volume_total"]),
        reverse=True,
    )[:5]
    top_rows = "\n".join(
        f"| {row['subtitle'] or row['ticker']} | {row['last_price_pct']:.1f}% | "
        f"{compact_number(row['volume_total'])} |"
        for row in top_markets
    )
    if not top_rows:
        top_rows = "| No markets returned | N/A | N/A |"

    return f"""## {event.get("title", selected["event_ticker"])}

**Event:** `{selected["event_ticker"]}`  
**Series:** `{selected["series_ticker"]}`  
**Category:** {event.get("category", "Unknown")}  
**Subtitle:** {event.get("sub_title") or "N/A"}

| Leading outcome | Yes probability | Volume |
| --- | ---: | ---: |
{top_rows}
"""


@app.get("/event_markets")
async def event_markets(
    event_ticker: str = Query(DEFAULT_EVENT_TICKER),
    market_key: str = Query(DEFAULT_MARKET_KEY),
) -> list[dict[str, Any]]:
    del market_key
    selected = await resolve_event(event_ticker)
    rows = [market_row(market, selected["series_ticker"]) for market in selected["markets"]]
    return sorted(
        rows,
        key=lambda row: (row["volume_24h"], row["volume_total"], row["last_price_pct"]),
        reverse=True,
    )


@app.get("/market_metrics")
async def market_metrics(
    market_key: str = Query(DEFAULT_MARKET_KEY),
) -> list[dict[str, str]]:
    selected = await resolve_market(market_key)
    market = selected["market"]
    event = selected["event"]
    trades = await fetch_market_trades(selected["market_ticker"], limit=100)
    trade_rows = [trade_row(trade) for trade in trades]
    yes_bid = pct(market.get("yes_bid_dollars"))
    yes_ask = pct(market.get("yes_ask_dollars"))
    market_last_price = pct(market.get("last_price_dollars"))
    spread = max(yes_ask - yes_bid, 0)
    latest_trade = trade_rows[0] if trade_rows else None
    oldest_trade = trade_rows[-1] if len(trade_rows) > 1 else None
    recent_contracts = sum(row["count"] for row in trade_rows)

    if latest_trade:
        displayed_price = latest_trade["yes_price"] * 100
        latest_time = latest_trade["created_time"] or "latest trade"
    else:
        displayed_price = market_last_price
        latest_time = "last market print"

    delta = None
    if latest_trade and oldest_trade:
        delta_value = (latest_trade["yes_price"] - oldest_trade["yes_price"]) * 100
        delta = f"{delta_value:+.1f} pts"

    metrics = [
        {
            "label": "Latest Trade",
            "value": f"{displayed_price:.1f}%",
            "subvalue": latest_time,
        },
        {
            "label": "Yes Bid / Ask",
            "value": f"{yes_bid:.1f}% / {yes_ask:.1f}%",
            "subvalue": f"Spread {spread:.1f} pts",
        },
        {
            "label": "Recent Tape",
            "value": compact_number(recent_contracts),
            "subvalue": f"{len(trade_rows)} latest trades",
        },
        {
            "label": "Open Interest",
            "value": compact_number(market.get("open_interest_fp")),
            "subvalue": f"Total volume {compact_number(market.get('volume_fp'))}",
        },
        {
            "label": "Event",
            "value": event.get("category", "Kalshi"),
            "subvalue": selected["event_ticker"] or market.get("event_ticker", ""),
        },
    ]
    if delta:
        metrics[0]["delta"] = delta
    return metrics


@app.get("/market_brief")
async def market_brief(
    market_key: str = Query(DEFAULT_MARKET_KEY),
) -> str:
    selected = await resolve_market(market_key)
    market = selected["market"]
    event = selected["event"]

    yes_bid = pct(market.get("yes_bid_dollars"))
    yes_ask = pct(market.get("yes_ask_dollars"))
    last_price = pct(market.get("last_price_dollars"))
    close_time = iso_to_display(market.get("close_time")) or "Not provided"
    rules = market.get("rules_primary") or "Rules were not included in the API response."

    return f"""## {market.get("title", selected["market_ticker"])}

**Market:** `{selected["market_ticker"]}`  
**Event:** `{selected["event_ticker"]}`  
**Series:** `{selected["series_ticker"]}`  
**Category:** {event.get("category", "Unknown")}

| Measure | Value |
| --- | ---: |
| Last price | {last_price:.1f}% |
| Yes bid | {yes_bid:.1f}% |
| Yes ask | {yes_ask:.1f}% |
| 24h volume | {compact_number(market.get("volume_24h_fp"))} contracts |
| Open interest | {compact_number(market.get("open_interest_fp"))} contracts |
| Close time | {close_time} |

### Resolution
{rules}
"""


@app.get("/market_probability_gauge")
async def market_probability_gauge(
    market_key: str = Query(DEFAULT_MARKET_KEY),
    theme: str = Query("dark"),
    raw: bool = Query(False),
) -> Any:
    selected = await resolve_market(market_key)
    market = selected["market"]
    trades = await fetch_market_trades(selected["market_ticker"], limit=1)
    latest_trade = trade_row(trades[0]) if trades else None
    yes_bid = pct(market.get("yes_bid_dollars"))
    yes_ask = pct(market.get("yes_ask_dollars"))
    last_price = pct(market.get("last_price_dollars"))
    midpoint = round((yes_bid + yes_ask) / 2, 2) if yes_bid or yes_ask else 0

    yes_probability = (
        round(latest_trade["yes_price"] * 100, 2)
        if latest_trade
        else last_price or midpoint
    )
    no_probability = round(100 - yes_probability, 2)
    rows = [
        {
            "side": "YES",
            "probability_pct": yes_probability,
            "bid_pct": yes_bid,
            "ask_pct": yes_ask,
            "source": "latest trade" if latest_trade else "market midpoint",
        },
        {
            "side": "NO",
            "probability_pct": no_probability,
            "bid_pct": pct(market.get("no_bid_dollars")),
            "ask_pct": pct(market.get("no_ask_dollars")),
            "source": "derived",
        },
    ]
    if raw:
        return rows

    yes_probability = max(0, min(100, yes_probability))
    no_probability = round(100 - yes_probability, 2)

    fig = go.Figure()
    for x0, x1, color in (
        (0, 40, "rgba(235, 87, 87, 0.25)"),
        (40, 60, "rgba(242, 201, 76, 0.28)"),
        (60, 100, "rgba(39, 174, 96, 0.25)"),
    ):
        fig.add_shape(
            type="rect",
            x0=x0,
            x1=x1,
            y0=-0.16,
            y1=0.16,
            line=dict(width=0),
            fillcolor=color,
            layer="below",
        )
    fig.add_shape(
        type="line",
        x0=yes_probability,
        x1=yes_probability,
        y0=-0.26,
        y1=0.26,
        line=dict(color="#2f80ed", width=4),
    )
    fig.add_trace(
        go.Scatter(
            x=[yes_probability],
            y=[0],
            mode="markers",
            marker=dict(size=13, color="#2f80ed", symbol="diamond"),
            name="YES probability",
            customdata=[[no_probability, yes_bid, yes_ask]],
            hovertemplate=(
                "YES %{x:.1f}%<br>"
                "NO %{customdata[0]:.1f}%<br>"
                "Bid/ask %{customdata[1]:.1f}% / %{customdata[2]:.1f}%"
                "<extra></extra>"
            ),
        )
    )
    fig.add_annotation(
        text=f"<b>YES {yes_probability:.1f}%</b>",
        x=0,
        y=0.62,
        xref="paper",
        yref="paper",
        xanchor="left",
        showarrow=False,
        font=dict(size=15, color="#27ae60"),
    )
    fig.add_annotation(
        text=f"<b>NO {no_probability:.1f}%</b>",
        x=1,
        y=0.62,
        xref="paper",
        yref="paper",
        xanchor="right",
        showarrow=False,
        font=dict(size=15, color="#eb5757"),
    )
    fig.add_annotation(
        text=f"Bid/ask {yes_bid:.1f}% / {yes_ask:.1f}% | {rows[0]['source']}",
        x=0.5,
        y=0.04,
        xref="paper",
        yref="paper",
        showarrow=False,
        font=dict(size=11),
    )
    fig.update_xaxes(
        range=[0, 100],
        tickmode="array",
        tickvals=[0, 25, 50, 75, 100],
        ticksuffix="%",
        fixedrange=True,
        showgrid=False,
        zeroline=False,
    )
    fig.update_yaxes(
        range=[-0.45, 0.7],
        visible=False,
        fixedrange=True,
    )
    fig.update_layout(
        template="plotly_dark" if theme == "dark" else "plotly_white",
        showlegend=False,
        margin=dict(l=22, r=22, t=6, b=28),
        hovermode="closest",
    )
    chart = json.loads(fig.to_json())
    chart["config"] = {
        "displayModeBar": False,
        "doubleClick": False,
        "scrollZoom": False,
    }
    return JSONResponse(content=chart)


@app.get("/market_probability_table")
async def market_probability_table(
    market_key: str = Query(DEFAULT_MARKET_KEY),
) -> list[dict[str, Any]]:
    rows = await market_probability_gauge(market_key=market_key, raw=True)
    return rows if isinstance(rows, list) else []


@app.get("/market_price_chart")
async def market_price_chart(
    market_key: str = Query(DEFAULT_MARKET_KEY),
    days: int = Query(30, ge=1, le=365),
    period_interval: int = Query(1440),
    theme: str = Query("dark"),
    raw: bool = Query(False),
) -> Any:
    selected = await resolve_market(market_key)
    interval = int(period_interval)
    if interval not in {1, 60, 1440}:
        interval = 1440

    end_ts = int(datetime.now(timezone.utc).timestamp())
    start_ts = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    data = await kalshi_get(
        f"/series/{selected['series_ticker']}/markets/{selected['market_ticker']}/candlesticks",
        {
            "start_ts": start_ts,
            "end_ts": end_ts,
            "period_interval": interval,
            "include_latest_before_start": True,
        },
        ttl=30,
    )

    rows = []
    for candle in data.get("candlesticks", []):
        price = candle.get("price") or {}
        yes_bid = candle.get("yes_bid") or {}
        yes_ask = candle.get("yes_ask") or {}
        rows.append(
            {
                "date": timestamp_to_iso(candle.get("end_period_ts")),
                "open": pct(price.get("open_dollars")),
                "high": pct(price.get("high_dollars")),
                "low": pct(price.get("low_dollars")),
                "close": pct(price.get("close_dollars")),
                "mean": pct(price.get("mean_dollars")),
                "yes_bid_close": pct(yes_bid.get("close_dollars")),
                "yes_ask_close": pct(yes_ask.get("close_dollars")),
                "volume": quantity(candle.get("volume_fp")),
                "open_interest": quantity(candle.get("open_interest_fp")),
            }
        )

    if not rows:
        trades = await fetch_market_trades(selected["market_ticker"], limit=1000)
        for trade in reversed(trades):
            trade_time = parse_iso_time(trade.get("created_time"))
            if not trade_time:
                continue
            row = trade_row(trade)
            price_pct = round(row["yes_price"] * 100, 2)
            rows.append(
                {
                    "date": trade_time.isoformat(),
                    "open": price_pct,
                    "high": price_pct,
                    "low": price_pct,
                    "close": price_pct,
                    "mean": price_pct,
                    "yes_bid_close": None,
                    "yes_ask_close": None,
                    "volume": row["count"],
                    "open_interest": quantity(selected["market"].get("open_interest_fp")),
                    "source": "trades",
                }
            )

    if not rows:
        market = selected["market"]
        now = datetime.now(timezone.utc).isoformat()
        current_price = pct(market.get("last_price_dollars"))
        rows.append(
            {
                "date": now,
                "open": current_price,
                "high": current_price,
                "low": current_price,
                "close": current_price,
                "mean": current_price,
                "yes_bid_close": pct(market.get("yes_bid_dollars")),
                "yes_ask_close": pct(market.get("yes_ask_dollars")),
                "volume": quantity(market.get("volume_24h_fp")),
                "open_interest": quantity(market.get("open_interest_fp")),
                "source": "market",
            }
        )

    if raw:
        return rows

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    has_ohlc = any(
        all(row.get(field) is not None for field in ("open", "high", "low", "close"))
        for row in rows
    )
    if has_ohlc:
        fig.add_trace(
            go.Candlestick(
                x=[row["date"] for row in rows],
                open=[row["open"] for row in rows],
                high=[row["high"] for row in rows],
                low=[row["low"] for row in rows],
                close=[row["close"] for row in rows],
                name="YES Price",
                increasing_line_color="#27ae60",
                decreasing_line_color="#eb5757",
            ),
            secondary_y=False,
        )
    else:
        fig.add_trace(
            go.Scatter(
                x=[row["date"] for row in rows],
                y=[row["close"] for row in rows],
                mode="lines",
                name="YES Price",
                line=dict(width=2.5, color="#2f80ed"),
            ),
            secondary_y=False,
        )
    if any(row.get("yes_bid_close") is not None for row in rows):
        fig.add_trace(
            go.Scatter(
                x=[row["date"] for row in rows],
                y=[row["yes_bid_close"] for row in rows],
                mode="lines",
                name="Yes Bid",
                line=dict(width=1.5, color="#27ae60"),
            ),
            secondary_y=False,
        )
    if any(row.get("yes_ask_close") is not None for row in rows):
        fig.add_trace(
            go.Scatter(
                x=[row["date"] for row in rows],
                y=[row["yes_ask_close"] for row in rows],
                mode="lines",
                name="Yes Ask",
                line=dict(width=1.5, color="#f2994a"),
            ),
            secondary_y=False,
        )
    fig.add_trace(
        go.Bar(
            x=[row["date"] for row in rows],
            y=[row["volume"] for row in rows],
            name="Volume",
            marker=dict(color="rgba(130, 130, 130, 0.35)"),
        ),
        secondary_y=True,
    )
    fig.update_xaxes(fixedrange=True)
    fig.update_yaxes(
        title_text="Probability (%)",
        range=[0, 100],
        fixedrange=True,
        secondary_y=False,
    )
    fig.update_yaxes(
        title_text="Contracts",
        fixedrange=True,
        secondary_y=True,
        showgrid=False,
    )
    fig.update_layout(
        template="plotly_dark" if theme == "dark" else "plotly_white",
        dragmode=False,
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        margin=dict(l=48, r=42, t=24, b=42),
        bargap=0.15,
        xaxis_rangeslider_visible=False,
    )
    chart = json.loads(fig.to_json())
    chart["config"] = {
        "displayModeBar": False,
        "doubleClick": False,
        "scrollZoom": False,
    }
    return JSONResponse(content=chart)


@app.get("/market_history_table")
async def market_history_table(
    market_key: str = Query(DEFAULT_MARKET_KEY),
    days: int = Query(30, ge=1, le=365),
    period_interval: int = Query(1440),
    limit: int = Query(500, ge=1, le=5000),
) -> list[dict[str, Any]]:
    rows = await market_price_chart(
        market_key=market_key,
        days=days,
        period_interval=period_interval,
        theme="dark",
        raw=True,
    )
    rows = rows[-clamp_limit(limit, maximum=5000) :]
    return [
        {
            "date": row.get("date", ""),
            "source": row.get("source", "candlesticks"),
            "open_pct": row.get("open"),
            "high_pct": row.get("high"),
            "low_pct": row.get("low"),
            "close_pct": row.get("close"),
            "mean_pct": row.get("mean"),
            "yes_bid_pct": row.get("yes_bid_close"),
            "yes_ask_pct": row.get("yes_ask_close"),
            "volume": row.get("volume"),
            "open_interest": row.get("open_interest"),
        }
        for row in rows
    ]


@app.get("/market_orderbook")
async def market_orderbook(
    market_key: str = Query(DEFAULT_MARKET_KEY),
    depth: int = Query(10, ge=1, le=100),
) -> list[dict[str, Any]]:
    selected = await resolve_market(market_key)
    data = await kalshi_get(
        f"/markets/{selected['market_ticker']}/orderbook",
        {"depth": depth},
        ttl=10,
    )
    orderbook = data.get("orderbook_fp", {})
    rows = []
    for side, levels in (
        ("yes", orderbook.get("yes_dollars", [])),
        ("no", orderbook.get("no_dollars", [])),
    ):
        sorted_levels = sorted(levels, key=lambda item: to_float(item[0]), reverse=True)
        for level, item in enumerate(sorted_levels, start=1):
            if len(item) < 2:
                continue
            price = money(item[0])
            contracts = quantity(item[1])
            yes_equivalent_price = price if side == "yes" else max(0, 1 - price)
            rows.append(
                {
                    "side": side.upper(),
                    "level": level,
                    "price": price,
                    "probability_pct": round(price * 100, 2),
                    "yes_equivalent_pct": round(yes_equivalent_price * 100, 2),
                    "contracts": contracts,
                    "notional": round(price * contracts, 2),
                    "yes_equivalent_notional": round(
                        yes_equivalent_price * contracts,
                        2,
                    ),
                }
            )
    return rows


@app.get("/orderbook_depth_chart")
async def orderbook_depth_chart(
    market_key: str = Query(DEFAULT_MARKET_KEY),
    depth: int = Query(10, ge=1, le=100),
    theme: str = Query("dark"),
) -> Any:
    rows = await market_orderbook(market_key=market_key, depth=depth)
    yes_rows = [row for row in rows if row["side"] == "YES"]
    no_rows = [row for row in rows if row["side"] == "NO"]

    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=[row["contracts"] for row in yes_rows],
            y=[row["yes_equivalent_pct"] for row in yes_rows],
            orientation="h",
            name="YES bids",
            marker=dict(color="#27ae60"),
            customdata=[
                [row["price"], row["contracts"], row["notional"]] for row in yes_rows
            ],
            hovertemplate=(
                "YES bid %{customdata[0]:.2f}<br>"
                "Implied YES %{y:.1f}%<br>"
                "Contracts %{customdata[1]:,.0f}<br>"
                "Notional %{customdata[2]:,.2f}<extra></extra>"
            ),
        )
    )
    fig.add_trace(
        go.Bar(
            x=[-row["contracts"] for row in no_rows],
            y=[row["yes_equivalent_pct"] for row in no_rows],
            orientation="h",
            name="NO bids / YES asks",
            marker=dict(color="#eb5757"),
            customdata=[
                [row["price"], row["contracts"], row["notional"]] for row in no_rows
            ],
            hovertemplate=(
                "NO bid %{customdata[0]:.2f}<br>"
                "Implied YES ask %{y:.1f}%<br>"
                "Contracts %{customdata[1]:,.0f}<br>"
                "Notional %{customdata[2]:,.2f}<extra></extra>"
            ),
        )
    )
    fig.update_layout(
        template="plotly_dark" if theme == "dark" else "plotly_white",
        barmode="relative",
        bargap=0.2,
        hovermode="closest",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        margin=dict(l=48, r=28, t=24, b=42),
        xaxis=dict(
            title="Contracts (YES bids right, implied YES asks left)",
            zeroline=True,
            zerolinewidth=2,
        ),
        yaxis=dict(title="Implied YES price (%)", range=[0, 100]),
    )
    return JSONResponse(content=json.loads(fig.to_json()))


def _ladder_side_html(rows: list[dict[str, Any]], side: str, max_contracts: float) -> str:
    empty = """
        <div class="empty-state">No visible levels</div>
    """
    if not rows:
        return empty

    cells = []
    for row in rows:
        contracts = float(row.get("contracts") or 0)
        bar_width = 0 if max_contracts <= 0 else min(100, contracts / max_contracts * 100)
        price_label = f"{row.get('yes_equivalent_pct', 0):.1f}%"
        side_price = f"{row.get('probability_pct', 0):.1f}%"
        label = "YES bid" if side == "yes" else f"from NO bid {side_price}"
        notional = float(row.get("yes_equivalent_notional") or row.get("notional") or 0)
        cells.append(
            f"""
            <div class="level {side}">
                <div class="bar" style="width:{bar_width:.2f}%"></div>
                <div class="level-main">
                    <span class="price">{escape(price_label)}</span>
                    <span class="meta">{escape(label)}</span>
                </div>
                <div class="size">
                    <span>{contracts:,.0f}</span>
                    <small>${notional:,.0f}</small>
                </div>
            </div>
            """
        )
    return "\n".join(cells)


@app.get("/orderbook_ladder")
async def orderbook_ladder(
    market_key: str = Query(DEFAULT_MARKET_KEY),
    depth: int = Query(10, ge=1, le=100),
    raw: bool = Query(False),
    theme: str = Query("dark"),
) -> Any:
    selected = await resolve_market(market_key)
    rows = await market_orderbook(market_key=market_key, depth=depth)
    if raw:
        return rows

    yes_rows = [row for row in rows if row["side"] == "YES"]
    no_rows = [row for row in rows if row["side"] == "NO"]
    best_bid = yes_rows[0]["yes_equivalent_pct"] if yes_rows else None
    best_ask = no_rows[0]["yes_equivalent_pct"] if no_rows else None
    spread = (
        round(float(best_ask) - float(best_bid), 2)
        if best_bid is not None and best_ask is not None
        else None
    )
    max_contracts = max([float(row.get("contracts") or 0) for row in rows] or [0])
    total_yes = sum(float(row.get("contracts") or 0) for row in yes_rows)
    total_no = sum(float(row.get("contracts") or 0) for row in no_rows)
    market = selected["market"]
    market_ticker = selected["market_ticker"]
    title = market.get("title") or selected["market_ticker"]
    subtitle = market.get("yes_sub_title") or market.get("subtitle") or ""
    is_light = theme == "light"

    html = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root {{
      color-scheme: {"light" if is_light else "dark"};
      --bg: {"#ffffff" if is_light else "#151518"};
      --panel: {"#f5f6f8" if is_light else "#1c1c20"};
      --panel-2: {"#eceff3" if is_light else "#232329"};
      --text: {"#1f2328" if is_light else "#f2f2f4"};
      --muted: {"#667085" if is_light else "#a0a0aa"};
      --line: {"#d8dde6" if is_light else "#34343b"};
      --yes: #27ae60;
      --yes-soft: rgba(39, 174, 96, 0.22);
      --no: #eb5757;
      --no-soft: rgba(235, 87, 87, 0.22);
      --accent: #2f80ed;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      overflow: hidden;
    }}
    .shell {{
      height: 100vh;
      display: grid;
      grid-template-rows: auto auto 1fr;
      gap: 10px;
      padding: 0 12px 12px;
    }}
    .header {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 12px;
      align-items: start;
      border-bottom: 1px solid var(--line);
      padding-bottom: 10px;
    }}
    .title {{
      font-size: 13px;
      font-weight: 650;
      line-height: 1.25;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .subtitle {{
      margin-top: 3px;
      font-size: 11px;
      color: var(--muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .ticker {{
      text-align: right;
      font-size: 11px;
      color: var(--muted);
      font-variant-numeric: tabular-nums;
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
    }}
    .stat {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px;
      min-width: 0;
    }}
    .stat span {{
      display: block;
      color: var(--muted);
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: .04em;
    }}
    .stat strong {{
      display: block;
      margin-top: 3px;
      font-size: 15px;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .book {{
      min-height: 0;
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      gap: 10px;
    }}
    .side {{
      min-height: 0;
      display: grid;
      grid-template-rows: auto 1fr;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 6px;
      overflow: hidden;
    }}
    .side-title {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 8px 10px;
      background: var(--panel-2);
      border-bottom: 1px solid var(--line);
      font-size: 11px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: .04em;
    }}
    .levels {{
      min-height: 0;
      overflow: auto;
    }}
    .level {{
      position: relative;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      align-items: center;
      min-height: 36px;
      padding: 6px 9px;
      border-bottom: 1px solid var(--line);
      isolation: isolate;
    }}
    .level .bar {{
      position: absolute;
      z-index: -1;
    }}
    .level.yes .bar {{
      inset: 4px 0 4px auto;
      background: var(--yes-soft);
      border-radius: 4px 0 0 4px;
    }}
    .level.no .bar {{
      inset: 4px auto 4px 0;
      background: var(--no-soft);
      border-radius: 0 4px 4px 0;
    }}
    .price {{
      display: block;
      font-size: 14px;
      font-weight: 650;
      font-variant-numeric: tabular-nums;
    }}
    .yes .price {{ color: var(--yes); }}
    .no .price {{ color: var(--no); }}
    .meta {{
      display: block;
      margin-top: 2px;
      color: var(--muted);
      font-size: 10px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .size {{
      text-align: right;
      font-variant-numeric: tabular-nums;
      font-size: 12px;
    }}
    .size small {{
      display: block;
      margin-top: 2px;
      color: var(--muted);
      font-size: 10px;
    }}
    .empty-state {{
      padding: 14px;
      color: var(--muted);
      font-size: 12px;
    }}
  </style>
</head>
<body>
  <main class="shell">
    <section class="header">
      <div>
        <div class="title">{escape(title)}</div>
        <div class="subtitle">{escape(subtitle)}</div>
      </div>
      <div class="ticker">{escape(market_ticker)}</div>
    </section>
    <section class="stats">
      <div class="stat"><span>Best bid</span><strong>{f"{best_bid:.1f}%" if best_bid is not None else "N/A"}</strong></div>
      <div class="stat"><span>Best ask</span><strong>{f"{best_ask:.1f}%" if best_ask is not None else "N/A"}</strong></div>
      <div class="stat"><span>Spread</span><strong>{f"{spread:.1f} pts" if spread is not None else "N/A"}</strong></div>
      <div class="stat"><span>Depth</span><strong>{len(yes_rows) + len(no_rows)} levels</strong></div>
    </section>
    <section class="book">
      <div class="side">
        <div class="side-title"><span>YES bids</span><span>{total_yes:,.0f} contracts</span></div>
        <div class="levels">{_ladder_side_html(yes_rows, "yes", max_contracts)}</div>
      </div>
      <div class="side">
        <div class="side-title"><span>YES asks</span><span>{total_no:,.0f} contracts</span></div>
        <div class="levels">{_ladder_side_html(no_rows, "no", max_contracts)}</div>
      </div>
    </section>
  </main>
</body>
</html>
    """
    return HTMLResponse(content=html)


@app.get("/selected_trades")
async def selected_trades(
    market_key: str = Query(DEFAULT_MARKET_KEY),
    limit: int = Query(100, ge=1, le=1000),
) -> list[dict[str, Any]]:
    selected = await resolve_market(market_key)
    trades = await fetch_market_trades(selected["market_ticker"], limit=limit)
    return [trade_row(trade) for trade in trades]


@app.get("/recent_trades")
async def recent_trades(
    limit: int = Query(100, ge=1, le=1000),
) -> list[dict[str, Any]]:
    data = await kalshi_get(
        "/markets/trades",
        {"limit": clamp_limit(limit, maximum=1000)},
        ttl=15,
    )
    return [trade_row(trade) for trade in data.get("trades", [])]


@app.get("/events_table")
async def events_table(
    status: str = Query("open"),
    category: str = Query("All"),
    limit: int = Query(100, ge=1, le=200),
    search_regex: str = Query("", max_length=200),
) -> list[dict[str, Any]]:
    visible_limit = clamp_limit(limit, maximum=200)
    has_filter = bool(search_regex.strip()) or category.strip() not in ("", "All")
    max_events_to_scan = 1000 if has_filter else visible_limit
    cursor: str | None = None
    rows: list[dict[str, Any]] = []
    scanned = 0

    while scanned < max_events_to_scan and len(rows) < visible_limit:
        page_limit = min(200, max_events_to_scan - scanned)
        data = await kalshi_get(
            "/events",
            {
                "cursor": cursor,
                "limit": page_limit,
                "status": status if status != "all" else None,
                "with_nested_markets": False,
            },
            ttl=60,
        )
        events = data.get("events", [])
        if not isinstance(events, list) or not events:
            break

        for event in events:
            scanned += 1
            row = event_row(event)
            if (
                event_matches_category(row, category)
                and event_matches_search(row, search_regex)
            ):
                rows.append(row)
                if len(rows) >= visible_limit:
                    break

        cursor = data.get("cursor")
        if not cursor:
            break

    return rows[:visible_limit]


@app.get("/series_table")
async def series_table(
    category: str = Query("All"),
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"include_volume": True}
    if category and category != "All":
        params["category"] = category
    data = await kalshi_get("/series", params, ttl=300)

    rows = []
    for series in data.get("series", []):
        sources = series.get("settlement_sources") or []
        rows.append(
            {
                "series_ticker": series.get("ticker", ""),
                "title": series.get("title", ""),
                "category": series.get("category", ""),
                "frequency": series.get("frequency", ""),
                "tags": ", ".join(series.get("tags") or []),
                "volume_total": quantity(series.get("volume_fp")),
                "fee_multiplier": to_float(series.get("fee_multiplier")),
                "settlement_sources": ", ".join(
                    source.get("name", "") for source in sources if isinstance(source, dict)
                ),
                "last_updated": iso_to_display(series.get("last_updated_ts")),
            }
        )
    rows.sort(key=lambda row: row["volume_total"], reverse=True)
    return rows[: clamp_limit(limit, maximum=500)]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=7779)
