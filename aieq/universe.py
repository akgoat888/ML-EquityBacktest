from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import StringIO
from typing import Any

import pandas as pd
import requests

from aieq.config import CACHE_DIR, MEGA_LIQUID, ensure_cache_dir
from aieq.yahoo import configure, gated, yf_ticker

configure()

GLOBAL_NAMES = [
    "TSM", "ASML", "NVO", "SAP", "SHOP", "BABA", "PDD", "JD", "BIDU",
    "SONY", "TM", "UL", "BP", "SHEL", "TTE", "RIO", "BHP", "VALE", "PBR",
    "ITUB", "HDB", "IBN", "INFY", "SE", "NU", "MELI", "NGG", "DEO", "BUD",
    "NEM", "2330.TW", "005930.KS", "7203.T", "0700.HK",
    "OR.PA", "MC.PA", "SIE.DE", "AIR.PA", "NVS", "RHHBY", "AZN",
    "SNY", "GSK", "NSRGY", "TSM",
]

SMALL_CAP_MIN = 700_000_000.0
SMALL_CAP_MAX = 2_000_000_000.0
MID_CAP_MIN = 2_000_000_000.0
MID_CAP_MAX = 10_000_000_000.0
_CAP_TTL_SEC = 24 * 3600
_UA = {"User-Agent": "Mozilla/5.0 (compatible; AIEquitiesDesk/2.0)"}
_mcap_lock = threading.Lock()


def _yahoo_class_share(sym: str) -> str:
    raw = str(sym).strip().upper()
    suffixes = (".TW", ".KS", ".HK", ".L", ".PA", ".DE", ".SW", ".AX", ".TO", ".SA", ".T", ".MX")
    if any(raw.endswith(s) for s in suffixes):
        return raw
    if "." in raw:
        left, right = raw.rsplit(".", 1)
        if len(right) <= 2:
            return f"{left}-{right}"
    return raw


def _wiki_tickers(url: str, fallback: list[str] | None = None) -> list[str]:
    fallback = fallback or list(MEGA_LIQUID)

    def _from_tables(tables: list[pd.DataFrame]) -> list[str]:
        df = None
        for t in tables:
            cols = [str(c).lower() for c in t.columns]
            if any("ticker" in c or "symbol" in c for c in cols):
                df = t
                break
        if df is None:
            df = tables[0]
        col = next(
            (c for c in df.columns if "ticker" in str(c).lower() or "symbol" in str(c).lower()),
            df.columns[0],
        )
        syms = [_yahoo_class_share(x) for x in df[col].astype(str).tolist()]
        return list(dict.fromkeys(s for s in syms if s and s not in {"NAN", "NONE", "-"}))

    try:
        tables = pd.read_html(url, flavor="lxml")
        out = _from_tables(tables)
        if out:
            return out
    except Exception:
        pass
    try:
        r = requests.get(url, timeout=25, headers=_UA)
        r.raise_for_status()
        tables = pd.read_html(StringIO(r.text))
        out = _from_tables(tables)
        if out:
            return out
    except Exception:
        pass
    return list(fallback)


def fetch_sp500() -> list[str]:
    return _wiki_tickers("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")


def fetch_nasdaq100() -> list[str]:
    return _wiki_tickers("https://en.wikipedia.org/wiki/Nasdaq-100")


def fetch_sp400() -> list[str]:
    return _wiki_tickers("https://en.wikipedia.org/wiki/List_of_S%26P_400_companies")


def fetch_sp600() -> list[str]:
    return _wiki_tickers("https://en.wikipedia.org/wiki/List_of_S%26P_600_companies")


def _mcap_cache() -> dict[str, Any]:
    from aieq.store import get_doc

    try:
        payload = get_doc("meta", "market_caps")
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    path = CACHE_DIR / "market_caps.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_mcap_cache(payload: dict[str, Any]) -> None:
    from aieq.store import put_doc

    put_doc("meta", "market_caps", payload)
    try:
        (ensure_cache_dir() / "market_caps.json").write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass


def _lookup_cap(sym: str) -> float | None:
    try:
        fi = gated(lambda: getattr(yf_ticker(sym), "fast_info", None))
        raw = None
        if fi is not None:
            if isinstance(fi, dict):
                raw = fi.get("market_cap") or fi.get("marketCap")
            else:
                raw = getattr(fi, "market_cap", None) or getattr(fi, "marketCap", None)
        if raw is not None:
            cap = float(raw)
            if cap > 0:
                return cap
    except Exception:
        pass
    try:
        info = gated(lambda: yf_ticker(sym).info) or {}
        raw = info.get("marketCap") or info.get("market_cap")
        if raw is not None:
            cap = float(raw)
            if cap > 0:
                return cap
    except Exception:
        pass
    return None


def market_cap(ticker: str) -> float | None:
    sym = ticker.strip().upper()
    with _mcap_lock:
        store = _mcap_cache()
        hit = store.get(sym) or {}
        if hit.get("cap") and (time.time() - float(hit.get("ts") or 0)) < _CAP_TTL_SEC:
            return float(hit["cap"])
    cap = _lookup_cap(sym)
    if cap:
        with _mcap_lock:
            store = _mcap_cache()
            store[sym] = {"cap": cap, "ts": time.time()}
            _save_mcap_cache(store)
        return cap
    return None


def _cap_universe(name: str, lo: float, hi: float, candidates: list[str]) -> list[str]:
    from aieq.store import doc_age_hours, get_doc, put_doc

    age = doc_age_hours("universe", name)
    if age is not None and age * 3600 < _CAP_TTL_SEC:
        try:
            cached = get_doc("universe", name)
            rows = cached.get("tickers") if isinstance(cached, dict) else cached
            if isinstance(rows, list) and rows:
                return [str(x) for x in rows]
        except Exception:
            pass
    path = CACHE_DIR / f"universe_{name}.json"
    if path.exists() and (time.time() - path.stat().st_mtime) < _CAP_TTL_SEC:
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(rows, list) and rows:
                return [str(x) for x in rows]
        except Exception:
            pass
    pool = list(dict.fromkeys(candidates))
    with _mcap_lock:
        store = dict(_mcap_cache())
    now = time.time()
    known: dict[str, float] = {}
    missing: list[str] = []
    for t in pool:
        hit = store.get(t) or {}
        if hit.get("cap") and (now - float(hit.get("ts") or 0)) < _CAP_TTL_SEC:
            known[t] = float(hit["cap"])
        else:
            missing.append(t)

    def _one(sym: str) -> tuple[str, float | None]:
        return sym, _lookup_cap(sym)

    fresh: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=4) as pool_ex:
        futs = [pool_ex.submit(_one, t) for t in missing]
        for fut in as_completed(futs):
            try:
                t, cap = fut.result()
            except Exception:
                continue
            if cap:
                fresh[t] = cap
                known[t] = cap
    if fresh:
        with _mcap_lock:
            store = _mcap_cache()
            ts = time.time()
            for t, cap in fresh.items():
                store[t] = {"cap": cap, "ts": ts}
            _save_mcap_cache(store)
    kept = sorted(t for t, cap in known.items() if lo <= cap < hi)
    put_doc("universe", name, {"tickers": kept})
    try:
        (ensure_cache_dir() / f"universe_{name}.json").write_text(json.dumps(kept), encoding="utf-8")
    except OSError:
        pass
    return kept


def fetch_smallcap() -> list[str]:
    """US names with market cap $700M–$2B (S&P 400/600 pool)."""
    pool = fetch_sp600() + fetch_sp400()
    return _cap_universe("smallcap", SMALL_CAP_MIN, SMALL_CAP_MAX, pool)


def fetch_midcap() -> list[str]:
    """US names with market cap $2B–$10B (S&P 400/600 pool)."""
    pool = fetch_sp400() + fetch_sp600()
    return _cap_universe("midcap", MID_CAP_MIN, MID_CAP_MAX, pool)


def universe_tickers(name: str = "global") -> list[str]:
    key = (name or "global").strip().lower().replace("-", "").replace("_", "")
    if key in {"mega", "desk"}:
        return list(MEGA_LIQUID)
    if key in {"sp500", "s&p500", "s&p", "market", "us"}:
        extra = ["TSM", "ASML", "NVO", "SAP", "SHOP", "BABA"]
        return list(dict.fromkeys(fetch_sp500() + extra))
    if key in {"nasdaq100", "ndx", "nasdaq"}:
        return fetch_nasdaq100()
    if key in {"smallcap", "small", "smallcaps"}:
        return fetch_smallcap()
    if key in {"midcap", "mid", "midcaps"}:
        return fetch_midcap()
    return list(dict.fromkeys(list(MEGA_LIQUID) + GLOBAL_NAMES))
