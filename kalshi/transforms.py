"""Transform raw Kalshi API objects into flat widget rows."""

from __future__ import annotations

from typing import Any

from kalshi.formatting import build_market_key, iso_to_display, money, pct, quantity, to_float


def market_row(market: dict[str, Any], series_ticker: str = "") -> dict[str, Any]:
    yes_bid = pct(market.get("yes_bid_dollars"))
    yes_ask = pct(market.get("yes_ask_dollars"))
    last_price = pct(market.get("last_price_dollars"))
    previous_price = pct(market.get("previous_price_dollars"))
    price_change = round(last_price - previous_price, 2) if previous_price > 0 else None
    event_ticker = market.get("event_ticker", "")
    market_ticker = market.get("ticker", "")

    return {
        "market_key": build_market_key(series_ticker, market_ticker, event_ticker),
        "ticker": market_ticker,
        "event_ticker": event_ticker,
        "series_ticker": series_ticker,
        "title": market.get("title", ""),
        "subtitle": (
            market.get("yes_sub_title")
            or market.get("subtitle")
            or market.get("no_sub_title")
            or ""
        ),
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


def orderbook_rows(orderbook: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for side, levels in (
        ("yes", orderbook.get("yes_dollars", [])),
        ("no", orderbook.get("no_dollars", [])),
    ):
        sorted_levels = sorted(
            (lvl for lvl in levels if isinstance(lvl, (list, tuple)) and len(lvl) >= 2),
            key=lambda item: to_float(item[0]),
            reverse=True,
        )
        for level, item in enumerate(sorted_levels, start=1):
            price = money(item[0])
            contracts = quantity(item[1])
            yes_equivalent_price = price if side == "yes" else max(0.0, 1 - price)
            rows.append(
                {
                    "side": side.upper(),
                    "level": level,
                    "price": price,
                    "probability_pct": round(price * 100, 2),
                    "yes_equivalent_pct": round(yes_equivalent_price * 100, 2),
                    "contracts": contracts,
                    "notional": round(price * contracts, 2),
                    "yes_equivalent_notional": round(yes_equivalent_price * contracts, 2),
                }
            )
    return rows
