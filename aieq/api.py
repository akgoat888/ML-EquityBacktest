"""FastAPI backend for the equities desk."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from aieq.config import Settings
from aieq.data import fetch_ohlcv, last_completed_session
from aieq.jobs import (
    get_options_state,
    get_state,
    get_volume_state,
    start_options_scan,
    start_scan,
    start_volume_scan,
)
from aieq.pipeline import _spark_points, analyze, board_market_session, load_board, result_to_jsonable
from aieq.screens import enrich_option_rows, flag_option_prints, fetch_statements, load_screen, option_sentiment_counts, tape_year_month
from aieq.symbols import _search_yahoo, resolve_symbol
from aieq.universe import universe_tickers

app = FastAPI(title="AI Equities Desk", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AnalyzeIn(BaseModel):
    ticker: str
    period: str = "5y"
    horizon: int = Field(default=5, ge=1, le=21)
    deep: bool = True


class ScanIn(BaseModel):
    universe: str = "global"
    deep: bool = False
    max_workers: int = Field(default=8, ge=1, le=12)
    year: int | None = None
    month: int | None = None
    cursor: int | None = Field(default=None, ge=0)


def _edge(response: Response, cache_control: str) -> None:
    """Browser and Vercel CDN. The CDN header is what keeps a repeat lookup off the model."""
    response.headers["Cache-Control"] = cache_control
    response.headers["CDN-Cache-Control"] = cache_control
    response.headers["Vercel-CDN-Cache-Control"] = cache_control


def _cached(response: Response, key: str, ttl_sec: float, build, cache_control: str, *, durable: bool = True):
    from aieq.memcache import cached_call

    body = cached_call(key, ttl_sec, build, durable=durable)
    _edge(response, cache_control)
    return body


def _safe_key(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in text)


@app.get("/health")
def health() -> dict[str, str]:
    from aieq.store import backend_name, ensure_schema

    ensure_schema()
    return {"status": "ok", "store": backend_name()}


@app.get("/universe/{name}")
def universe_members(name: str, response: Response) -> dict[str, Any]:
    """Membership only. Snapshots make this return before any quote is fetched."""
    tickers = universe_tickers(name)
    response.headers["Cache-Control"] = (
        "public, max-age=3600, s-maxage=86400, stale-while-revalidate=604800"
    )
    return {"id": name, "n": len(tickers), "tickers": tickers}


@app.get("/universes")
def universes() -> dict[str, Any]:
    return {
        "choices": [
            {"id": "mega", "label": "Mega liquid (~55)", "n": len(universe_tickers("mega"))},
            {"id": "global", "label": "Mega + ADRs / overseas", "n": len(universe_tickers("global"))},
            {"id": "nasdaq100", "label": "Nasdaq-100", "n": None},
            {"id": "sp500", "label": "S&P 500 + ADRs (slow)", "n": None},
            {"id": "midcap", "label": "Mid-cap $2B–$10B", "n": None},
            {"id": "smallcap", "label": "Small-cap $700M–$2B", "n": None},
        ]
    }


@app.get("/search")
def search(response: Response, q: str = Query(..., min_length=1)) -> dict[str, Any]:
    def build() -> dict[str, Any]:
        resolved, tried = resolve_symbol(q)
        quotes = []
        for item in _search_yahoo(q)[:10]:
            quotes.append(
                {
                    "symbol": item.get("symbol"),
                    "shortname": item.get("shortname") or item.get("longname") or item.get("shortName"),
                    "longname": item.get("longname") or item.get("longName"),
                    "exchange": item.get("exchange") or item.get("exchDisp"),
                    "quoteType": item.get("quoteType"),
                }
            )
        return {"query": q, "resolved": resolved, "tried": tried, "quotes": quotes}

    return _cached(
        response,
        "search_v1_" + _safe_key(q.strip().lower()),
        6 * 3600,
        build,
        "public, max-age=300, s-maxage=86400, stale-while-revalidate=86400",
    )


@app.get("/quote")
def quote(response: Response, ticker: str = Query(..., min_length=1)) -> dict[str, Any]:
    from aieq.data import fetch_quote

    try:
        payload = fetch_quote(ticker)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    _edge(response, "public, max-age=15, s-maxage=30, stale-while-revalidate=120")
    return payload


@app.post("/analyze")
def analyze_ep(response: Response, body: AnalyzeIn) -> dict[str, Any]:
    from aieq.symbols import resolve_symbol

    resolved, _tried = resolve_symbol(body.ticker)
    sym = resolved or body.ticker.strip().upper()
    key = "analysis_v1_" + _safe_key(f"{sym}|{body.period}|{int(body.horizon)}|{int(body.deep)}")

    def build() -> dict[str, Any]:
        settings = Settings(period=body.period, horizon=int(body.horizon))
        res = analyze(body.ticker, settings=settings, deep=body.deep)
        return result_to_jsonable(res)

    try:
        return _cached(
            response,
            key,
            6 * 3600,
            build,
            "public, max-age=60, s-maxage=21600, stale-while-revalidate=86400",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/analyze")
def analyze_get(
    response: Response,
    ticker: str = Query(...),
    period: str = "5y",
    horizon: int = 5,
    deep: bool = True,
) -> dict[str, Any]:
    return analyze_ep(response, AnalyzeIn(ticker=ticker, period=period, horizon=horizon, deep=deep))


@app.get("/board")
def board(response: Response, universe: str | None = Query(default=None)) -> dict[str, Any]:
    df, as_of, uni = load_board(universe)
    rows = df.to_dict(orient="records") if df is not None and not df.empty else []
    counts = dict(Counter(str(r.get("rating") or "") for r in rows)) if rows else {}
    session = board_market_session(rows)
    last_sess = last_completed_session().strftime("%Y-%m-%d")
    stale = bool(session and session < last_sess)
    job = get_state()
    scanning_this = job.get("status") == "running" and (not universe or job.get("universe") == (uni or universe))
    # Edge-cache a finished board. A scan in progress must not be frozen at the CDN.
    if rows and not scanning_this:
        response.headers["Cache-Control"] = "public, max-age=60, s-maxage=300, stale-while-revalidate=3600"
    else:
        response.headers["Cache-Control"] = "no-store"
    return {
        "as_of": as_of,
        "session": session,
        "last_session": last_sess,
        "stale": stale,
        "n": len(rows),
        "universe": uni or universe,
        "counts": counts,
        "rows": rows,
        "job": job,
    }


@app.get("/sparkline")
def sparkline(ticker: str = Query(..., min_length=1), n: int = Query(default=90, ge=10, le=252)) -> dict[str, Any]:
    resolved, tried = resolve_symbol(ticker)
    if not resolved:
        raise HTTPException(status_code=404, detail=f"No history for {ticker!r}. Tried: {', '.join(tried) or 'nothing'}.")
    df = fetch_ohlcv(resolved, Settings(period="1y"))
    return {"ticker": resolved, "points": _spark_points(df, n)}


@app.get("/board/status")
def board_status() -> dict[str, Any]:
    return get_state()


@app.post("/board/refresh")
def board_refresh(body: ScanIn | None = None) -> dict[str, Any]:
    body = body or ScanIn()
    return start_scan(
        universe=body.universe,
        deep=body.deep,
        max_workers=body.max_workers,
        cursor=body.cursor,
    )


@app.get("/options/flags")
def options_flags(
    response: Response,
    ticker: str | None = Query(default=None),
    universe: str | None = Query(default=None),
    year: int | None = Query(default=None),
    month: int | None = Query(default=None),
) -> dict[str, Any]:
    if ticker:
        resolved, tried = resolve_symbol(ticker)
        if not resolved:
            raise HTTPException(status_code=400, detail=f"No listing for {ticker!r}. Tried: {', '.join(tried) or 'nothing'}.")
        year, month = tape_year_month(year, month)

        def build() -> dict[str, Any]:
            rows = enrich_option_rows(flag_option_prints(resolved, max_expiries=12, year=year, month=month), universe)
            return {
                "ticker": resolved,
                "queried": ticker,
                "n": len(rows),
                "n_tickers": 1 if rows else 0,
                "counts": option_sentiment_counts(rows),
                "year": year,
                "month": month,
                "min_premium": 1_000_000,
                "rows": rows,
                "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                "job": get_options_state(),
                "note": f"Tape for {year}-{month:02d} expiries (current month).",
            }

        try:
            return _cached(
                response,
                "opt_v1_" + _safe_key(f"{resolved}|{year}|{month}"),
                15 * 60,
                build,
                "public, max-age=60, s-maxage=300, stale-while-revalidate=3600",
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
    response.headers["Cache-Control"] = "no-store"
    uni = universe or "mega"
    payload = load_screen("options", uni)
    rows = enrich_option_rows(payload.get("rows") or [], uni)
    payload["rows"] = rows
    payload["n"] = len(rows)
    payload["n_tickers"] = len({str(r.get("ticker") or "").upper() for r in rows if r.get("ticker")})
    payload["counts"] = option_sentiment_counts(rows)
    payload["job"] = get_options_state()
    payload["min_premium"] = 1_000_000
    return payload


@app.post("/options/refresh")
def options_refresh(body: ScanIn | None = None) -> dict[str, Any]:
    body = body or ScanIn()
    return start_options_scan(universe=body.universe, year=body.year, month=body.month)


@app.get("/volume/large")
def volume_large(
    response: Response,
    ticker: str | None = Query(default=None),
    universe: str | None = Query(default=None),
    year: int | None = Query(default=None),
    month: int | None = Query(default=None),
    min_notional: float = Query(default=10_000_000, ge=100_000, le=5_000_000_000),
) -> dict[str, Any]:
    from aieq.screens import large_block_prints

    if ticker:
        resolved, tried = resolve_symbol(ticker)
        if not resolved:
            raise HTTPException(status_code=400, detail=f"No listing for {ticker!r}.")
        year, month = tape_year_month(year, month)

        def build() -> dict[str, Any]:
            block = large_block_prints(resolved, year=year, month=month, min_notional=min_notional)
            prints = (block or {}).get("prints") or []
            return {
                "ticker": resolved,
                "n": len(prints),
                "year": year,
                "month": month,
                "min_notional": min_notional,
                "interval": (block or {}).get("interval"),
                "summary": {k: v for k, v in (block or {}).items() if k not in {"prints"}} if block else None,
                "rows": prints,
                "job": get_volume_state(),
                "note": (
                    f"Bars of at least ${min_notional:,.0f}. A green bar is treated as a one-time buy "
                    "(bullish); a red bar as a sell. Yahoo does not name the buyer."
                ),
            }

        return _cached(
            response,
            "vol_v1_" + _safe_key(f"{resolved}|{year}|{month}|{int(min_notional)}"),
            15 * 60,
            build,
            "public, max-age=60, s-maxage=300, stale-while-revalidate=3600",
        )
    response.headers["Cache-Control"] = "no-store"
    uni = universe or "mega"
    payload = load_screen("volume", uni)
    payload["job"] = get_volume_state()
    payload["min_notional"] = min_notional
    return payload


@app.post("/volume/refresh")
def volume_refresh(body: ScanIn | None = None) -> dict[str, Any]:
    body = body or ScanIn()
    return start_volume_scan(universe=body.universe, year=body.year, month=body.month)


@app.get("/portfolios")
def portfolios(
    response: Response,
    universe: str = Query(default="global"),
    years: int = Query(default=1, ge=1, le=10),
    refresh: bool = Query(default=False),
) -> dict[str, Any]:
    from aieq.portfolios import suggest_portfolios

    try:
        payload = suggest_portfolios(universe=universe, years=years, refresh=refresh)
    except Exception:
        from aieq.portfolios import _empty_portfolios, load_portfolios

        payload = load_portfolios(universe, years) or _empty_portfolios(universe, years)
    if refresh:
        response.headers["Cache-Control"] = "no-store"
    elif payload.get("sleeves") is not None:
        response.headers["Cache-Control"] = "public, max-age=60, s-maxage=600, stale-while-revalidate=86400"
    return payload


@app.get("/etfs")
def etfs(response: Response, refresh: bool = Query(default=False)) -> dict[str, Any]:
    from aieq.etfs import suggest_etfs

    payload = suggest_etfs(refresh=refresh)
    if refresh:
        response.headers["Cache-Control"] = "no-store"
    elif payload.get("funds") is not None:
        response.headers["Cache-Control"] = "public, max-age=60, s-maxage=600, stale-while-revalidate=86400"
    return payload


@app.get("/financials")
def financials(
    response: Response,
    ticker: str = Query(..., min_length=1),
    freq: str = Query(default="quarterly"),
    quarter: int | None = Query(default=None, ge=1, le=4),
    year: int | None = Query(default=None, ge=1990, le=2100),
) -> dict[str, Any]:
    try:
        return _cached(
            response,
            "fin_v1_" + _safe_key(f"{ticker.strip().upper()}|{freq}|{quarter}|{year}"),
            12 * 3600,
            lambda: fetch_statements(ticker, freq=freq, quarter=quarter, year=year),
            "public, max-age=300, s-maxage=3600, stale-while-revalidate=86400",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
