"""Backend metadata, Workspace manifests, and thumbnail."""

from __future__ import annotations

import json
from copy import deepcopy
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from kalshi.config import ROOT_PATH
from kalshi.constants import TOP_HISTORY_MARKET_COUNT
from kalshi.dependencies import get_service, get_stats, resolve_base_url
from kalshi.formatting import build_market_key, to_float
from kalshi.service import MarketDataService
from kalshi.stats import MarketStatsCache

router = APIRouter()

_THUMBNAIL_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="675" viewBox="0 0 1200 675" role="img" aria-labelledby="title desc">
  <title id="title">Kalshi</title>
  <desc id="desc">Kalshi wordmark on a brand green background.</desc>
  <rect width="1200" height="675" fill="#21c891"/>
  <text x="600" y="358" text-anchor="middle" dominant-baseline="middle" fill="#ffffff" font-family="Inter, Arial, sans-serif" font-size="184" font-weight="800">Kalshi</text>
</svg>
""".strip()


@lru_cache(maxsize=4)
def _load_manifest(name: str) -> Any:
    with (ROOT_PATH / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


@router.get("/")
async def root(request: Request) -> dict[str, str]:
    return {
        "status": "ok",
        "app": "Kalshi Market Dashboard",
        "kalshi_api": request.app.state.settings.api_base_url,
    }


def _resolve_widget_endpoints(manifest: dict[str, Any], base_url: str) -> dict[str, Any]:
    """Resolve widget endpoint URLs, option endpoints, and mcpUrls to absolute URLs."""
    resolved = deepcopy(manifest)
    base = base_url.rstrip("/")
    for widget in resolved.values():
        if not isinstance(widget, dict):
            continue
        endpoint = str(widget.get("endpoint", "")).strip()
        if endpoint and not endpoint.startswith(("http://", "https://")):
            widget["endpoint"] = f"{base}/{endpoint.lstrip('/')}"
        for param in widget.get("params", []):
            if not isinstance(param, dict):
                continue
            options_endpoint = str(param.get("optionsEndpoint", "")).strip()
            if options_endpoint and not options_endpoint.startswith(("http://", "https://")):
                param["optionsEndpoint"] = f"{base}/{options_endpoint.lstrip('/')}"
        storage = widget.get("storage")
        if isinstance(storage, dict):
            mcp_url = str(storage.get("mcpUrl", "")).strip()
            if mcp_url and not mcp_url.startswith(("http://", "https://")):
                storage["mcpUrl"] = f"{base}/{mcp_url.lstrip('/')}"
    return resolved


def _resolve_app_assets(manifest: Any, base_url: str) -> Any:
    """Resolve apps.json thumbnail paths to absolute URLs."""
    resolved = deepcopy(manifest)
    base = base_url.rstrip("/")
    apps = resolved if isinstance(resolved, list) else [resolved]
    for app in apps:
        if not isinstance(app, dict):
            continue
        for key in ("img", "img_dark", "img_light"):
            value = str(app.get(key, "")).strip()
            if value and not value.startswith(("http://", "https://")):
                app[key] = f"{base}/{value.lstrip('/')}"
    return resolved


def _is_empty_default(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, tuple, set)):
        return len(value) == 0
    return not str(value).strip()


async def _top_history_market_keys(service: MarketDataService, event_ticker: str) -> list[str]:
    """The highest-probability markets for the default history selection."""
    if not (event_ticker or "").strip():
        return []
    try:
        resolved = await service.resolve_event(event_ticker=event_ticker)
    except HTTPException:
        return []

    def probability(market: dict[str, Any]) -> float:
        return to_float(market.get("last_price_dollars")) or to_float(
            market.get("yes_bid_dollars")
        )

    markets = sorted(resolved["markets"], key=probability, reverse=True)
    return [
        build_market_key(resolved["series_ticker"], market.get("ticker", ""), resolved["event_ticker"])
        for market in markets[:TOP_HISTORY_MARKET_COUNT]
        if market.get("ticker")
    ]


async def _selection_defaults(stats: MarketStatsCache, service: MarketDataService) -> dict[str, Any]:
    """Return the live default event, market, and history outcomes."""
    event = await stats.default_event_ticker()
    market = await service.default_market_key(event) if event else ""
    history_markets = await _top_history_market_keys(service, event)
    return {"event_ticker": event, "market_key": market, "history_market_key": history_markets}


def _apply_value_defaults(manifest: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """Fill empty endpoint-param values with the resolved default."""
    for widget in manifest.values():
        if not isinstance(widget, dict):
            continue
        for param in widget.get("params", []):
            name = param.get("paramName")
            if name in defaults and defaults[name] and _is_empty_default(param.get("value")):
                param["value"] = defaults[name]
    return manifest


def _apply_group_defaults(manifest: Any, defaults: dict[str, Any]) -> Any:
    """Fill empty group defaultValues with the resolved default."""
    apps = manifest if isinstance(manifest, list) else [manifest]
    for app in apps:
        if not isinstance(app, dict):
            continue
        for group in app.get("groups", []):
            name = group.get("paramName")
            if name in defaults and defaults[name] and _is_empty_default(group.get("defaultValue")):
                group["defaultValue"] = defaults[name]
    return manifest


@router.get("/widgets.json")
async def widgets(
    request: Request,
    stats: MarketStatsCache = Depends(get_stats),
    service: MarketDataService = Depends(get_service),
) -> JSONResponse:
    manifest = _resolve_widget_endpoints(_load_manifest("widgets.json"), resolve_base_url(request))
    _apply_value_defaults(manifest, await _selection_defaults(stats, service))
    return JSONResponse(content=manifest)


@router.get("/apps.json")
async def apps(
    request: Request,
    stats: MarketStatsCache = Depends(get_stats),
    service: MarketDataService = Depends(get_service),
) -> JSONResponse:
    manifest = _resolve_app_assets(_load_manifest("apps.json"), resolve_base_url(request))
    _apply_group_defaults(manifest, await _selection_defaults(stats, service))
    return JSONResponse(content=manifest)


@router.get("/agents.json")
async def agents() -> JSONResponse:
    return JSONResponse(content={})


@router.get("/thumbnail.svg", include_in_schema=False)
async def thumbnail() -> Response:
    return Response(content=_THUMBNAIL_SVG, media_type="image/svg+xml")
