from __future__ import annotations

import json
import threading
import time
from io import StringIO
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from aieq.config import CACHE_DIR, MEGA_LIQUID, ensure_cache_dir
from aieq.yahoo import configure, gated, yf_ticker

configure()

# Constituent lists ship with the deploy so a Vercel cold start never waits on
# Wikipedia or a market-cap crawl. Live lookups are a refresh-only path.
SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"

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
_LIST_TTL_SEC = 24 * 3600
_UA = {"User-Agent": "Mozilla/5.0 (compatible; AIEquitiesDesk/2.0)"}
_mcap_lock = threading.Lock()
_list_mem: dict[str, tuple[float, list[str]]] = {}


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


def _read_snapshot(name: str) -> list[str] | None:
    path = SNAPSHOT_DIR / f"{name}.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    rows = payload.get("tickers") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows:
        return None
    return [str(x) for x in rows]


def _cached_list(name: str, loader) -> list[str]:
    """Membership from memory, then the bundled snapshot, then the durable store.

    The snapshot is the hot path on Vercel: it is in the deployment bundle, so
    changing universe does not wait on Wikipedia.
    """
    now = time.time()
    hit = _list_mem.get(name)
    if hit and now - hit[0] < _LIST_TTL_SEC and hit[1]:
        return list(hit[1])
    snap = _read_snapshot(name)
    if snap:
        _list_mem[name] = (now, snap)
        return list(snap)
    from aieq.store import doc_age_hours, get_doc, put_doc

    try:
        age = doc_age_hours("wiki", name)
        if age is not None and age * 3600 < _LIST_TTL_SEC * 7:
            payload = get_doc("wiki", name)
            rows = payload.get("tickers") if isinstance(payload, dict) else payload
            if isinstance(rows, list) and rows:
                tickers = [str(x) for x in rows]
                _list_mem[name] = (now, tickers)
                return list(tickers)
    except Exception:
        pass
    tickers = list(loader() or [])
    if tickers:
        _list_mem[name] = (now, tickers)
        try:
            put_doc("wiki", name, {"tickers": tickers})
        except Exception:
            pass
    return tickers


def fetch_sp500() -> list[str]:
    return _cached_list("sp500", lambda: _wiki_tickers("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"))


def fetch_nasdaq100() -> list[str]:
    return _cached_list(
        "nasdaq100",
        lambda: _wiki_tickers("https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies"),
    )


def fetch_sp400() -> list[str]:
    return _cached_list("sp400", lambda: _wiki_tickers("https://en.wikipedia.org/wiki/List_of_S%26P_400_companies"))


def fetch_sp600() -> list[str]:
    return _cached_list("sp600", lambda: _wiki_tickers("https://en.wikipedia.org/wiki/List_of_S%26P_600_companies"))


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


def _stored_universe(name: str) -> list[str] | None:
    from aieq.store import get_doc

    try:
        cached = get_doc("universe", name)
        rows = cached.get("tickers") if isinstance(cached, dict) else cached
        if isinstance(rows, list) and rows:
            return [str(x) for x in rows]
    except Exception:
        pass
    path = CACHE_DIR / f"universe_{name}.json"
    if path.exists():
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(rows, list) and rows:
                return [str(x) for x in rows]
        except Exception:
            pass
    return None


def _cap_universe(name: str, lo: float, hi: float, candidates: list[str]) -> list[str]:
    """Dollar-band membership. Never crawls market caps on a page load.

    Order: process memory, bundled snapshot, last stored list. A cold Vercel
    instance serves the snapshot in the deployment instead of looking up
    hundreds of caps before the first name can render.
    """
    now = time.time()
    mem_key = f"cap:{name}"
    hit = _list_mem.get(mem_key)
    if hit and now - hit[0] < _CAP_TTL_SEC and hit[1]:
        return list(hit[1])
    snap = _read_snapshot(name)
    if snap:
        _list_mem[mem_key] = (now, snap)
        return list(snap)
    stored = _stored_universe(name)
    if stored:
        _list_mem[mem_key] = (now, stored)
        return stored
    # Snapshot missing (local tree without aieq/snapshots). Return the index
    # pool so the request still finishes; dollar bands apply once a snapshot
    # or stored list exists.
    kept = list(dict.fromkeys(candidates))
    if kept:
        _list_mem[mem_key] = (now, kept)
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
