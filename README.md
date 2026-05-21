# Kalshi Market Dashboard

OpenBB Workspace backend for public Kalshi prediction-market data.

The app exposes a FastAPI backend that OpenBB Workspace can register as a
custom data connector. Workspace reads `widgets.json` for widget definitions and
`apps.json` for the prebuilt `Kalshi Event Explorer` app layout.

## Quick Start

From this app directory:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 7779
```

Then add this backend URL in OpenBB Workspace:

```text
http://localhost:7779
```

Workspace will load:

- `widgets.json` from `http://localhost:7779/widgets.json`
- `apps.json` from `http://localhost:7779/apps.json`

## Adding to OpenBB Workspace

1. Open OpenBB Workspace.
2. Go to **Settings** > **Data Connectors**.
3. Click **Add Custom Backend**.
4. Enter `http://localhost:7779`.
5. Open **Apps** and launch `Kalshi Event Explorer`.

If the backend is hosted somewhere else, use that deployed base URL instead of
`http://localhost:7779`.

## App Workflow

`Kalshi Event Explorer` is event-first:

1. Browse live Kalshi events by readable title, category, or search regex.
2. Click an event to update the event summary and event markets.
3. Click a market row to update probability, orderbook, trades, and history.
4. Use raw table views when you need inspectable rows instead of charts.

The app uses two Workspace endpoint-parameter groups:

| Group | Parameter | Default |
|-------|-----------|---------|
| Group 1 | `event_ticker` | `EVSHARE-30JAN` |
| Group 2 | `market_key` | `KXEVSHARE\|EVSHARE-30JAN-20\|EVSHARE-30JAN` |

`market_key` is encoded as `series_ticker|market_ticker|event_ticker`.

## Dashboard Tabs

| Tab | Widgets |
|-----|---------|
| Events | Event Discovery, Selected Event Metrics, Selected Event Brief |
| Markets | Event Markets, YES / NO Probability, Probability Snapshot, Market Rules |
| Market Detail | Orderbook Ladder, Trade Tape |
| History | Price History Chart |

## Configuration

Environment variables are optional. Copy `.env.example` to `.env` if you want to
override defaults:

```bash
cp .env.example .env
```

Available settings:

```bash
KALSHI_API_BASE_URL=https://external-api.kalshi.com/trade-api/v2
KALSHI_CACHE_TTL_SECONDS=30
KALSHI_DEFAULT_EVENT_TICKER=KXELONMARS-99
KALSHI_DEFAULT_MARKET_KEY=KXELONMARS|KXELONMARS-99|KXELONMARS-99
```

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Health check and backend metadata |
| `GET /widgets.json` | OpenBB Workspace widget definitions |
| `GET /apps.json` | OpenBB Workspace app layout |
| `GET /thumbnail.svg` | App thumbnail |
| `GET /event_options` | Event dropdown options |
| `GET /event_category_options` | Event category dropdown options |
| `GET /market_options` | Market dropdown options |
| `GET /exchange_status` | Exchange and trading status metrics |
| `GET /events_table` | Searchable event discovery table |
| `GET /event_metrics` | Selected event metric strip |
| `GET /event_brief` | Selected event markdown summary |
| `GET /event_markets` | Markets for the selected event |
| `GET /market_probability_gauge` | YES / NO probability chart |
| `GET /market_probability_table` | Selected market probability table |
| `GET /market_brief` | Selected market rules and metadata |
| `GET /market_orderbook` | Selected market orderbook table |
| `GET /orderbook_depth_chart` | Two-sided orderbook depth chart |
| `GET /orderbook_ladder` | HTML orderbook ladder |
| `GET /selected_trades` | Recent trades for the selected market |
| `GET /recent_trades` | Recent public trades sample |
| `GET /market_price_chart` | Selected market price and volume chart |
| `GET /market_history_table` | Selected market historical rows |
| `GET /featured_markets` | High-activity market table |
| `GET /series_table` | Kalshi series metadata catalog |

## Widgets

| Widget | Type | Endpoint |
|--------|------|----------|
| Exchange Status | metric | `exchange_status` |
| Event Discovery | table | `events_table` |
| Selected Event Metrics | metric | `event_metrics` |
| Selected Event Brief | markdown | `event_brief` |
| Event Markets | table | `event_markets` |
| YES / NO Probability | chart | `market_probability_gauge` |
| Probability Snapshot | table | `market_probability_table` |
| Market Rules | markdown | `market_brief` |
| Orderbook | table | `market_orderbook` |
| Orderbook Depth | chart | `orderbook_depth_chart` |
| Orderbook Ladder | html | `orderbook_ladder` |
| Trade Tape | table | `selected_trades` |
| Price History Chart | chart | `market_price_chart` |
| Historical Prices | table | `market_history_table` |
| Series Catalog | table | `series_table` |

## Data Source and Safety

The backend uses Kalshi public market-data endpoints. It does not submit orders,
read portfolio data, or require Kalshi credentials. Responses are cached in
memory for short intervals to reduce repeated API calls.

## Validate Locally

With the server running, check the core Workspace files:

```bash
curl http://localhost:7779/
curl http://localhost:7779/widgets.json
curl http://localhost:7779/apps.json
```

If this app is inside the `backend-examples-for-openbb-workspace` repository,
you can also run the shared validators from that repository root:

```bash
python scripts/validate_app.py apps/kalshi-market-dashboard
python scripts/validate_endpoints.py apps/kalshi-market-dashboard --base-url http://localhost:7779
```
