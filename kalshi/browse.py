"""Iframe market browser following the OpenBB Iframe Widget Protocol: the page
declares its sub-widgets and toolbar params via `openbb-connect`, answers
`openbb-request` with `openbb-data`, and reloads on `openbb-params-update`. It
renders Kalshi event cards plus an AgGrid table view, with same-origin drilldown."""

from __future__ import annotations

import json
from html import escape
from typing import Any
from urllib.parse import quote

from kalshi.formatting import compact_number

_SERIES_IMAGE = "https://kalshi-public-docs.s3.amazonaws.com/series-images-webp/{ticker}.webp"
_AGGRID = "32.2.0"

_MANIFESTS = [
    {
        "widgetId": "browse_markets_events",
        "name": "Browse Markets — Events",
        "description": "Active Kalshi events with leading outcome, probability, markets, and volumes.",
        "category": "Kalshi",
        "dataType": "table",
    }
]

_BRIDGE_JS = r"""
(function () {
  var MANIFESTS = JSON.parse(document.getElementById("ob-manifests").textContent || "[]");
  var PARAM_DEFS = JSON.parse(document.getElementById("ob-params").textContent || "[]");
  var ROWS = JSON.parse(document.getElementById("ob-rowdata").textContent || "[]");
  var CFG = JSON.parse(document.getElementById("ob-cfg").textContent || "{}");
  var PARAM_KEYS = PARAM_DEFS.map(function (p) { return p.paramName; });

  var WIDGET_DATA = {};
  MANIFESTS.forEach(function (m) {
    WIDGET_DATA[m.widgetId] = { type: "openbb-data", widgetId: m.widgetId, dataType: m.dataType, data: ROWS };
  });

  function localizeTimes(root) {
    (root || document).querySelectorAll(".ts-t").forEach(function (el) {
      var ms = Number(el.getAttribute("data-ts")); if (!ms) return;
      el.textContent = new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    });
    (root || document).querySelectorAll(".ts-d").forEach(function (el) {
      var ms = Number(el.getAttribute("data-ts")); if (!ms) return;
      el.textContent = (el.getAttribute("data-prefix") || "")
        + new Date(ms).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
    });
  }
  localizeTimes();

  var target = window.top || window.parent;

  if (target !== window) {
    target.postMessage({ type: "openbb-connect", widgets: MANIFESTS, params: PARAM_DEFS }, "*");
  }

  function applyTheme(theme) {
    var t = theme === "light" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", t);
    CFG.theme = t;
    var grid = document.getElementById("ob-grid");
    if (grid) {
      grid.classList.remove("ag-theme-quartz", "ag-theme-quartz-dark");
      grid.classList.add(t === "light" ? "ag-theme-quartz" : "ag-theme-quartz-dark");
    }
  }
  function extractTheme(d) {
    if (!d || typeof d !== "object") return null;
    var raw = d.theme || d.colorScheme || d.appearance
      || (d.payload && (d.payload.theme || d.payload.colorScheme))
      || (d.data && d.data.theme)
      || (d.params && (typeof d.params.theme === "object" ? d.params.theme && d.params.theme.value : d.params.theme));
    if (typeof raw === "object" && raw) raw = raw.value;
    if (raw === "dark" || raw === "light") return raw;
    if (typeof d.type === "string" && d.type.toLowerCase().indexOf("theme") >= 0 && (d.value === "dark" || d.value === "light")) return d.value;
    return null;
  }
  applyTheme(CFG.theme || "dark");

  function applyParams(params) {
    if (!params) return;
    var pairs = [];
    if (Array.isArray(params)) {
      pairs = params.map(function (p) { return [p.paramName || p.name, p.value]; });
    } else if (typeof params === "object") {
      pairs = Object.keys(params).map(function (k) {
        var v = params[k];
        return [k, (v && typeof v === "object" && "value" in v) ? v.value : v];
      });
    }
    var qs = new URLSearchParams(window.location.search), changed = false;
    pairs.forEach(function (pair) {
      var k = pair[0], v = pair[1];
      if (!k || v == null || PARAM_KEYS.indexOf(k) < 0) return;
      var s = String(v);
      if (qs.get(k) !== s) { qs.set(k, s); changed = true; }
    });
    if (changed) window.location.search = qs.toString();
  }

  window.addEventListener("message", function (event) {
    var d = event.data;
    if (!d || typeof d !== "object") return;
    try { if (window.console && console.debug) console.debug("[kalshi-iframe] message", d.type, d); } catch (e) {}
    var th = extractTheme(d);
    if (th) applyTheme(th);
    if (!d.type) return;
    if (d.type === "openbb-request") {
      var widgetId = d.widgetId;
      if (widgetId === null || widgetId === undefined) {
        Object.keys(WIDGET_DATA).forEach(function (id) { target.postMessage(WIDGET_DATA[id], "*"); });
      } else if (WIDGET_DATA[widgetId]) {
        target.postMessage(WIDGET_DATA[widgetId], "*");
      }
    } else if (d.type === "openbb-params-update") {
      applyParams(d.params || d.payload || d.data || d.values || d);
    }
  });

  var gridApi = null;
  function intFmt(p) { return p.value == null ? "" : Number(p.value).toLocaleString(); }
  function pctFmt(p) { return p.value == null ? "" : Number(p.value).toFixed(0) + "%"; }
  function eventUrl(et) {
    var u = CFG.base + "/event_details?event_ticker=" + encodeURIComponent(et) + "&theme=" + encodeURIComponent(CFG.theme || "dark");
    if (CFG.back) u += "&back=" + encodeURIComponent(CFG.back);
    return u;
  }
  function buildGrid() {
    if (gridApi || !window.agGrid) return;
    gridApi = agGrid.createGrid(document.getElementById("ob-grid"), {
      rowData: ROWS,
      defaultColDef: { sortable: true, filter: true, resizable: true },
      columnDefs: [
        { headerName: "Event", field: "title", flex: 2, minWidth: 260, pinned: "left" },
        { headerName: "Category", field: "category", width: 140 },
        { headerName: "Tags", field: "tags", width: 170 },
        { headerName: "Leading", field: "leading_outcome", width: 160 },
        { headerName: "Leading %", field: "leading_pct", width: 110, type: "numericColumn", valueFormatter: pctFmt },
        { headerName: "Markets", field: "market_count", width: 100, type: "numericColumn", valueFormatter: intFmt },
        { headerName: "24h Vol", field: "volume_24h", width: 120, type: "numericColumn", valueFormatter: intFmt },
        { headerName: "Total Vol", field: "volume_total", width: 120, type: "numericColumn", valueFormatter: intFmt },
        { headerName: "OI", field: "open_interest", width: 110, type: "numericColumn", valueFormatter: intFmt },
        { headerName: "Closes", field: "close_time", width: 160 },
        { headerName: "Series", field: "series_ticker", width: 140 }
      ],
      onRowClicked: function (e) { if (e.data && e.data.event_ticker) window.location.href = eventUrl(e.data.event_ticker); }
    });
  }
  function setView(view) {
    var cards = document.getElementById("ob-cards"), grid = document.getElementById("ob-grid");
    var bc = document.getElementById("ob-view-cards"), bt = document.getElementById("ob-view-table"), csv = document.getElementById("ob-csv");
    if (view === "table") {
      buildGrid();
      cards.classList.add("ob-hidden"); grid.classList.remove("ob-hidden");
      bt.classList.add("active"); bc.classList.remove("active"); csv.classList.remove("ob-hidden");
    } else {
      grid.classList.add("ob-hidden"); cards.classList.remove("ob-hidden");
      bc.classList.add("active"); bt.classList.remove("active"); csv.classList.add("ob-hidden");
    }
  }
  document.getElementById("ob-view-cards").addEventListener("click", function () { setView("cards"); });
  document.getElementById("ob-view-table").addEventListener("click", function () { setView("table"); });
  document.getElementById("ob-csv").addEventListener("click", function () { if (gridApi) gridApi.exportDataAsCsv({ fileName: "kalshi_markets.csv" }); });
})();
"""


def _payout(probability_pct: float) -> str:
    return f"{100 / probability_pct:.2f}x" if probability_pct > 0 else "—"


def _outcome_html(outcome: dict[str, Any]) -> str:
    prob = max(0.0, min(100.0, float(outcome.get("probability_pct") or 0)))
    color = outcome.get("color") or "#8b8b94"
    name = escape(str(outcome.get("name") or ""))
    image = outcome.get("image_url") or ""
    avatar = (
        f"<span class=\"oc-img\" style=\"background-image:url('{escape(image)}')\"></span>"
        if image else ""
    )
    return f"""
    <div class="outcome">
      {avatar}
      <div class="oc-name"><span style="border-color:{escape(color)}">{name}</span></div>
      <div class="oc-payout">{_payout(prob)}</div>
      <div class="oc-pill" style="border-color:{escape(color)}">{prob:.0f}%</div>
    </div>
    """


def _event_html(event: dict[str, Any], theme: str, base_url: str, back_qs: str) -> str:
    series = str(event.get("series_ticker") or "")
    thumb = (
        f"<span class=\"ev-img\" style=\"background-image:url('{escape(_SERIES_IMAGE.format(ticker=quote(series)))}')\"></span>"
        if series else '<span class="ev-img"></span>'
    )
    outcomes = "".join(_outcome_html(o) for o in event.get("outcomes", []))
    more = event.get("market_count", 0) - len(event.get("outcomes", []))
    more_html = f'<div class="more">+{more} more</div>' if more > 0 else ""
    tags = ", ".join(str(t) for t in (event.get("tags") or [])[:3])
    live_html = ""
    if event.get("live"):
        since = ""
        if event.get("open_ts"):
            since = f' <time class="ts-t" data-ts="{int(event["open_ts"] * 1000)}"></time>'
        live_html = f'<span class="live"><span class="live-dot"></span>LIVE</span>{since}'
    close_ts = event.get("close_ts")
    if close_ts:
        close_html = f'<time class="ts-d" data-ts="{int(close_ts * 1000)}" data-prefix="closes "></time>'
    elif event.get("close_time"):
        close_html = f'closes {escape(str(event.get("close_time"))[:16])}'
    else:
        close_html = ""
    sub = " · ".join(
        part for part in (
            live_html,
            escape(str(event.get("category") or "")),
            escape(tags),
            close_html,
        ) if part
    )
    href = f'{base_url}/event_details?event_ticker={quote(str(event.get("event_ticker") or ""))}&theme={quote(theme)}'
    if back_qs:
        href += f"&back={quote(back_qs, safe='')}"
    return f"""
    <a class="event" href="{href}">
      <header>
        {thumb}
        <div class="ev-head">
          <div class="title">{escape(str(event.get("title") or event.get("event_ticker") or ""))} <span class="open">↗</span></div>
          <div class="sub">{sub}</div>
        </div>
      </header>
      <div class="outcomes">{outcomes}{more_html}</div>
      <footer>
        <span>${compact_number(event.get("volume_total"))} vol</span>
        <span>{event.get("market_count", 0)} markets</span>
      </footer>
    </a>
    """


def render_browse(
    events: list[dict[str, Any]],
    *,
    rows: list[dict[str, Any]],
    param_defs: list[dict[str, Any]],
    total: int,
    search: str,
    theme: str,
    base_url: str = "",
    back_qs: str = "",
) -> str:
    is_light = theme == "light"
    grid_theme = "ag-theme-quartz" if is_light else "ag-theme-quartz-dark"
    if events:
        body = "".join(_event_html(e, theme, base_url, back_qs) for e in events)
    else:
        hint = f' for "{escape(search)}"' if search else ""
        body = f'<div class="empty">No markets found{hint}.</div>'
    caption = f'{len(events)} of {total} active events' + (f' · "{escape(search)}"' if search else "")

    def _emb(value: Any) -> str:
        return json.dumps(value).replace("</", "<\\/")

    rowdata = _emb(rows)
    manifests = _emb(_MANIFESTS)
    params = _emb(param_defs)
    cfg = _emb({"base": base_url, "theme": theme, "back": back_qs})

    return f"""
<!doctype html>
<html data-theme="{'light' if is_light else 'dark'}">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/ag-grid-community@{_AGGRID}/styles/ag-grid.css" />
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/ag-grid-community@{_AGGRID}/styles/ag-theme-quartz.css" />
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0f0f12; --card: #16161a; --card-h: #1d1d22; --text: #f2f2f4;
      --muted: #9a9aa4; --line: #2a2a31; --track: #26262d;
    }}
    :root[data-theme="light"] {{
      color-scheme: light;
      --bg: #ffffff; --card: #ffffff; --card-h: #f4f6f9; --text: #1f2328;
      --muted: #667085; --line: #e2e6ec; --track: #e6e9ef;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ height: 100%; }}
    body {{
      margin: 0; height: 100vh; overflow: hidden;
      background: var(--bg); color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, sans-serif;
    }}
    .wrap {{ height: 100vh; display: flex; flex-direction: column; }}
    .toolbar {{ flex: 0 0 auto; display: flex; align-items: center; justify-content: space-between;
      gap: 12px; padding: 8px 14px; border-bottom: 1px solid var(--line); }}
    .caption {{ font-size: 11px; color: var(--muted); }}
    .views {{ display: flex; gap: 6px; }}
    .views button {{ background: var(--card); color: var(--muted); border: 1px solid var(--line);
      border-radius: 7px; padding: 5px 12px; font-size: 12px; cursor: pointer; }}
    .views button.active {{ color: var(--text); border-color: var(--muted); background: var(--card-h); }}
    .content {{ flex: 1 1 auto; min-height: 0; position: relative; }}
    #ob-cards {{ position: absolute; inset: 0; overflow: auto; padding: 12px; display: grid;
      align-content: start; gap: 12px; grid-template-columns: repeat(auto-fill, minmax(440px, 1fr)); }}
    #ob-grid {{ position: absolute; inset: 0; }}
    .ob-hidden {{ display: none !important; }}
    a.event {{
      background: var(--card); border: 1px solid var(--line); border-radius: 12px;
      padding: 16px; display: flex; flex-direction: column; gap: 14px;
      text-decoration: none; color: inherit; transition: background .12s, border-color .12s;
    }}
    a.event:hover {{ background: var(--card-h); border-color: var(--muted); }}
    .event header {{ display: flex; align-items: center; gap: 12px; min-width: 0; }}
    .ev-img {{ flex: 0 0 auto; width: 38px; height: 38px; border-radius: 9px; background-color: var(--track);
      background-size: cover; background-position: center; background-repeat: no-repeat; }}
    .ev-head {{ min-width: 0; }}
    .title {{ font-size: 15px; font-weight: 650; line-height: 1.25; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .open {{ color: var(--muted); font-size: 11px; }}
    .sub {{ margin-top: 3px; font-size: 12px; color: var(--muted); }}
    .live {{ color: #eb5757; font-weight: 700; letter-spacing: .03em; }}
    .live-dot {{ display: inline-block; width: 7px; height: 7px; border-radius: 50%;
      background: #eb5757; margin-right: 4px; vertical-align: middle; }}
    .outcomes {{ display: grid; gap: 11px; }}
    .outcome {{ display: flex; align-items: center; gap: 10px; font-size: 13px; }}
    .oc-img {{ flex: 0 0 auto; width: 22px; height: 22px; border-radius: 50%; background-color: var(--track);
      background-size: cover; background-position: center; background-repeat: no-repeat; }}
    .oc-name {{ flex: 1; min-width: 0; }}
    .oc-name span {{ display: inline-block; max-width: 100%; overflow: hidden; text-overflow: ellipsis;
      white-space: nowrap; vertical-align: bottom; font-weight: 550; border-bottom: 2px solid; padding-bottom: 2px; }}
    .oc-payout {{ flex: 0 0 auto; color: var(--muted); font-variant-numeric: tabular-nums; }}
    .oc-pill {{ flex: 0 0 auto; min-width: 50px; text-align: center; border: 1.5px solid var(--line);
      border-radius: 999px; padding: 5px 11px; font-weight: 700; font-variant-numeric: tabular-nums; }}
    .more {{ font-size: 12px; color: var(--muted); }}
    footer {{ display: flex; justify-content: space-between; border-top: 1px solid var(--line);
      padding-top: 12px; margin-top: auto; font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }}
    .empty {{ padding: 40px 16px; text-align: center; color: var(--muted); font-size: 13px; grid-column: 1 / -1; }}
  </style>
</head>
<body>
  <main class="wrap">
    <div class="toolbar">
      <div class="caption">{caption}</div>
      <div class="views">
        <button id="ob-view-cards" class="active" type="button">Cards</button>
        <button id="ob-view-table" type="button">Table</button>
        <button id="ob-csv" class="ob-hidden" type="button">Export CSV</button>
      </div>
    </div>
    <div class="content">
      <div id="ob-cards">{body}</div>
      <div id="ob-grid" class="{grid_theme} ob-hidden"></div>
    </div>
  </main>
  <script id="ob-manifests" type="application/json">{manifests}</script>
  <script id="ob-params" type="application/json">{params}</script>
  <script id="ob-rowdata" type="application/json">{rowdata}</script>
  <script id="ob-cfg" type="application/json">{cfg}</script>
  <script src="https://cdn.jsdelivr.net/npm/ag-grid-community@{_AGGRID}/dist/ag-grid-community.min.js"></script>
  <script>{_BRIDGE_JS}</script>
</body>
</html>
""".strip()
