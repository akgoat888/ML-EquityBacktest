"""Flask UI for the equities desk. Talks to the FastAPI backend."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

ROOT = Path(__file__).resolve().parent
API_URL = os.getenv("API_URL", "").strip().rstrip("/")
if not API_URL:
    API_URL = "/api" if os.getenv("VERCEL") else "http://127.0.0.1:8000"
ASSET_V = "20260907c"

app = Flask(
    __name__,
    template_folder=str(ROOT / "templates"),
    static_folder=str(ROOT / "static"),
)
app.config["API_URL"] = API_URL
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0


def _desk(page: str, ticker: str = "", period: str = "5y", horizon: str = "5"):
    return render_template(
        "desk.html",
        api_url=API_URL,
        page=page,
        ticker=ticker,
        period=period,
        horizon=horizon,
        asset_v=ASSET_V,
    )


@app.route("/")
def index():
    return _desk("home")


@app.route("/options")
def options_page():
    return _desk("options", ticker=request.args.get("ticker", ""))


@app.route("/volume")
def volume_page():
    return _desk("volume", ticker=request.args.get("ticker", ""))


@app.route("/financials")
@app.route("/financials/<path:ticker>")
def financials_page(ticker: str = ""):
    return _desk("financials", ticker=ticker or request.args.get("ticker", ""))


@app.route("/portfolios")
def portfolios_page():
    return _desk("portfolios")


@app.route("/ticker/<path:ticker>")
def ticker_page(ticker: str):
    return _desk(
        "ticker",
        ticker=ticker,
        period=request.args.get("period", "5y"),
        horizon=request.args.get("horizon", "5"),
    )


def _json_error(exc, code=400):
    return jsonify({"detail": str(exc)}), code


@app.errorhandler(Exception)
def _api_uncaught(exc):
    if isinstance(exc, HTTPException):
        return exc
    if request.path.startswith("/api/"):
        return _json_error(exc, 500)
    raise exc


@app.route("/api/quote")
def api_quote():
    from aieq.data import fetch_quote

    ticker = (request.args.get("ticker") or "").strip()
    if not ticker:
        return _json_error("Enter a ticker.")
    try:
        return jsonify(fetch_quote(ticker))
    except Exception as exc:
        return _json_error(exc)


@app.route("/api/search")
def api_search():
    from aieq.symbols import _search_yahoo, resolve_symbol

    q = (request.args.get("q") or "").strip()
    if not q:
        return _json_error("Enter a query.")
    try:
        resolved, tried = resolve_symbol(q)
        quotes = [
            {
                "symbol": item.get("symbol"),
                "shortname": item.get("shortname") or item.get("longname") or item.get("shortName"),
                "longname": item.get("longname") or item.get("longName"),
                "exchange": item.get("exchange") or item.get("exchDisp"),
                "quoteType": item.get("quoteType"),
            }
            for item in _search_yahoo(q)[:10]
        ]
        return jsonify({"query": q, "resolved": resolved, "tried": tried, "quotes": quotes})
    except Exception as exc:
        return _json_error(exc)


@app.route("/api/financials")
def api_financials():
    from aieq.screens import fetch_statements

    ticker = (request.args.get("ticker") or "").strip()
    freq = (request.args.get("freq") or "quarterly").strip()
    quarter = request.args.get("quarter", type=int)
    year = request.args.get("year", type=int)
    if not ticker:
        return _json_error("Enter a ticker.")
    try:
        return jsonify(fetch_statements(ticker, freq=freq, quarter=quarter, year=year))
    except Exception as exc:
        return _json_error(exc)


@app.route("/api/options/flags")
def api_options_flags():
    from aieq.jobs import get_options_state
    from aieq.screens import enrich_option_rows, flag_option_prints, load_screen, option_sentiment_counts, tape_year_month
    from aieq.symbols import resolve_symbol

    ticker = (request.args.get("ticker") or "").strip()
    year = request.args.get("year", type=int)
    month = request.args.get("month", type=int)
    uni = request.args.get("universe") or "mega"
    if ticker:
        resolved, tried = resolve_symbol(ticker)
        if not resolved:
            return _json_error(f"No listing for {ticker!r}. Tried: {', '.join(tried) or 'nothing'}.")
        year, month = tape_year_month(year, month)
        try:
            rows = enrich_option_rows(flag_option_prints(resolved, max_expiries=12, year=year, month=month), uni)
        except Exception as exc:
            return _json_error(exc, 500)
        return jsonify(
            {
                "ticker": resolved,
                "n": len(rows),
                "n_tickers": 1 if rows else 0,
                "counts": option_sentiment_counts(rows),
                "year": year,
                "month": month,
                "rows": rows,
                "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                "job": get_options_state(),
            }
        )
    payload = load_screen("options", uni)
    rows = enrich_option_rows(payload.get("rows") or [], uni)
    payload["rows"] = rows
    payload["n"] = len(rows)
    payload["n_tickers"] = len({str(r.get("ticker") or "").upper() for r in rows if r.get("ticker")})
    payload["counts"] = option_sentiment_counts(rows)
    payload["job"] = get_options_state()
    return jsonify(payload)


@app.route("/api/options/refresh", methods=["POST"])
def api_options_refresh():
    from aieq.jobs import start_options_scan

    body = request.get_json(silent=True) or {}
    return jsonify(
        start_options_scan(
            universe=body.get("universe") or "mega",
            year=body.get("year"),
            month=body.get("month"),
        )
    )


@app.route("/api/volume/large")
def api_volume_large():
    from aieq.jobs import get_volume_state
    from aieq.screens import large_block_prints, load_screen, tape_year_month
    from aieq.symbols import resolve_symbol

    ticker = (request.args.get("ticker") or "").strip()
    year = request.args.get("year", type=int)
    month = request.args.get("month", type=int)
    min_notional = request.args.get("min_notional", default=10_000_000, type=float)
    if ticker:
        resolved, tried = resolve_symbol(ticker)
        if not resolved:
            return _json_error(f"No listing for {ticker!r}.")
        year, month = tape_year_month(year, month)
        block = large_block_prints(resolved, year=year, month=month, min_notional=min_notional)
        prints = (block or {}).get("prints") or []
        return jsonify(
            {
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
                    "(bullish); a red bar as a sell."
                ),
            }
        )
    uni = request.args.get("universe") or "mega"
    payload = load_screen("volume", uni)
    payload["job"] = get_volume_state()
    payload["min_notional"] = min_notional
    return jsonify(payload)


@app.route("/api/volume/refresh", methods=["POST"])
def api_volume_refresh():
    from aieq.jobs import start_volume_scan

    body = request.get_json(silent=True) or {}
    return jsonify(
        start_volume_scan(
            universe=body.get("universe") or "mega",
            year=body.get("year"),
            month=body.get("month"),
        )
    )


@app.route("/api/portfolios")
def api_portfolios():
    from aieq.portfolios import suggest_portfolios

    uni = request.args.get("universe") or "global"
    years = request.args.get("years", default=1, type=int)
    refresh = str(request.args.get("refresh") or "").lower() in {"1", "true", "yes"}
    try:
        return jsonify(suggest_portfolios(universe=uni, years=years, refresh=refresh))
    except Exception:
        from aieq.portfolios import _empty_portfolios, load_portfolios

        cached = load_portfolios(uni, years)
        return jsonify(cached or _empty_portfolios(uni, years))
