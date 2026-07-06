"""HTML panel rendering a market's full resolution rules and metadata."""

from __future__ import annotations

import json
from html import escape
from typing import Any

from kalshi.formatting import compact_number, iso_to_display, pct, to_float
from kalshi.pdfviewer import doc_section

_BRIDGE_JS = """<script>
(function () {
  var PARAM_DEFS = __PARAM_DEFS__;
  var PARAM_KEYS = PARAM_DEFS.map(function (p) { return p.paramName; });
  var target = window.top || window.parent;
  if (target !== window) {
    target.postMessage({ type: "openbb-connect", widgets: [], params: PARAM_DEFS }, "*");
  }
  function extractTheme(d) {
    if (!d || typeof d !== "object") return null;
    var raw = d.theme || d.colorScheme || d.appearance
      || (d.payload && (d.payload.theme || d.payload.colorScheme))
      || (d.params && (typeof d.params.theme === "object" ? d.params.theme && d.params.theme.value : d.params.theme));
    if (typeof raw === "object" && raw) raw = raw.value;
    return (raw === "dark" || raw === "light") ? raw : null;
  }
  function marketKeyEvent(marketKey) {
    var parts = String(marketKey || "").split("|");
    return parts.length >= 3 ? parts[2].trim() : "";
  }
  function normalizeMarketKey(eventTicker, marketKey) {
    var mk = String(marketKey || "");
    var mkEvent = marketKeyEvent(mk);
    if (eventTicker && mk && mkEvent && mkEvent !== String(eventTicker)) return "";
    return mk;
  }
  function emitWidgetParams(params) {
    if (target === window || !params) return;
    target.postMessage({ type: "openbb:widget-params:update", params: params }, "*");
  }
  window.addEventListener("message", function (event) {
    var d = event.data;
    if (!d || typeof d !== "object") return;
    var incoming = {};
    var th = extractTheme(d);
    if (th) incoming.theme = th;
    if (d.type === "openbb-params-update") {
      var p = d.params || d.payload || d.data || d.values;
      if (Array.isArray(p)) p.forEach(function (x) { incoming[x.paramName || x.name] = x.value; });
      else if (p && typeof p === "object") Object.keys(p).forEach(function (k) {
        var v = p[k];
        incoming[k] = (v && typeof v === "object" && "value" in v) ? v.value : v;
      });
    }
    var qs = new URLSearchParams(window.location.search), changed = false;
    // When the event changes, a market_key from the previous event is stale.
    // Clear it so the reload resolves the new event's default market, and tell
    // the workspace so the shared Market selector resets too.
    var eventTicker = String(incoming.event_ticker != null ? incoming.event_ticker : (qs.get("event_ticker") || ""));
    var marketKey = String(incoming.market_key != null ? incoming.market_key : (qs.get("market_key") || ""));
    var normalizedMarketKey = normalizeMarketKey(eventTicker, marketKey);
    if (marketKey && normalizedMarketKey !== marketKey) {
      incoming.market_key = "";
      emitWidgetParams({ event_ticker: eventTicker, market_key: "" });
    }
    Object.keys(incoming).forEach(function (k) {
      if (k !== "theme" && PARAM_KEYS.indexOf(k) < 0) return;
      var v = incoming[k];
      if (v == null) return;
      if (qs.get(k) !== String(v)) { qs.set(k, String(v)); changed = true; }
    });
    if (changed) window.location.search = qs.toString();
  });
})();
</script>"""

_SYNC_JS = """<script>
(function () {
  var SYNC = __SYNC__;
  if (!SYNC || typeof EventSource === "undefined") return;
  new EventSource(SYNC).onmessage = function (e) {
    var mk = e.data;
    var qs = new URLSearchParams(window.location.search);
    // Compare against the market_key already in the URL, not the rendered
    // market. A stale selection resolves to the event default, which would
    // never equal the pushed key and reload forever.
    if (mk && mk !== (qs.get("market_key") || "")) {
      qs.set("market_key", mk);
      window.location.search = qs.toString();
    }
  };
})();
</script>"""


def _row(label: str, value: str) -> str:
    if not value:
        return ""
    return f'<div class="row"><span>{escape(label)}</span><strong>{value}</strong></div>'


def _section(title: str, body: str) -> str:
    if not body.strip():
        return ""
    return f'<section><h3>{escape(title)}</h3>{body}</section>'


def _timeline(market: dict[str, Any]) -> str:
    rows = [
        _row("Opened", escape(iso_to_display(market.get("open_time")))),
        _row("Closes (trading)", escape(iso_to_display(market.get("close_time")))),
        _row(
            "Expected settlement",
            escape(iso_to_display(market.get("expected_expiration_time") or market.get("expiration_time"))),
        ),
    ]
    timer = to_float(market.get("settlement_timer_seconds"))
    if timer:
        rows.append(_row("Settlement timer", f"{timer / 60:.0f} min"))
    if market.get("can_close_early"):
        rows.append(_row("Early close", "Possible"))
    return "".join(rows)


def _settlement_sources(series: dict[str, Any]) -> str:
    sources = series.get("settlement_sources") or []
    items = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        name = escape(str(source.get("name") or ""))
        url = source.get("url") or ""
        if not name:
            continue
        if url:
            items.append(f'<li><a href="{escape(url)}" target="_blank" rel="noopener">{name}</a></li>')
        else:
            items.append(f"<li>{name}</li>")
    return f"<ul>{''.join(items)}</ul>" if items else ""


def _prohibitions(series: dict[str, Any]) -> str:
    items = [
        f"<li>{escape(str(p))}</li>"
        for p in (series.get("additional_prohibitions") or [])
        if isinstance(p, str) and p
    ]
    return f"<ul>{''.join(items)}</ul>" if items else ""


def render_market_rules(
    *,
    market: dict[str, Any],
    event: dict[str, Any],
    series: dict[str, Any],
    market_ticker: str,
    series_ticker: str,
    theme: str,
    doc_base: str = "",
    param_defs: list[dict[str, Any]] | None = None,
    sync_url: str = "",
) -> str:
    is_light = theme == "light"
    bridge = _BRIDGE_JS.replace("__PARAM_DEFS__", json.dumps(param_defs or []))
    poller = _SYNC_JS.replace("__SYNC__", json.dumps(sync_url)) if sync_url else ""
    title = market.get("title") or market_ticker
    outcome = market.get("yes_sub_title") or market.get("subtitle") or ""
    status = (market.get("status") or "").title()
    yes = pct(market.get("last_price_dollars"))
    yes_bid = pct(market.get("yes_bid_dollars"))
    yes_ask = pct(market.get("yes_ask_dollars"))

    rules_primary = escape(str(market.get("rules_primary") or "")).replace("\n", "<br/>")
    rules_secondary = escape(str(market.get("rules_secondary") or "")).replace("\n", "<br/>")
    resolution = ""
    if rules_primary:
        resolution += f'<p class="lead">{rules_primary}</p>'
    if rules_secondary:
        resolution += f"<p>{rules_secondary}</p>"
    if event.get("mutually_exclusive"):
        resolution += '<p class="note">Note: this event is mutually exclusive.</p>'
    if not resolution:
        resolution = "<p>Resolution rules were not included in the API response.</p>"

    meta_rows = "".join(
        [
            _row("Outcome", escape(outcome)),
            _row("Category", escape(str(event.get("category") or ""))),
            _row("Status", escape(status)),
            _row("YES probability", f"{yes:.1f}%"),
            _row("Bid / Ask", f"{yes_bid:.1f}% / {yes_ask:.1f}%"),
            _row("24h volume", f"{compact_number(market.get('volume_24h_fp'))} contracts"),
            _row("Open interest", f"{compact_number(market.get('open_interest_fp'))} contracts"),
            _row("Frequency", escape(str(series.get("frequency") or ""))),
            _row("Fee", f"{escape(str(series.get('fee_type') or ''))} × {to_float(series.get('fee_multiplier')):g}"),
            _row("Market", escape(market_ticker)),
            _row("Series", escape(series_ticker)),
        ]
    )

    sections = "".join(
        [
            _section("Resolution", resolution),
            _section("Timeline & payout", _timeline(market)),
            _section("Settlement sources", _settlement_sources(series)),
            _section("Trading restrictions", _prohibitions(series)),
        ]
    )

    return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root {{
      color-scheme: {"light" if is_light else "dark"};
      --bg: {"#ffffff" if is_light else "#0f0f12"};
      --card: {"#f7f8fa" if is_light else "#17171b"};
      --text: {"#1f2328" if is_light else "#f2f2f4"};
      --muted: {"#667085" if is_light else "#9a9aa4"};
      --line: {"#e2e6ec" if is_light else "#2a2a31"};
      --accent: #21c891;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ height: 100%; }}
    body {{ margin: 0; min-height: 100vh; overflow: hidden; background: var(--bg); color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, sans-serif;
      font-size: 13px; line-height: 1.5; }}
    .scroll {{ height: 100vh; overflow: auto; }}
    .wrap {{ padding: 12px 18px; }}
    .head {{ display: flex; justify-content: space-between; align-items: flex-start;
      gap: 20px; margin-bottom: 12px; }}
    .head .doc-links {{ margin-top: 0; flex-shrink: 0; }}
    h2 {{ font-size: 17px; margin: 0 0 2px; }}
    .sub {{ color: var(--accent); font-weight: 600; }}
    h3 {{ font-size: 13px; text-transform: uppercase; letter-spacing: .05em;
      color: var(--muted); margin: 12px 0 8px; padding-bottom: 6px; border-bottom: 1px solid var(--line); }}
    p {{ margin: 0 0 10px; }}
    .lead {{ font-weight: 550; }}
    .note {{ color: var(--muted); font-style: italic; }}
    .meta {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px;
      padding: 6px 14px; display: grid; grid-template-columns: 1fr 1fr; gap: 0 28px; }}
    .row {{ display: flex; justify-content: space-between; gap: 16px; padding: 6px 0;
      border-bottom: 1px solid var(--line); font-variant-numeric: tabular-nums; }}
    .row:last-child {{ border-bottom: none; }}
    .grid {{ display: grid; grid-template-columns: minmax(400px, 1fr) 1.6fr; gap: 0 30px;
      align-items: start; }}
    .rest {{ columns: 320px; column-gap: 30px; }}
    .rest > section {{ break-inside: avoid; margin-bottom: 14px; }}
    .grid h3 {{ margin-top: 0; }}
    .row span {{ color: var(--muted); }}
    .row strong {{ text-align: right; font-weight: 600; word-break: break-word; }}
    ul {{ margin: 4px 0; padding-left: 18px; }}
    li {{ margin: 4px 0; color: var(--text); }}
    a {{ color: var(--accent); }}
    .links {{ display: flex; gap: 10px; margin-top: 12px; flex-wrap: wrap; }}
    .btn {{ border: 1px solid var(--line); border-radius: 8px; padding: 7px 14px;
      text-decoration: none; font-weight: 600; background: none; color: var(--text);
      font: inherit; cursor: pointer; }}
  </style>
</head>
<body>
  <div class="scroll">
  <main class="wrap">
    <div class="head">
      <div>
        <h2>{escape(title)}</h2>
        <div class="sub">{escape(outcome)}</div>
      </div>
      {doc_section(series, doc_base)}
    </div>
    <div class="grid">
      <section><h3>Details</h3><div class="meta">{meta_rows}</div></section>
      <div class="rest">{sections}</div>
    </div>
  </main>
  </div>
  {bridge}{poller}
</body>
</html>
""".strip()
