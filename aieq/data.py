from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from aieq.config import Settings, DEFAULT, ensure_cache_dir
from aieq.yahoo import configure, gated, yf_ticker

configure()

_US_EASTERN = ZoneInfo("America/New_York")


def last_completed_session(now: datetime | None = None) -> pd.Timestamp:
    """Most recent finished US regular session (16:05 ET), skipping weekends."""
    now = now or datetime.now(_US_EASTERN)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_US_EASTERN)
    else:
        now = now.astimezone(_US_EASTERN)
    closed = now.hour > 16 or (now.hour == 16 and now.minute >= 5)
    day = now.date()
    if now.weekday() >= 5 or not closed:
        if now.weekday() == 5:
            delta = 1
        elif now.weekday() == 6:
            delta = 2
        else:
            delta = 1
        day = (pd.Timestamp(day) - pd.Timedelta(days=delta)).date()
    while pd.Timestamp(day).weekday() >= 5:
        day = (pd.Timestamp(day) - pd.Timedelta(days=1)).date()
    return pd.Timestamp(day)


def _bars_behind_session(df: pd.DataFrame) -> bool:
    if df is None or df.empty:
        return True
    last = pd.Timestamp(df.index.max()).tz_localize(None).normalize()
    return last < last_completed_session()


def _cache_path(key: str) -> Path:
    root = ensure_cache_dir()
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in f"v2_{key}")
    return root / f"{safe}.pkl"


def _load_cache(key: str, ttl_hours: float) -> Any | None:
    from aieq.store import get_blob

    hit = get_blob(f"v2_{key}", ttl_hours)
    if hit is not None:
        return hit
    path = _cache_path(key)
    if not path.exists():
        return None
    age_h = (time.time() - path.stat().st_mtime) / 3600.0
    if age_h > ttl_hours:
        return None
    try:
        return pd.read_pickle(path)
    except Exception:
        return None


def _save_cache(key: str, obj: Any) -> None:
    from aieq.store import put_blob, uses_postgres

    try:
        put_blob(f"v2_{key}", obj)
    except Exception:
        pass
    if uses_postgres():
        return
    try:
        pd.to_pickle(obj, _cache_path(key))
    except Exception:
        pass


def _flatten_bars(df: pd.DataFrame, *, keep_time: bool = False) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = [str(c[0]).lower() for c in out.columns]
    else:
        out.columns = [str(c).lower().replace(" ", "_") for c in out.columns]
    rename = {
        "adj_close": "close",
        "adjclose": "close",
        "adj close": "close",
    }
    out = out.rename(columns=rename)
    needed = ["open", "high", "low", "close", "volume"]
    for col in needed:
        if col not in out.columns:
            if col == "volume":
                out[col] = 0.0
            elif "close" in out.columns:
                out[col] = out["close"]
            else:
                return pd.DataFrame(columns=needed)
    out = out[needed].copy()
    idx = pd.to_datetime(out.index)
    if getattr(idx, "tz", None) is not None:
        if keep_time:
            idx = idx.tz_convert("America/New_York").tz_localize(None)
        else:
            # Daily bars are session dates. Converting UTC midnight to NY
            # would shift the calendar date back a day during US DST.
            idx = pd.DatetimeIndex([pd.Timestamp(ts.date()) for ts in idx])
    out.index = pd.DatetimeIndex(idx) if keep_time else pd.DatetimeIndex(idx).normalize()
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out = out.replace([np.inf, -np.inf], np.nan).dropna(subset=["close"])
    return out.astype(float)


def _flatten_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    return _flatten_bars(df, keep_time=False)


def _month_bounds(year: int, month: int) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = pd.Timestamp(year=int(year), month=int(month), day=1)
    end = start + pd.offsets.MonthBegin(1)
    now = pd.Timestamp.now()
    if end > now:
        end = now + pd.Timedelta(hours=1)
    return start, end


def pick_intraday_interval(start: pd.Timestamp, end: pd.Timestamp | None = None) -> str:
    """Yahoo caps: 1m ~7d, 5m ~60d, 60m ~730d."""
    now = pd.Timestamp.now()
    span = (now - pd.Timestamp(start)).days
    window = (pd.Timestamp(end) - pd.Timestamp(start)).days if end is not None else span
    if span <= 6 and window <= 7:
        return "1m"
    if span <= 59:
        return "5m"
    if span <= 720:
        return "60m"
    return "1d"


def fetch_intraday(
    ticker: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    interval: str | None = None,
) -> tuple[pd.DataFrame, str]:
    """OHLCV bars with timestamps kept (for concentrated print detection)."""
    ticker = ticker.strip().upper()
    start = pd.Timestamp(start)
    end = pd.Timestamp(end)
    preferred = interval or pick_intraday_interval(start, end)
    chain: list[str] = []
    for iv in (preferred, "5m", "15m", "60m", "1d"):
        if iv not in chain:
            chain.append(iv)
    if (end - start).days > 7:
        chain = [c for c in chain if c != "1m"] or ["5m", "60m", "1d"]
    last_iv = preferred
    for iv in chain:
        last_iv = iv
        ttl = 0.35 if iv in {"1m", "2m", "5m", "15m"} else 6.0
        key = f"intra_{ticker}_{start.date()}_{end.date()}_{iv}"
        cached = _load_cache(key, ttl)
        if isinstance(cached, pd.DataFrame) and not cached.empty:
            return cached, iv
        df = pd.DataFrame()
        try:
            raw = gated(
                lambda: yf_ticker(ticker).history(
                    start=start.strftime("%Y-%m-%d"),
                    end=end.strftime("%Y-%m-%d"),
                    interval=iv,
                    auto_adjust=True,
                    prepost=False,
                )
            )
            df = _flatten_bars(raw, keep_time=iv != "1d")
        except Exception:
            df = pd.DataFrame()
        if df is not None and not df.empty:
            _save_cache(key, df)
            return df, iv
    return pd.DataFrame(), last_iv


def _stooq_symbol(ticker: str) -> str | None:
    t = ticker.strip().upper()
    if t.startswith("^"):
        mapping = {"^GSPC": "^spx", "^DJI": "^dji", "^IXIC": "^ndq", "^VIX": "^vix", "^RUT": "^rut"}
        return mapping.get(t)
    if t in {"SPY", "QQQ", "IWM", "DIA", "GLD", "SLV", "TLT", "HYG", "XLE", "XLF", "XLK", "SMH"}:
        return f"{t.lower()}.us"
    if "." in t:
        return None
    return f"{t.lower()}.us"


def _fetch_stooq(ticker: str) -> pd.DataFrame:
    sym = _stooq_symbol(ticker)
    if not sym:
        return pd.DataFrame()
    url = f"https://stooq.com/q/d/l/?s={sym}&i=d"
    try:
        df = pd.read_csv(url)
        if df.empty or "Close" not in df.columns:
            return pd.DataFrame()
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date").sort_index()
        df = df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
        return _flatten_ohlcv(df)
    except Exception:
        return pd.DataFrame()


def fetch_ohlcv(ticker: str, settings: Settings = DEFAULT) -> pd.DataFrame:
    ticker = ticker.strip().upper()
    key = f"ohlcv_{ticker}_{settings.period}_{settings.interval}"
    stale_cached = None
    cached = _load_cache(key, settings.cache_ttl_hours)
    if isinstance(cached, pd.DataFrame) and not cached.empty:
        daily = str(settings.interval).lower() in {"1d", "1wk", "1mo"}
        if daily and _bars_behind_session(cached):
            stale_cached = cached
        else:
            return cached

    df = pd.DataFrame()
    try:
        raw = gated(
            lambda: yf_ticker(ticker).history(
                period=settings.period, interval=settings.interval, auto_adjust=True
            )
        )
        df = _flatten_ohlcv(raw)
    except Exception:
        df = pd.DataFrame()

    if df.empty and settings.period != "max":
        try:
            raw = gated(
                lambda: yf_ticker(ticker).history(period="max", interval=settings.interval, auto_adjust=True)
            )
            df = _flatten_ohlcv(raw)
        except Exception:
            df = pd.DataFrame()

    if df.empty:
        df = _fetch_stooq(ticker)

    if df.empty and stale_cached is not None:
        return stale_cached

    if not df.empty:
        _save_cache(key, df)
    return df


def _quote_get(obj: Any, *names: str) -> Any:
    for name in names:
        try:
            if isinstance(obj, dict):
                val = obj.get(name)
            elif hasattr(obj, "get"):
                val = obj.get(name)
            else:
                val = getattr(obj, name, None)
        except Exception:
            val = None
        if val is None or val == "":
            continue
        try:
            f = float(val)
            if np.isfinite(f):
                return f
        except (TypeError, ValueError):
            return val
    return None


def fetch_quote(ticker: str) -> dict[str, Any]:
    """Last trade / previous close from Yahoo fast_info (cached ~20s)."""
    from aieq.symbols import resolve_symbol

    queried = ticker.strip()
    resolved, tried = resolve_symbol(queried)
    if not resolved:
        raise ValueError(f"No listing for {queried!r}. Tried: {', '.join(tried) or 'nothing'}.")
    key = f"quote_{resolved}"
    cached = _load_cache(key, ttl_hours=20.0 / 3600.0)
    if isinstance(cached, dict) and cached.get("price"):
        return cached

    t = yf_ticker(resolved)
    last = prev = None
    name = None
    currency = "USD"
    try:
        fi = getattr(t, "fast_info", None)
        if fi is not None:
            last = _quote_get(fi, "last_price", "lastPrice", "regularMarketPrice")
            prev = _quote_get(fi, "previous_close", "previousClose", "regular_market_previous_close")
            currency = _quote_get(fi, "currency") or "USD"
    except Exception:
        pass
    info: dict[str, Any] = {}
    try:
        info = _safe_info(resolved)
    except Exception:
        info = {}
    if last is None:
        last = _quote_get(info, "currentPrice", "regularMarketPrice", "lastPrice")
    if prev is None:
        prev = _quote_get(info, "previousClose", "regularMarketPreviousClose")
    name = info.get("shortName") or info.get("longName") or resolved
    if last is None:
        df = fetch_ohlcv(resolved, Settings(period="5d"))
        if df is not None and not df.empty:
            last = float(df["close"].iloc[-1])
            if prev is None and len(df) > 1:
                prev = float(df["close"].iloc[-2])
    last_f = float(last) if last is not None else None
    prev_f = float(prev) if prev is not None else None
    change = (last_f - prev_f) if last_f is not None and prev_f is not None else None
    change_pct = (change / prev_f) if change is not None and prev_f else None
    payload = {
        "ticker": resolved,
        "queried": queried,
        "name": name,
        "price": None if last_f is None else round(last_f, 4),
        "prev_close": None if prev_f is None else round(prev_f, 4),
        "change": None if change is None else round(change, 4),
        "change_pct": None if change_pct is None else round(change_pct, 6),
        "currency": currency if isinstance(currency, str) else "USD",
        "as_of": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
    }
    if payload["price"] is not None:
        _save_cache(key, payload)
    return payload


def fetch_benchmarks(settings: Settings = DEFAULT) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for sym in ("SPY", "QQQ", "^VIX"):
        try:
            out[sym] = fetch_ohlcv(sym, settings)
        except Exception:
            out[sym] = pd.DataFrame()
    return out


def _safe_info(ticker: str) -> dict[str, Any]:
    try:
        info = gated(lambda: yf_ticker(ticker).info) or {}
        return dict(info) if isinstance(info, dict) else {}
    except Exception:
        return {}


def fetch_fundamentals(ticker: str, settings: Settings = DEFAULT) -> dict[str, Any]:
    ticker = ticker.strip().upper()
    key = f"fund3_{ticker}"
    cached = _load_cache(key, settings.cache_ttl_hours)
    if isinstance(cached, dict) and (
        cached.get("sector") or cached.get("industry") or cached.get("business_summary")
    ):
        return cached

    info = _safe_info(ticker)
    fundamentals = {
        "name": info.get("shortName") or info.get("longName") or ticker,
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "business_summary": info.get("longBusinessSummary") or info.get("description"),
        "market_cap": info.get("marketCap"),
        "trailing_pe": info.get("trailingPE"),
        "forward_pe": info.get("forwardPE"),
        "peg": info.get("pegRatio"),
        "price_to_book": info.get("priceToBook"),
        "profit_margin": info.get("profitMargins"),
        "roe": info.get("returnOnEquity"),
        "revenue_growth": info.get("revenueGrowth"),
        "earnings_growth": info.get("earningsGrowth"),
        "recommendation_mean": info.get("recommendationMean"),
        "target_mean": info.get("targetMeanPrice"),
        "target_high": info.get("targetHighPrice"),
        "target_low": info.get("targetLowPrice"),
        "current_price": info.get("currentPrice") or info.get("regularMarketPrice"),
        "short_ratio": info.get("shortRatio"),
        "short_percent": info.get("shortPercentOfFloat"),
        "fifty_two_high": info.get("fiftyTwoWeekHigh"),
        "fifty_two_low": info.get("fiftyTwoWeekLow"),
        "beta": info.get("beta"),
        "dividend_yield": info.get("dividendYield"),
        "average_volume": info.get("averageVolume"),
        "implied_volatility": info.get("impliedVolatility"),
    }

    if settings.alpha_vantage_key:
        try:
            url = (
                "https://www.alphavantage.co/query"
                f"?function=OVERVIEW&symbol={ticker}&apikey={settings.alpha_vantage_key}"
            )
            r = requests.get(url, timeout=12)
            av = r.json() if r.ok else {}
            if isinstance(av, dict) and av.get("Symbol"):
                fundamentals["av_pe"] = _to_float(av.get("PERatio"))
                fundamentals["av_peg"] = _to_float(av.get("PEGRatio"))
                fundamentals["av_profit_margin"] = _to_float(av.get("ProfitMargin"))
                fundamentals["av_analyst_target"] = _to_float(av.get("AnalystTargetPrice"))
        except Exception:
            pass

    _save_cache(key, fundamentals)
    return fundamentals


def format_company_blurb(fundamentals: dict[str, Any] | None) -> str:
    """One-line description of what the company does."""
    if not fundamentals:
        return ""
    summary = str(fundamentals.get("business_summary") or "").strip()
    industry = str(fundamentals.get("industry") or "").strip()
    sector = str(fundamentals.get("sector") or "").strip()

    if summary:
        lead = summary.split(". ")[0].strip()
        if len(lead) > 260:
            lead = lead[:257].rstrip() + "..."
        if lead and not lead.endswith("."):
            lead += "."
        return lead

    if industry and sector and industry.lower() not in sector.lower():
        return f"Operates in {industry} ({sector})."
    if industry:
        return f"Operates in {industry}."
    if sector:
        return f"{sector} company."
    return ""


def _to_float(val: Any) -> float | None:
    try:
        if val in (None, "None", "-", ""):
            return None
        return float(val)
    except (TypeError, ValueError):
        return None


def fetch_news(ticker: str, settings: Settings = DEFAULT) -> list[dict[str, Any]]:
    ticker = ticker.strip().upper()
    key = f"news_{ticker}"
    cached = _load_cache(key, min(settings.cache_ttl_hours, 2.0))
    if isinstance(cached, list):
        return cached

    items: list[dict[str, Any]] = []

    if settings.finnhub_key:
        try:
            now = datetime.now(timezone.utc)
            frm = (now - pd.Timedelta(days=14)).strftime("%Y-%m-%d")
            to = now.strftime("%Y-%m-%d")
            url = (
                "https://finnhub.io/api/v1/company-news"
                f"?symbol={ticker}&from={frm}&to={to}&token={settings.finnhub_key}"
            )
            r = requests.get(url, timeout=12)
            if r.ok:
                for row in r.json()[:25]:
                    items.append(
                        {
                            "title": row.get("headline") or "",
                            "publisher": row.get("source") or "",
                            "ts": row.get("datetime"),
                            "url": row.get("url") or "",
                        }
                    )
        except Exception:
            pass

        try:
            url = f"https://finnhub.io/api/v1/news-sentiment?symbol={ticker}&token={settings.finnhub_key}"
            r = requests.get(url, timeout=10)
            if r.ok:
                sent = r.json()
                items.append({"_finnhub_sentiment": sent})
        except Exception:
            pass

    if not items:
        try:
            raw_news = gated(lambda: yf_ticker(ticker).news) or []
            for row in raw_news[:20]:
                content = row.get("content") if isinstance(row.get("content"), dict) else row
                title = (
                    content.get("title")
                    or row.get("title")
                    or content.get("headline")
                    or ""
                )
                publisher = ""
                provider = content.get("provider") if isinstance(content, dict) else None
                if isinstance(provider, dict):
                    publisher = provider.get("displayName") or ""
                publisher = publisher or row.get("publisher") or ""
                url = ""
                click = content.get("clickThroughUrl") if isinstance(content, dict) else None
                if isinstance(click, dict):
                    url = click.get("url") or ""
                url = url or row.get("link") or row.get("url") or ""
                items.append({"title": title, "publisher": publisher, "ts": row.get("providerPublishTime"), "url": url})
        except Exception:
            pass

    _save_cache(key, items)
    return items


def _rss_items(url: str, publisher: str, n: int = 18) -> list[dict[str, Any]]:
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0 (compatible; AIEquitiesDesk/1.0)"})
        if not r.ok or not r.content:
            return []
        import xml.etree.ElementTree as ET

        root = ET.fromstring(r.content)
        out: list[dict[str, Any]] = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            link = (item.findtext("link") or "").strip()
            out.append({"title": title, "url": link, "publisher": publisher, "geo": True})
            if len(out) >= n:
                break
        return out
    except Exception:
        return []


_GEO_HEADLINE_TERMS = (
    "war", "wars", "wartime", "conflict", "invasion", "invade", "airstrike", "airstrikes",
    "missile", "missiles", "drone strike", "bombing", "bombings", "ceasefire", "truce",
    "sanction", "sanctions", "embargo", "blockade", "tariff", "tariffs", "trade war",
    "nato", "pentagon", "kremlin", "ukraine", "russia", "gaza", "israel", "hamas",
    "hezbollah", "iran", "taiwan", "china", "beijing", "xi jinping", "south china",
    "hormuz", "red sea", "opec", "oil shock", "nuclear", "geopolit", "diplomat",
    "diplomacy", "hostage", "hostages", "coup", "military", "troops", "armed forces",
    "un security council", "white house", "state department", "defense secretary",
    "border clash", "shelling", "artillery", "refugee", "refugees", "humanitarian",
)


def _geo_relevant(title: str) -> bool:
    t = str(title or "").lower()
    if not t:
        return False
    return any(term in t for term in _GEO_HEADLINE_TERMS)


def fetch_geo_news(settings: Settings = DEFAULT) -> list[dict[str, Any]]:
    """World / geopolitical headlines from free sources (GDELT, RSS, optional Finnhub)."""
    key = "geo_news_world_v2"
    cached = _load_cache(key, min(settings.cache_ttl_hours, 2.0))
    if isinstance(cached, list) and cached:
        return cached

    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(rows: list[dict[str, Any]], *, require_geo: bool = False) -> None:
        for row in rows:
            title = (row.get("title") or "").strip()
            if not title:
                continue
            if require_geo and not _geo_relevant(title):
                continue
            k = title.lower()[:160]
            if k in seen:
                continue
            seen.add(k)
            row = dict(row)
            row["geo"] = True
            items.append(row)

    try:
        r = requests.get(
            "https://api.gdeltproject.org/api/v2/doc/doc",
            params={
                "query": (
                    '(war OR tariff OR tariffs OR sanctions OR ukraine OR gaza OR iran OR '
                    'taiwan OR "south china" OR opec OR nato OR ceasefire OR blockade OR '
                    'embargo OR "red sea" OR hormuz) sourcelang:english'
                ),
                "mode": "ArtList",
                "maxrecords": 30,
                "format": "json",
                "timespan": "3d",
            },
            timeout=12,
        )
        if r.ok:
            payload = r.json() if r.content else {}
            arts = payload.get("articles") if isinstance(payload, dict) else None
            if isinstance(arts, list):
                _add(
                    [
                        {
                            "title": a.get("title") or "",
                            "url": a.get("url") or "",
                            "publisher": a.get("sourceCountry") or a.get("domain") or "GDELT",
                        }
                        for a in arts
                    ]
                )
    except Exception:
        pass

    if settings.finnhub_key:
        try:
            r = requests.get(
                "https://finnhub.io/api/v1/news",
                params={"category": "general", "token": settings.finnhub_key},
                timeout=12,
            )
            if r.ok:
                rows = r.json() if r.content else []
                if isinstance(rows, list):
                    _add(
                        [
                            {
                                "title": row.get("headline") or "",
                                "url": row.get("url") or "",
                                "publisher": row.get("source") or "Finnhub",
                            }
                            for row in rows[:25]
                        ],
                        require_geo=True,
                    )
        except Exception:
            pass

    for url, pub in (
        ("https://feeds.bbci.co.uk/news/world/rss.xml", "BBC World"),
        ("https://news.yahoo.com/rss/world", "Yahoo World"),
        ("https://www.aljazeera.com/xml/rss/all.xml", "Al Jazeera"),
    ):
        _add(_rss_items(url, pub, n=12), require_geo=True)

    _save_cache(key, items)
    return items


def fetch_options_chain(ticker: str, settings: Settings = DEFAULT) -> dict[str, Any]:
    ticker = ticker.strip().upper()
    key = f"opt_{ticker}"
    cached = _load_cache(key, min(settings.cache_ttl_hours, 1.5))
    if isinstance(cached, dict) and cached.get("expirations"):
        return cached

    out: dict[str, Any] = {
        "expirations": [],
        "calls": pd.DataFrame(),
        "puts": pd.DataFrame(),
        "spot": None,
        "expiry": None,
        "dte": None,
    }
    try:
        t = yf_ticker(ticker)
        expirations = list(gated(lambda: t.options) or [])
        if not expirations:
            _save_cache(key, out)
            return out
        out["expirations"] = expirations
        today = pd.Timestamp.now().normalize()
        best = None
        best_diff = 10**9
        for exp in expirations:
            dte = (pd.Timestamp(exp) - today).days
            if dte < settings.options_min_dte:
                continue
            if dte > settings.options_max_dte:
                continue
            diff = abs(dte - settings.options_target_dte)
            if diff < best_diff:
                best_diff = diff
                best = exp
        if best is None:
            for exp in expirations:
                dte = (pd.Timestamp(exp) - today).days
                if dte >= 7:
                    best = exp
                    break
        if best is None:
            best = expirations[0]
        chain = t.option_chain(best)
        out["expiry"] = best
        out["dte"] = int((pd.Timestamp(best) - today).days)
        out["calls"] = chain.calls.copy() if chain.calls is not None else pd.DataFrame()
        out["puts"] = chain.puts.copy() if chain.puts is not None else pd.DataFrame()
        hist = t.history(period="5d")
        if hist is not None and not hist.empty:
            close_col = "Close" if "Close" in hist.columns else hist.columns[-2]
            out["spot"] = float(hist[close_col].iloc[-1])
    except Exception:
        pass

    _save_cache(key, out)
    return out


def fetch_recommendations(ticker: str) -> pd.DataFrame:
    try:
        rec = gated(lambda: yf_ticker(ticker).recommendations)
        if rec is None or rec.empty:
            return pd.DataFrame()
        return rec.tail(8)
    except Exception:
        return pd.DataFrame()
