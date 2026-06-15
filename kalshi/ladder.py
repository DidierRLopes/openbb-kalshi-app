"""Kalshi-style order book: a single ladder with asks stacked above the spread
and bids below, cumulative-depth bars, and a Trade-Yes/No divider with the last
price. Toggle the YES book vs the NO book with the `side` param."""

from __future__ import annotations

from html import escape

def _price(value: float) -> str:
    return (f"{value:.1f}".rstrip("0").rstrip(".")) + "¢"


def _total(value: float) -> str:
    return f"${value:,.2f}"


def _amount(value: float) -> str:
    return f"{int(value):,}" if float(value).is_integer() else f"{value:,.2f}"


def _cumulative(levels: list[tuple[float, float]]) -> list[tuple[float, float, float]]:
    out: list[tuple[float, float, float]] = []
    cum = 0.0
    for price, contracts in levels:
        cum += contracts * price / 100.0
        out.append((price, contracts, cum))
    return out


def _row(price: float, contracts: float, cum: float, side: str, max_cum: float, label: str) -> str:
    width = 0.0 if max_cum <= 0 else min(100.0, cum / max_cum * 100)
    lab = f'<span class="seclab">{label}</span>' if label else ""
    return (
        f'<div class="row {side}"><span class="bar" style="width:{width:.1f}%"></span>{lab}'
        f'<span class="c price">{_price(price)}</span>'
        f'<span class="c ct">{_amount(contracts)}</span>'
        f'<span class="c tot">{_total(cum)}</span></div>'
    )


def render_ladder(
    *,
    title: str,
    subtitle: str,
    market_ticker: str,
    asks: list[tuple[float, float]],
    bids: list[tuple[float, float]],
    last_price: float | None,
    side: str,
    theme: str,
) -> str:
    asks_c = _cumulative(asks)
    bids_c = _cumulative(bids)
    max_cum = max([c for *_, c in (asks_c + bids_c)] or [0.0])

    ask_disp = list(reversed(asks_c))
    ask_html = "".join(
        _row(p, ct, cu, "ask", max_cum, "Asks" if i == len(ask_disp) - 1 else "")
        for i, (p, ct, cu) in enumerate(ask_disp)
    ) or '<div class="empty">No asks</div>'
    bid_html = "".join(
        _row(p, ct, cu, "bid", max_cum, "Bids" if i == 0 else "")
        for i, (p, ct, cu) in enumerate(bids_c)
    ) or '<div class="empty">No bids</div>'

    is_light = theme == "light"
    is_no = side == "no"
    side_label = "Trade No" if is_no else "Trade Yes"
    side_var = "var(--no)" if is_no else "var(--yes)"
    last_label = f"Last {_price(last_price)}" if last_price else ""

    return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root {{
      color-scheme: {"light" if is_light else "dark"};
      --bg: {"#ffffff" if is_light else "#0e0e10"};
      --text: {"#1f2328" if is_light else "#f2f2f4"};
      --muted: {"#8b8f98" if is_light else "#86868f"};
      --line: {"#e2e6ec" if is_light else "#26262b"};
      --yes: #2fbd6b; --no: #f2566a;
      --yes-bar: {"rgba(47,189,107,0.14)" if is_light else "rgba(47,189,107,0.16)"};
      --no-bar: {"rgba(242,86,106,0.12)" if is_light else "rgba(242,86,106,0.15)"};
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; min-height: 100vh; background: var(--bg); color: var(--text); overflow: hidden;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    .shell {{ height: 100vh; display: grid; grid-template-rows: auto auto 1fr; }}
    .head {{ display: grid; grid-template-columns: minmax(0,1fr) auto; gap: 12px; align-items: start;
      padding: 7px 18px 5px; border-bottom: 1px solid var(--line); }}
    .title {{ font-size: 15px; font-weight: 680; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .subtitle {{ margin-top: 3px; font-size: 12px; color: var(--muted); }}
    .ticker {{ text-align: right; font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; }}
    .colhead {{ display: grid; grid-template-columns: 1fr 1fr 1fr; padding: 4px 18px; font-size: 11px; color: var(--muted); }}
    .colhead span {{ text-align: right; }}
    .book {{ min-height: 0; overflow: auto; }}
    .row {{ position: relative; display: grid; grid-template-columns: 1fr 1fr 1fr; align-items: center;
      min-height: 25px; padding: 2px 18px; }}
    .bar {{ position: absolute; left: 0; top: 3px; bottom: 3px; z-index: 0; border-radius: 0 3px 3px 0; }}
    .row.ask .bar {{ background: var(--no-bar); }}
    .row.bid .bar {{ background: var(--yes-bar); }}
    .c {{ position: relative; z-index: 1; text-align: right; font-variant-numeric: tabular-nums; }}
    .price {{ font-size: 14px; font-weight: 650; }}
    .row.ask .price {{ color: var(--no); }} .row.bid .price {{ color: var(--yes); }}
    .ct {{ font-size: 13px; }} .tot {{ font-size: 13px; color: var(--muted); }}
    .seclab {{ position: absolute; left: 18px; top: 50%; transform: translateY(-50%); z-index: 1;
      font-size: 13px; font-weight: 650; }}
    .row.ask .seclab {{ color: var(--no); }} .row.bid .seclab {{ color: var(--yes); }}
    .divider {{ display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 12px;
      padding: 5px 18px; }}
    .divider::before, .divider::after {{ content: ""; height: 1px; background: var(--line); }}
    .divider .lbl {{ font-size: 13px; font-weight: 680; color: {side_var}; white-space: nowrap; }}
    .divider .lbl small {{ color: var(--muted); font-weight: 500; margin-left: 8px; }}
    .empty {{ padding: 16px 18px; color: var(--muted); font-size: 12px; }}
  </style>
</head>
<body>
  <main class="shell">
    <div class="head">
      <div>
        <div class="title">{escape(title)}</div>
        <div class="subtitle">{escape(subtitle)}</div>
      </div>
      <div class="ticker">{escape(market_ticker)}</div>
    </div>
    <div class="colhead"><span>Price</span><span>Contracts</span><span>Total</span></div>
    <div class="book">
      {ask_html}
      <div class="divider"><span class="lbl">{side_label}<small>{last_label}</small></span></div>
      {bid_html}
    </div>
  </main>
</body>
</html>
""".strip()
