"""Full HTML event-details page opened from a Browse Markets card."""

from __future__ import annotations

import json
from html import escape
from typing import Any

from kalshi.formatting import compact_number, iso_to_display, parse_iso_time, pct, quantity, to_float
from kalshi.pdfviewer import doc_section


def _time_el(iso: Any, prefix: str = "") -> str:
    """A <time> the page localizes client-side, with the server format as fallback."""
    display = escape(iso_to_display(iso))
    dt = parse_iso_time(iso) if iso else None
    if not dt:
        return f"{prefix}{display}" if display else ""
    return (
        f'<time class="ts-d" data-ts="{int(dt.timestamp() * 1000)}" '
        f'data-prefix="{escape(prefix)}">{prefix}{display}</time>'
    )


def _payout(probability_pct: float) -> str:
    return f"{100 / probability_pct:.2f}x" if probability_pct > 0 else "—"


def _outcome_row(market: dict[str, Any], images: dict[str, dict[str, str]]) -> dict[str, Any]:
    ticker = market.get("ticker", "")
    info = images.get(ticker, {})
    return {
        "name": market.get("yes_sub_title") or market.get("subtitle") or ticker,
        "probability_pct": pct(market.get("last_price_dollars")),
        "yes_bid_pct": pct(market.get("yes_bid_dollars")),
        "yes_ask_pct": pct(market.get("yes_ask_dollars")),
        "volume_total": quantity(market.get("volume_fp")),
        "volume_24h": quantity(market.get("volume_24h_fp")),
        "open_interest": quantity(market.get("open_interest_fp")),
        "image_url": info.get("image_url", ""),
        "color": info.get("color", ""),
    }


def _outcome_html(outcome: dict[str, Any]) -> str:
    prob = max(0.0, min(100.0, outcome["probability_pct"]))
    width = max(2.0, prob)
    color = outcome.get("color") or ("#27ae60" if prob >= 50 else ("#f2994a" if prob >= 20 else "#8b8b94"))
    image = outcome.get("image_url") or ""
    thumb = (
        f"<span class=\"thumb\" style=\"background-image:url('{escape(image)}')\"></span>"
        if image else f'<span class="dot" style="background:{escape(color)}"></span>'
    )
    return f"""
    <tr>
      <td class="oc">{thumb}<span class="oc-name">{escape(str(outcome["name"]))}</span></td>
      <td class="bar"><div class="track"><span style="width:{width:.1f}%;background:{escape(color)}"></span></div></td>
      <td class="num strong">{prob:.1f}%</td>
      <td class="num muted">{outcome["yes_bid_pct"]:.0f} / {outcome["yes_ask_pct"]:.0f}</td>
      <td class="num muted">{_payout(prob)}</td>
      <td class="num">{compact_number(outcome["volume_total"])}</td>
      <td class="num">{compact_number(outcome["open_interest"])}</td>
    </tr>
    """


def _section(title: str, body: str) -> str:
    return f'<section><h3>{escape(title)}</h3>{body}</section>' if body.strip() else ""


def _list(items: list[Any], link_key: bool = False) -> str:
    out = []
    for item in items:
        if link_key and isinstance(item, dict):
            name = escape(str(item.get("name") or ""))
            url = item.get("url")
            if not name:
                continue
            out.append(
                f'<li><a href="{escape(str(url))}" target="_blank" rel="noopener">{name}</a></li>'
                if url else f"<li>{name}</li>"
            )
        elif isinstance(item, str) and item:
            out.append(f"<li>{escape(item)}</li>")
    return f"<ul>{''.join(out)}</ul>" if out else ""


def render_event_page(
    *,
    event: dict[str, Any],
    markets: list[dict[str, Any]],
    series: dict[str, Any],
    images: dict[str, dict[str, str]],
    event_ticker: str,
    series_ticker: str,
    theme: str,
    back_url: str = "",
    history_figure: dict[str, Any] | None = None,
    poll_url: str = "",
    doc_base: str = "",
) -> str:
    is_light = theme == "light"
    chart_section = (
        '<section><h3>Price history</h3>'
        '<div id="ev-chart" class="chart" style="height:340px;padding:4px"></div></section>'
        if history_figure else ""
    )
    figure_json = json.dumps(history_figure or {}).replace("</", "<\\/")
    poll_json = json.dumps(poll_url)
    outcomes = sorted(
        (_outcome_row(m, images) for m in markets),
        key=lambda o: to_float(o["volume_total"]),
        reverse=True,
    )
    total_volume = sum(o["volume_total"] for o in outcomes)
    total_oi = sum(o["open_interest"] for o in outcomes)
    rep = markets[0] if markets else {}

    meta = " · ".join(
        part for part in (
            escape(str(event.get("category") or "")),
            f"{len(outcomes)} markets",
            f"${compact_number(total_volume)} volume",
            f"{compact_number(total_oi)} open interest",
            (_time_el(rep.get("close_time"), "closes ") if rep.get("close_time") else ""),
        ) if part
    )

    rows = "".join(_outcome_html(o) for o in outcomes) or '<tr><td colspan="7" class="muted">No markets.</td></tr>'

    resolution = ""
    if rep.get("rules_primary"):
        resolution += f'<p class="lead">{escape(str(rep["rules_primary"]))}</p>'
    if rep.get("rules_secondary"):
        resolution += f'<p>{escape(str(rep["rules_secondary"]))}</p>'
    if event.get("mutually_exclusive"):
        resolution += '<p class="note">Note: this event is mutually exclusive.</p>'

    timeline = "".join(
        f'<div class="row"><span>{escape(label)}</span><strong>{_time_el(rep.get(key))}</strong></div>'
        for label, key in (("Opened", "open_time"), ("Closes (trading)", "close_time"),
                           ("Expected settlement", "expected_expiration_time"))
        if rep.get(key)
    )

    links = doc_section(series, doc_base)

    return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>{escape(str(event.get("title") or event_ticker))}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    :root {{
      color-scheme: {"light" if is_light else "dark"};
      --bg: {"#ffffff" if is_light else "#0f0f12"};
      --card: {"#f7f8fa" if is_light else "#17171b"};
      --text: {"#1f2328" if is_light else "#f2f2f4"};
      --muted: {"#667085" if is_light else "#9a9aa4"};
      --line: {"#e2e6ec" if is_light else "#2a2a31"};
      --track: {"#e6e9ef" if is_light else "#26262d"};
      --accent: #21c891;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ height: 100%; }}
    body {{ margin: 0; min-height: 100vh; overflow: hidden; background: var(--bg); color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, sans-serif; font-size: 13px; }}
    .scroll {{ height: 100vh; overflow: auto; }}
    .wrap {{ width: 100%; margin: 0; padding: 16px 28px 60px; }}
    .back {{ display: inline-block; margin-bottom: 14px; color: var(--accent);
      text-decoration: none; font-weight: 600; }}
    .back:hover {{ text-decoration: underline; }}
    .legend {{ display: flex; flex-wrap: wrap; gap: 16px; margin-bottom: 8px; font-size: 12px; }}
    .lg {{ color: var(--muted); }}
    .lg b {{ color: var(--text); }}
    .lg-dot {{ display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 5px; vertical-align: middle; }}
    .chart {{ width: 100%; border: 1px solid var(--line); border-radius: 10px; padding: 4px; overflow: hidden; }}
    .live {{ color: #eb5757; font-weight: 700; }}
    .live-dot {{ display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: #eb5757; margin-right: 5px; vertical-align: middle; }}
    h1 {{ font-size: 24px; margin: 0 0 6px; }}
    .meta {{ color: var(--muted); font-size: 13px; margin-bottom: 20px; }}
    h3 {{ font-size: 13px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted);
      margin: 26px 0 10px; padding-bottom: 6px; border-bottom: 1px solid var(--line); }}
    table {{ width: 100%; border-collapse: collapse; }}
    th {{ text-align: right; font-size: 11px; color: var(--muted); text-transform: uppercase;
      letter-spacing: .04em; padding: 6px 8px; border-bottom: 1px solid var(--line); }}
    th.l {{ text-align: left; }}
    td {{ padding: 9px 8px; border-bottom: 1px solid var(--line); font-variant-numeric: tabular-nums; }}
    td.oc {{ font-weight: 600; display: flex; align-items: center; gap: 10px; }}
    .oc-name {{ white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .thumb {{ width: 24px; height: 24px; border-radius: 50%; flex: 0 0 auto; background-color: var(--track);
      background-size: cover; background-position: center; background-repeat: no-repeat; }}
    .dot {{ width: 9px; height: 9px; border-radius: 50%; flex: 0 0 auto; }}
    td.num {{ text-align: right; white-space: nowrap; }}
    td.strong {{ font-weight: 700; }}
    td.muted {{ color: var(--muted); }}
    td.bar {{ width: 34%; }}
    .track {{ width: 100%; height: 8px; background: var(--track); border-radius: 999px; overflow: hidden; }}
    .track span {{ display: block; height: 100%; border-radius: 999px; }}
    p {{ line-height: 1.6; margin: 0 0 12px; }}
    .lead {{ font-weight: 550; }}
    .note {{ color: var(--muted); font-style: italic; }}
    .row {{ display: flex; justify-content: space-between; max-width: 380px; padding: 7px 0; border-bottom: 1px solid var(--line); }}
    .row span {{ color: var(--muted); }}
    ul {{ padding-left: 18px; }}
    li {{ margin: 5px 0; }}
    a {{ color: var(--accent); }}
    .btns {{ display: flex; gap: 10px; margin-top: 14px; flex-wrap: wrap; }}
    .btn {{ border: 1px solid var(--line); border-radius: 8px; padding: 8px 16px; text-decoration: none; font-weight: 600; }}
  </style>
</head>
<body>
  <div class="scroll">
  <main class="wrap">
    <a class="back" href="{escape(back_url)}">← Back to markets</a>
    <h1>{escape(str(event.get("title") or event_ticker))}</h1>
    <div class="meta">{meta}{(' · ' + escape(str(event.get('sub_title')))) if event.get('sub_title') else ''}</div>
    {chart_section}
    <section>
      <h3>Markets</h3>
      <table>
        <thead><tr>
          <th class="l">Outcome</th><th></th><th>YES</th><th>Bid / Ask</th>
          <th>Payout</th><th>Volume</th><th>OI</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </section>
    {_section("Resolution", resolution)}
    {_section("Timeline & payout", timeline)}
    {_section("Settlement sources", _list(series.get("settlement_sources") or [], link_key=True))}
    {_section("Trading restrictions", _list(series.get("additional_prohibitions") or []))}
    {links}
    <div class="meta" style="margin-top:24px">Event <code>{escape(event_ticker)}</code> · Series <code>{escape(series_ticker)}</code></div>
  </main>
  </div>
  <script id="ev-fig" type="application/json">{figure_json}</script>
  <script>
  (function () {{
    var backLink = document.querySelector("a.back");
    if (backLink) {{
      backLink.addEventListener("click", function () {{
        try {{ window.sessionStorage.removeItem("kalshi-browse-drill"); }} catch (e) {{}}
      }});
    }}
    document.querySelectorAll(".ts-d").forEach(function (el) {{
      var ms = Number(el.getAttribute("data-ts")); if (!ms) return;
      el.textContent = (el.getAttribute("data-prefix") || "")
        + new Date(ms).toLocaleString([], {{ month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }});
    }});
    function localize(fig) {{
      if (fig && fig.data) fig.data.forEach(function (tr) {{
        if (tr.x && tr.x.length) tr.x = tr.x.map(function (v) {{
          var d = new Date(v);
          return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 23);
        }});
      }});
      return fig;
    }}
    var el = document.getElementById("ev-chart");
    var CFG = {{ responsive: true, displayModeBar: false, scrollZoom: true }};
    if (el && window.Plotly) {{
      var fig = localize(JSON.parse(document.getElementById("ev-fig").textContent || "{{}}"));
      if (fig.data) Plotly.newPlot(el, fig.data, fig.layout || {{}}, CFG);
      var POLL = {poll_json};
      if (POLL) setInterval(function () {{
        fetch(POLL, {{ cache: "no-store" }}).then(function (r) {{ return r.ok ? r.json() : null; }})
          .then(function (f) {{ if (f && f.data) {{ localize(f); Plotly.react(el, f.data, f.layout || {{}}, CFG); }} }})
          .catch(function () {{}});
      }}, 10000);
    }}
  }})();
  </script>
</body>
</html>
""".strip()
