# AI Equities Desk

Personal research terminal: type a ticker or company name, nine agents vote, XGBoost predicts the next few sessions, a walk-forward backtest shows whether that model had an out-of-sample edge, and the desk prints a **High Buy / Buy / Hold / Sell** rating.

**Backend is FastAPI. Frontend is Flask.** Names like `TSMC` resolve to a Yahoo symbol that actually has history (`TSM` NYSE ADR, or `2330.TW`).

## What it does

1. **Pulls free data** — Yahoo Finance for OHLCV, fundamentals, news, and the options chain. Stooq is the price fallback. Geopolitics from GDELT, BBC/Yahoo/Al Jazeera world RSS, and optional Finnhub general news. Optional `FINNHUB_API_KEY` and `ALPHA_VANTAGE_API_KEY` deepen news and overview stats.
2. **Resolves symbols** — aliases (`TSMC` → `TSM`), class shares (`BRK.B` → `BRK-B`), and ranked Yahoo search so you are not stuck on a 55-name mega list.
3. **Builds a feature set** — returns, trend stack, MACD, RSI, ADX, Bollinger, volume z, realized vol, 52-week location, SPY relative strength, rolling beta, VIX regime.
4. **Trains XGBoost with purged walk-forward** — expanding train window, 21-day test slices, embargo equal to the forecast horizon so labels cannot leak. If OOS Sharpe is negative, the ML vote is automatically down-weighted.
5. **Rating** — Trend, Momentum, Mean-reversion, XGBoost, **Options flow**, **Geopolitics**, Sentiment, Macro, Fundamentals. Output is **HIGH BUY**, **BUY**, **HOLD**, or **SELL**.
6. **Universe scan** — mega liquid, mega + ADRs/overseas, Nasdaq-100, S&P 500, mid-cap ($2B–$10B), or small-cap ($700M–$2B). S&P 500 and the cap universes are slower; the Flask board polls progress.

Signals are generated at the close and filled at the next open. 10 bps commission + 2 bps slippage are charged on turnover.

## Run it

```bash
cd Backtesting-Equities
pip install -r requirements.txt
python app.py
```

- Desk UI: http://127.0.0.1:5050
- API docs: http://127.0.0.1:8000/docs

Same app on one port (what Vercel runs):

```bash
uvicorn asgi:app --host 127.0.0.1 --port 5050
```

Then the UI and JSON API share `http://127.0.0.1:5050` (`/api/...` for FastAPI).

CLI:

```bash
python cli.py TSMC
python cli.py AAPL MSFT TSLA
python cli.py --scan --universe global
python cli.py --scan --universe sp500
```

Optional keys (export in the shell, never commit them):

```bash
export FINNHUB_API_KEY=...
export ALPHA_VANTAGE_API_KEY=...
export DATABASE_URL=postgresql://user:pass@host/neondb?sslmode=require
```

`DATABASE_URL` (or `POSTGRES_URL`) stores boards, option/volume tapes, universes, portfolios, and the Yahoo cache in Postgres. Without it the desk still uses `.cache/` files. Neon or Vercel Postgres is the usual hosted option. `/health` reports `"store": "postgres"` or `"files"`.

## Deploy on Vercel

The UI and API run as one FastAPI function (`asgi.py`). Universe scans must finish within the function timeout (up to 300s on Hobby), so prefer **mega** rather than S&P 500. Set `DATABASE_URL` in the Vercel project env or boards reset on every cold start.

Linux deploys install `xgboost-cpu` so the function stays under the 500 MB Python bundle limit (the default `xgboost` wheel pulls CUDA). If a build still exceeds 500 MB, add `VERCEL_SUPPORT_LARGE_FUNCTIONS=1` in Vercel → Project → Settings → Environment Variables and redeploy.

```bash
npx vercel --yes
```

Add `DATABASE_URL` in Vercel → Project → Settings → Environment Variables, then redeploy.

## Layout

```
app.py              launches FastAPI + Flask
aieq/api.py         FastAPI routes
web/                Flask templates + static
cli.py              terminal desk
aieq/
  symbols.py        TSMC → TSM, Yahoo search
  universe.py       S&P 500 / Nasdaq-100 / global
  data.py           Yahoo / Stooq / Finnhub / AV
  features.py       indicators + matrix
  models.py         XGBoost walk-forward
  backtest.py       next-open engine + metrics
  consensus.py      agents + weighted vote
  options.py        live chain ranking
  pipeline.py       analyze() / scan_universe()
  store.py          Postgres or .cache
asgi.py             one-process UI + /api (Vercel)
vercel.json         Vercel function config
```

Cache lives in Postgres when `DATABASE_URL` is set, otherwise `.cache/` (4h TTL on prices). Delete `.cache/` or the `desk_cache` table to force a refresh.
