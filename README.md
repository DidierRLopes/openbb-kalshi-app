# Kalshi Market Dashboard

OpenBB Workspace backend for public Kalshi prediction-market data.

A FastAPI backend that OpenBB Workspace registers as a custom data connector.
Workspace reads `widgets.json` for the widget definitions and `apps.json` for
the prebuilt **Kalshi Market Explorer** app layout.

Every parameter choice in the app (category → tag → series → event → market) is
derived from a single cached **taxonomy** built from two Kalshi endpoints:

- [`GET /series`](https://docs.kalshi.com/api-reference/market/get-series-list) —
  the full catalog of series, each with its category, tags, and traded volume.
- [`GET /search/tags_by_categories`](https://docs.kalshi.com/api-reference/search/get-tags-for-series-categories) —
  the canonical mapping of category → tags.

Because the choice lists come from this cache rather than from re-scanning live
endpoints, filtering by category and tag is fast, complete, and always
consistent with the data shown in the tables.

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 7779
```

Then add the backend URL in OpenBB Workspace → **Settings → Data Connectors →
Add Custom Backend**:

```text
http://localhost:7779
```

Workspace loads `widgets.json` and `apps.json` from that URL automatically.
Open **Apps** and launch **Kalshi Market Explorer**.

## Project Layout

The backend is a small package instead of one monolithic file:

```text
main.py                  # thin entry point: exposes `app` for `uvicorn main:app`
kalshi/
├── app.py               # application factory: wiring + CORS + routers
├── config.py            # Settings loaded from the environment (.env optional)
├── client.py            # cached async httpx wrapper around the Kalshi API
├── taxonomy.py          # TaxonomyCache: categories/tags/series from 2 endpoints
├── service.py           # fetch + resolve logic with live, volume-based fallbacks
├── transforms.py        # raw Kalshi objects → flat widget rows
├── formatting.py        # pure value/format helpers + market_key codec
├── charts.py            # Plotly figure builders
├── ladder.py            # HTML orderbook-ladder widget
├── dependencies.py      # FastAPI accessors for the shared singletons
└── routers/
    ├── meta.py          # health, manifests, thumbnail, exchange status
    ├── options.py       # the category → tag → series → event → market cascade
    ├── discover.py      # 24h volume by category, top markets
    ├── series.py        # series catalog table
    ├── events.py        # event discovery, metrics, brief, markets
    └── markets.py       # probability, rules, orderbook, trades, price history
```

A `MarketStatsCache` (`kalshi/stats.py`) pages a bounded active-market slice in
the background (multivariate markets excluded), maps each market to a category
via its series prefix, and keeps the active subset in memory. The Discover
widgets serve from that snapshot when available; stale data remains usable while
a refresh runs in the background.

## How the Cascade Works

Each dropdown is populated by an options endpoint that depends only on the
choice above it, and every data widget falls back to the most active live
instrument when nothing is selected (so the dashboard never depends on a
hardcoded ticker that goes stale when a market settles):

| Choice | Endpoint | Derived from |
|--------|----------|--------------|
| Category | `/category_options` | taxonomy (series catalog) |
| Tag | `/tag_options?category=` | taxonomy (tags-by-category + series) |
| Series | `/series_options?category=&tag=` | taxonomy (filtered series) |
| Event | `/event_options?series_ticker=` | live `/events` for the series |
| Market | `/market_options?event_ticker=` | live markets for the event |

`market_key` is the opaque value passed between market widgets, encoded as
`series_ticker|market_ticker|event_ticker`.

## Dashboard Tabs

| Tab | Widgets |
|-----|---------|
| Discover | 24h Volume by Category, Top Markets, Browse Markets |
| Catalog | Series Catalog, Event Discovery |
| Event | Selected Event Metrics, Event Markets |
| Market | YES/NO Probability, Probability Snapshot, Market Rules & Details, Orderbook Depth, Orderbook |
| Orderbook & Trades | Orderbook Ladder, Trade Tape |
| History | Price History Chart, Historical Prices |

**Browse Markets** is an HTML widget that searches and lists active markets as
event cards (leading outcomes, probabilities, volume, and per-outcome
thumbnails). Clicking a card opens a full **event details** page
(`/event_details`) in a new tab. Outcome images and accent colours come from
Kalshi's batched cards endpoint (`/v1/bff/cards`), fetched once per page and
cached; they degrade gracefully to plain cards if unavailable.

**Top Markets** ranks the most active open markets by 24h volume, open interest,
or total volume, filterable by category and by how soon the market resolves
(`Closes Within`), and shows each market's current YES probability and bid/ask.
Click a market to load it across the dashboard, or click the event ticker to
inspect the event.

## Configuration

All settings are optional. Copy `.env.example` to `.env` to override defaults
(loaded automatically via `python-dotenv`):

```bash
cp .env.example .env
```

| Variable | Default | Purpose |
|----------|---------|---------|
| `KALSHI_API_BASE_URL` | `https://api.elections.kalshi.com/trade-api/v2` | Kalshi public API base |
| `KALSHI_PUBLIC_BASE_URL` | request base URL | Browser-facing backend base URL used when resolving widget, app, and MCP URLs |
| `KALSHI_HTTP_TIMEOUT` | `20` | Request timeout (seconds) |
| `KALSHI_RATE_LIMIT_PER_SEC` | `8` | Shared upstream request rate limit |
| `KALSHI_QUOTE_TTL_SECONDS` | `30` | Cache TTL for markets/events/status |
| `KALSHI_REALTIME_TTL_SECONDS` | `10` | Cache TTL for orderbook/trades |
| `KALSHI_TAXONOMY_TTL_SECONDS` | `600` | Cache TTL for the series/tag taxonomy |
| `KALSHI_STATS_TTL_SECONDS` | `1800` | Cache TTL for Discover stats |
| `KALSHI_STATS_SCAN_MAX_PAGES` | `10` | Maximum 1000-market pages scanned per stats refresh |

## Deployment

This repository is prepared for the standard OpenBB GitHub Actions to Dokku
deployment pattern. Pushes to `main` run `.github/workflows/deploy.yml`, which
pushes the repository to the Dokku Git remote supplied by repository secrets.

Required GitHub repository secrets:

- `DOKKU_PROD_REMOTE`: SSH Git remote for the Dokku app, for example
  `dokku@<host>:<app>`.
- `DEPLOYER_SSH_PRIVATE_KEY`: private key allowed to push to that Dokku app.

The app uses Python buildpack inputs at the repository root:

- `requirements.txt` for dependencies.
- `Procfile` with the web process command.

Runtime configuration belongs in Dokku app config. The application can run with
defaults, but deployment administrators should review the variables in the
configuration table above. The expected health-check endpoint is `GET /`.

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Health check and backend metadata |
| `GET /widgets.json` | Workspace widget definitions |
| `GET /apps.json` | Workspace app layout |
| `GET /thumbnail.svg` | App thumbnail |
| `GET /category_options` | Category choices (taxonomy) |
| `GET /tag_options` | Tag choices for a category (taxonomy) |
| `GET /series_options` | Series choices for a category/tag (taxonomy) |
| `GET /event_options` | Event choices for a series |
| `GET /market_options` | Market choices for an event |
| `GET /volume_by_category` | 24h volume / open interest by category (chart) |
| `GET /top_markets` | Most active markets by category / metric / close window |
| `GET /browse_markets` | HTML event-card browser with global search and thumbnails |
| `GET /event_details` | Full HTML event page (opened from a Browse Markets card) |
| `GET /series_table` | Series catalog filtered by category/tag |
| `GET /events_table` | Event discovery for a series |
| `GET /event_metrics` | Selected event metric strip |
| `GET /event_markets` | Markets for the selected event |
| `GET /market_metrics` | Selected market metric strip |
| `GET /market_brief` | Selected market rules and metadata |
| `GET /market_probability_gauge` | YES/NO probability chart (`raw=true` for rows) |
| `GET /market_probability_table` | Selected market probability table |
| `GET /market_orderbook` | Selected market orderbook table |
| `GET /orderbook_depth_chart` | Two-sided orderbook depth chart |
| `GET /orderbook_ladder` | HTML orderbook ladder (`raw=true` for rows) |
| `GET /selected_trades` | Recent trades for the selected market |
| `GET /recent_trades` | Recent public trades sample |
| `GET /market_price_chart` | Selected market price/volume chart (`raw=true` for rows) |
| `GET /market_history_table` | Selected market historical rows |

## Data Source and Safety

The backend uses only Kalshi public market-data endpoints. It does not submit
orders, read portfolio data, or require Kalshi credentials. Responses are cached
in memory for short intervals to reduce repeated API calls.

## Validate Locally

With the server running:

```bash
curl http://localhost:7779/
curl http://localhost:7779/widgets.json
curl "http://localhost:7779/category_options"
curl "http://localhost:7779/tag_options?category=Economics"
curl "http://localhost:7779/series_options?category=Economics&tag=Inflation"
```
