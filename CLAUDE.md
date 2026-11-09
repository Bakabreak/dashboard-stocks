# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

"Pulse" is a stock market dashboard with two files that matter: `server.py`, a Python HTTP server that uses only the standard library, and `static/index.html`, a single page that holds all the HTML, CSS and JS. There's no build step, package manager, test suite or linter, and no dependencies to install. The one external frontend library is TradingView `lightweight-charts@4.1.3`, loaded from unpkg.

## Running

```
python server.py          # serves http://127.0.0.1:8000 (override with PORT env var)
start.bat                 # Windows: opens the browser, then runs the server
```

Static files are sent with `Cache-Control: no-cache`, so a browser reload picks up edits to `index.html` without restarting the server. Changes to `server.py` need a restart. The server suppresses request logging (`log_message` is a no-op).

To check endpoints by hand, curl them, e.g. `curl "localhost:8000/api/quotes?symbols=AAPL,^GSPC"`.

## Deployment

- **Frontend:** GitHub Pages. `.github/workflows/pages.yml` publishes `static/` on every push to `main` that touches it.
- **API:** `worker/worker.js` is a Cloudflare Worker, a dependency-free JS port of the `/api/*` routes in `server.py`. The Cloudflare project is connected to this repo, so every push to `main` redeploys it according to `wrangler.toml`: the Worker runs `/api/*`, and `static/` is served at every other path. The Worker URL therefore hosts the full site too. Without `wrangler.toml`, the Cloudflare build deploys only the static files, and every API route returns 404. **Any change to an API route must be made in both `server.py` and `worker/worker.js`.**
- `API_BASE` in `index.html` is empty on localhost (requests go to `server.py`) and `WORKER_URL` everywhere else.

## Architecture

### Backend (`server.py`): a Yahoo Finance proxy

The server exists so the browser avoids CORS. Every `/api/*` route reshapes upstream data into a compact JSON shape:

| Route | Upstream | Notes |
|---|---|---|
| `/api/quotes?symbols=A,B` | Yahoo v8 chart (range 1d) | Max 30 symbols, fetched in parallel. A failing symbol returns `{symbol, error}` instead of failing the whole batch. Includes a downsampled `spark` array of about 60 points. |
| `/api/chart?symbol=&range=` | Yahoo v8 chart | `RANGES` maps each range to a candle interval. An unknown range falls back to `1d`. |
| `/api/search?q=` | Yahoo v1 search | |
| `/api/screener?id=&count=` | Yahoo predefined screeners | `id` must be in `SCREENERS`. |
| `/api/news?symbol=` | Yahoo RSS, Yahoo search, Google News RSS | Merged, deduplicated by normalized title, sorted newest first. Returns whatever arrives within `NEWS_DEADLINE` (8s). Errors only if every source fails. |

Key behaviors:
- `fetch()` is the single upstream entry point. It keeps an in-memory cache with a TTL per call, retries once on network errors or 5xx responses, and on failure **serves stale cached data** rather than erroring.
- `quote()` is built from `chart(symbol, "1d")` metadata, not from a separate quote API.
- Symbols are validated against `SYMBOL_RE`. Index symbols such as `^GSPC` and `000001.SS` must stay valid.
- Upstream HTTP errors map to a 502 response with `{error}`, and a 404 becomes "Symbol not found".

### Frontend (`static/index.html`)

- **Two views, routed by hash:** no hash shows the home/markets view (indices, world markets clock, heatmap, movers screeners, ticker tape). `#SYMBOL` shows the stock view (quote, chart, watchlist, news). `route()` switches between them, and `refreshAll()` loads the active view.
- **Config constants** at the top of the `<script>`: `INDICES`, `MARKETS`, `DEFAULT_WATCH`, `RANGES` and `REFRESH_MS` (auto-refresh every 5 minutes). `RANGES` must stay in sync with `RANGES` in `server.py`.
- **Persistence:** `localStorage` through the `store` wrapper, under the keys `symbol`, `range`, `watch` and `theme`.
- A 1-second `setInterval` drives the refresh countdown and live market open/closed clocks (`tickHome` / `renderMarket`, using `tradingPeriod` from quotes).
- News sentiment and relevance are computed client-side with the regex heuristics `BULL`, `BEAR`, `STOP` and `relevanceTest`.
- Theme is light/dark via `data-theme` on `<html>`. When the theme changes, `applyChartTheme()` and the home renderers re-run, because the chart and heatmap colors are read from CSS.
- `api()` wraps `fetch` with a timeout and throws on an `{error}` payload.
