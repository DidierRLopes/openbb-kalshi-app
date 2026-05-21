# Kalshi Market Dashboard

OpenBB Workspace backend for public Kalshi prediction-market data.

## Run

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 7779
```

Then add `http://localhost:7779` as a data connector in OpenBB Workspace.

## Configuration

Environment variables are optional:

```bash
KALSHI_API_BASE_URL=https://external-api.kalshi.com/trade-api/v2
KALSHI_CACHE_TTL_SECONDS=30
KALSHI_DEFAULT_EVENT_TICKER=KXELONMARS-99
KALSHI_DEFAULT_MARKET_KEY=KXELONMARS|KXELONMARS-99|KXELONMARS-99
```

The app uses public market-data endpoints and does not submit orders or read portfolio data.
# openbb-kalshi-app
