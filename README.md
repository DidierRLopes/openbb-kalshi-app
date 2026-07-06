# Kalshi Market Dashboard

FastAPI backend that OpenBB Workspace registers as a custom data connector for
public Kalshi prediction-market data. Workspace loads `widgets.json` (widget
definitions) and `apps.json` (the **Kalshi Market Explorer** layout) from it.

## Run

```bash
# Docker
docker build -t kalshi-app .
docker run -p 7779:7779 -v kalshi-cache:/cache kalshi-app

# or locally
pip install -r requirements.txt
uvicorn main:app --port 7779
```

Add `http://localhost:7779` in Workspace → **Settings → Data Connectors → Add
Custom Backend**, then launch **Kalshi Market Explorer** from **Apps**.

## How it works

All caching is on disk via [`diskcache`](https://grantjenks.com/docs/diskcache/)
under `KALSHI_CACHE_DIR` — nothing stays in RAM and the cache survives restarts.
A single background ingestor pages the full open-market book (paced, one chunk at
a time so it never spikes CPU/RAM), refreshes the category/tag taxonomy, and warms
every event's card images. It runs as a **blocking startup warmup** — the app does
not serve until the first load completes — then refreshes on a schedule:

| Data | Refresh interval |
|------|------------------|
| Tags / categories / series | ~10 min (`KALSHI_TAXONOMY_TTL_SECONDS`) |
| Markets snapshot + card images | ~30 min (`KALSHI_STATS_TTL_SECONDS`) |

## Configuration

Optional; copy `.env.example` to `.env` to override.

| Variable | Default | Purpose |
|----------|---------|---------|
| `KALSHI_CACHE_DIR` | `./.cache` | On-disk cache location (mount a volume in prod) |
| `KALSHI_HTTP_CACHE_SIZE_LIMIT_MB` | `256` | HTTP response cache size cap |
| `KALSHI_TAXONOMY_TTL_SECONDS` | `600` | Taxonomy refresh interval |
| `KALSHI_STATS_TTL_SECONDS` | `1800` | Market snapshot refresh interval |
| `KALSHI_STATS_SCAN_MAX_PAGES` | `200` | Safety ceiling on 1000-market pages per scan |
| `KALSHI_STATS_PAGE_PAUSE_SECONDS` | `0.25` | Pause between ingest pages |
| `KALSHI_RATE_LIMIT_PER_SEC` | `8` | Upstream request rate limit |
| `KALSHI_PUBLIC_BASE_URL` | request URL | Browser-facing base URL for widget/app/MCP links |

## Deployment

Pushes to `main` run `.github/workflows/deploy.yml`, which deploys to Dokku via the
`Dockerfile` using the `DOKKU_PROD_REMOTE` and `DEPLOYER_SSH_PRIVATE_KEY` secrets.
Mount a persistent volume for the cache:

```bash
dokku storage:ensure-directory kalshi-cache
dokku storage:mount <app> /var/lib/dokku/data/storage/kalshi-cache:/cache
dokku config:set <app> KALSHI_CACHE_DIR=/cache
```

Provision ~1 GB. The **first** deploy on a cold volume takes ~1–2 min to warm up
before answering (raise `DOKKU_CHECKS_WAIT` if the health check is too strict);
later restarts serve the persisted snapshot immediately.

Uses only public market-data endpoints — no orders, no credentials.
</content>
