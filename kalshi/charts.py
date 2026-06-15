"""Plotly figure builders returning JSON-serialisable chart dicts."""

from __future__ import annotations

import json
from typing import Any

import plotly.graph_objects as go

from kalshi.formatting import timestamp_to_iso

_STATIC_CONFIG = {"displayModeBar": False, "doubleClick": False, "scrollZoom": False}


def _template(theme: str) -> str:
    return "plotly_dark" if theme == "dark" else "plotly_white"


def _safe_color(color: Any, fallback: str | None = "#7a8190") -> str | None:
    """Normalise a colour to #RRGGBB, dropping a leading alpha byte if present."""
    c = str(color or "").strip()
    if c.startswith("#") and len(c) == 9:
        c = "#" + c[3:]
    return c or fallback


def _to_chart(fig: go.Figure, static: bool = True) -> dict[str, Any]:
    chart = json.loads(fig.to_json())
    chart["config"] = (
        dict(_STATIC_CONFIG) if static else {"scrollZoom": True, "displayModeBar": False}
    )
    return chart


def empty_figure(message: str, theme: str) -> dict[str, Any]:
    fig = go.Figure()
    fig.add_annotation(
        text=message, xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False
    )
    fig.update_layout(template=_template(theme), margin=dict(l=36, r=24, t=20, b=36))
    return _to_chart(fig)


def _rangeselector(theme: str) -> dict[str, Any]:
    """Build 1H/3H/1D/ALL range buttons for a time axis."""
    dark = theme != "light"
    return dict(
        buttons=[
            dict(count=1, label="1H", step="hour", stepmode="backward"),
            dict(count=3, label="3H", step="hour", stepmode="backward"),
            dict(count=1, label="1D", step="day", stepmode="backward"),
            dict(step="all", label="ALL"),
        ],
        bgcolor="#1d1d22" if dark else "#eef1f5",
        bordercolor="#2a2a31" if dark else "#e2e6ec",
        borderwidth=1,
        activecolor="#21c891",
        font=dict(color="#f2f2f4" if dark else "#1f2328", size=11),
        x=1.0, xanchor="right", y=1.02, yanchor="bottom",
    )


def live_asset_price(
    points: list[dict[str, Any]],
    thresholds: list[dict[str, Any]],
    asset: str,
    theme: str,
) -> dict[str, Any]:
    """Build a live asset price line with market strikes as threshold lines."""
    xs = [timestamp_to_iso(int(point["t"]) // 1000) for point in points]
    ys = [point.get("v") for point in points]
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=xs, y=ys, mode="lines", name=asset or "Price",
            line=dict(color="#21c891", width=1.6),
            hovertemplate="%{x|%b %d, %H:%M}<br><b>%{y:.4f}</b><extra></extra>",
        )
    )
    for line in thresholds or []:
        value = line.get("value")
        if value is None:
            continue
        color = _safe_color(line.get("color"))
        fig.add_hline(
            y=value,
            line=dict(color=color, width=1, dash="dash"),
            annotation_text=line.get("label") or f"{value:g}",
            annotation_position="top left",
            annotation_font=dict(color=color, size=11),
        )
    fig.update_xaxes(rangeselector=_rangeselector(theme), rangeslider=dict(visible=False))
    fig.update_layout(
        template=_template(theme), hovermode="x unified", showlegend=False,
        margin=dict(l=54, r=20, t=40, b=36), uirevision="kalshi",
    )
    return _to_chart(fig, static=False)


def _wrap(text: str, width: int = 34) -> str:
    """Wrap a long axis label onto multiple lines at word boundaries."""
    words = str(text).split()
    lines: list[str] = []
    current = ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return "<br>".join(lines) or str(text)


def category_volume(
    rows: list[dict[str, Any]],
    metric: str,
    metric_label: str,
    theme: str,
) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda r: r.get(metric, 0))
    categories = [_wrap(r["category"]) for r in ordered]
    values = [r.get(metric, 0) for r in ordered]
    fig = go.Figure(
        go.Bar(
            x=values,
            y=categories,
            orientation="h",
            marker=dict(color="#21c891"),
            customdata=[
                [r.get("volume_24h", 0), r.get("open_interest", 0), int(r.get("market_count", 0))]
                for r in ordered
            ],
            hovertemplate=(
                "<b>%{y}</b><br>24h volume %{customdata[0]:,.0f}<br>"
                "Open interest %{customdata[1]:,.0f}<br>"
                "Markets %{customdata[2]:,}<extra></extra>"
            ),
        )
    )
    fig.update_layout(
        template=_template(theme),
        margin=dict(l=8, r=24, t=12, b=36),
        xaxis=dict(title=metric_label),
        yaxis=dict(automargin=True, tickfont=dict(size=11), ticklabelposition="outside"),
        bargap=0.18,
    )
    return _to_chart(fig)


_LINE_PALETTE = (
    "#5b9bff", "#2fbd6b", "#f5a623", "#f2566a",
    "#a872f0", "#22c1c3", "#ec6ba6", "#b3c63a",
)


def outcome_history(lines: list[dict[str, Any]], theme: str) -> dict[str, Any]:
    """Build a multi-line YES-probability history, one line per outcome."""
    fig = go.Figure()
    ymax = 0.0
    for index, line in enumerate(lines):
        ys = [point[1] for point in line["points"]]
        ymax = max(ymax, max(ys) if ys else 0.0)
        fig.add_trace(
            go.Scatter(
                x=[timestamp_to_iso(point[0]) for point in line["points"]],
                y=ys,
                mode="lines",
                name=str(line.get("name", "")),
                line=dict(color=_LINE_PALETTE[index % len(_LINE_PALETTE)], width=1.8),
                hovertemplate="<b>%{fullData.name}</b> %{y:.0f}%<extra></extra>",
            )
        )
    top = max(10.0, min(100.0, (int(ymax) // 10 + 1) * 10.0))
    fig.update_xaxes(rangeselector=_rangeselector(theme), rangeslider=dict(visible=False))
    fig.update_yaxes(
        title_text="YES probability", range=[0, top], ticksuffix="%", fixedrange=True
    )
    fig.update_layout(
        template=_template(theme),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.08, x=0),
        margin=dict(l=48, r=24, t=40, b=36), uirevision="kalshi",
    )
    return _to_chart(fig, static=False)
