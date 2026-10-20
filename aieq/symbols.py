from __future__ import annotations

import re
from typing import Any

import yfinance as yf

from aieq.memcache import get_mem, put_mem
from aieq.yahoo import gated, yf_ticker

_SUFFIXES = (".TW", ".KS", ".T", ".HK", ".L", ".PA", ".DE", ".SW", ".AX", ".TO", ".SA", ".MX")
_TICKER = re.compile(r"^[A-Za-z]{1,5}(?:[.\-][A-Za-z]{1,2})?$")
_LISTED = re.compile(r"^[A-Za-z0-9]{1,6}\.(?:TW|KS|T|HK|L|PA|DE|SW|AX|TO|SA|MX)$", re.I)

# Common names / local tickers -> Yahoo symbols that actually have history.
ALIASES: dict[str, str] = {
    "TSMC": "TSM",
    "TSM C": "TSM",
    "TSM": "TSM",
    "TAIWAN SEMICONDUCTOR": "TSM",
    "TAIWAN SEMICONDUCTOR MANUFACTURING": "TSM",
    "TAIWANSEMI": "TSM",
    "TAIWAN SEMI": "TSM",
    "2330": "2330.TW",
    "2330TW": "2330.TW",
    "SAMSUNG": "005930.KS",
    "005930": "005930.KS",
    "TOYOTA": "TM",
    "7203": "7203.T",
    "SONY": "SONY",
    "NESTLE": "NSRGY",
    "ASML": "ASML",
    "NOVO": "NVO",
    "NVO-B": "NVO",
    "SAP": "SAP",
    "SHELL": "SHEL",
    "BP": "BP",
    "TOTAL": "TTE",
    "LVMH": "LVMUY",
    "MC": "MC.PA",
    "TENCENT": "0700.HK",
    "BABA": "BABA",
    "ALIBABA": "BABA",
    "TSMC.TW": "2330.TW",
    "BRK.B": "BRK-B",
    "BRK.A": "BRK-A",
    "BRKB": "BRK-B",
    "BF.B": "BF-B",
    "ROG.SW": "RHHBY",
    "RO.SW": "RHHBY",
    "ROG": "RHHBY",
    "ROCHE": "RHHBY",
    "NOVN.SW": "NVS",
    "NOVN": "NVS",
    "NESN.SW": "NSRGY",
    "NESN": "NSRGY",
}

PREFERRED_EXCHANGES = {
    "NMS", "NGM", "NYQ", "NYSE", "NASDAQ", "PCX", "ASE", "BTS",
    "NCM", "WCB", "CQS",
}


def _has_history(symbol: str, min_bars: int = 30) -> bool:
    try:
        hist = gated(lambda: yf_ticker(symbol).history(period="6mo", auto_adjust=True))
        return hist is not None and len(hist) >= min_bars
    except Exception:
        return False


def _search_yahoo(query: str) -> list[dict[str, Any]]:
    key = "ysearch:" + str(query or "").strip().lower()
    hit = get_mem(key, 6 * 3600)
    if isinstance(hit, list):
        return hit
    try:
        result = gated(lambda: yf.Search(query, max_results=12))
        quotes = list(getattr(result, "quotes", None) or [])
    except Exception:
        return []
    if quotes:
        put_mem(key, quotes)
    return quotes


def _rank_quote(q: dict[str, Any]) -> tuple[int, float]:
    qtype = str(q.get("quoteType") or "").upper()
    exch = str(q.get("exchange") or q.get("exchDisp") or "").upper()
    score = float(q.get("score") or 0)
    if qtype in {"CRYPTOCURRENCY", "OPTION", "FUTURE", "INDEX"}:
        return (90, -score)
    if qtype == "ETF":
        tier = 2
    elif qtype == "EQUITY":
        tier = 0 if any(p in exch for p in PREFERRED_EXCHANGES) or exch in {"NMS", "NYQ", "NGM"} else 1
        if exch in {"PNK", "CCC", "OTC"}:
            tier = 5
    else:
        tier = 4
    return (tier, -score)


def _remember(query: str, found: tuple[str | None, list[str]]) -> tuple[str | None, list[str]]:
    if found[0]:
        put_mem("resolve:" + query.strip().upper(), found)
    return found


def _fast_symbol(raw: str) -> str | None:
    """Trust a ticker-shaped query. Company names still go through search."""
    compact = re.sub(r"\s+", "", raw)
    if not compact or " " in raw.strip():
        return None
    key = compact.upper()
    alias = ALIASES.get(raw.upper()) or ALIASES.get(key)
    if alias:
        return alias
    # "Apple" is a name. "AAPL" and "nvda" are tickers.
    if compact != compact.upper() and compact != compact.lower():
        return None
    if _LISTED.match(compact):
        return key
    if not _TICKER.match(compact):
        return None
    if "." in key and not any(key.endswith(s) for s in _SUFFIXES):
        return key.replace(".", "-")
    return key


def resolve_symbol(query: str) -> tuple[str | None, list[str]]:
    """Map a user ticker or company name to a Yahoo symbol with real history."""
    raw = (query or "").strip()
    if not raw:
        return None, []
    cached = get_mem("resolve:" + raw.upper(), 24 * 3600)
    if isinstance(cached, tuple) and cached and cached[0]:
        return cached
    fast = _fast_symbol(raw)
    if fast:
        return _remember(raw, (fast, [fast]))
    tried: list[str] = []
    key = raw.upper().replace(" ", "")

    candidates: list[str] = []
    alias = ALIASES.get(raw.upper()) or ALIASES.get(key)
    if alias:
        candidates.append(alias)
    if "TSMC" in raw.upper():
        candidates.extend(["TSM", "2330.TW"])
    candidates.append(raw.upper() if not raw.endswith(".TW") else raw)
    if raw.upper() != raw:
        candidates.append(raw)
    # Yahoo uses dashes for class shares: BRK.B -> BRK-B
    if "." in raw.upper() and not any(raw.upper().endswith(s) for s in (".TW", ".KS", ".T", ".HK", ".L", ".PA", ".DE", ".SW", ".AX", ".TO", ".SA")):
        candidates.append(raw.upper().replace(".", "-"))

    seen: set[str] = set()
    ordered: list[str] = []
    for c in candidates:
        c = c.strip()
        if not c or c in seen:
            continue
        seen.add(c)
        ordered.append(c)

    for sym in ordered:
        tried.append(sym)
        if _has_history(sym):
            return _remember(raw, (sym, tried))

    search_q = ALIASES.get(raw.upper(), raw)
    if search_q in {"TSMC", "TSM"}:
        search_q = "Taiwan Semiconductor Manufacturing"
    quotes = _search_yahoo(search_q)
    quotes = sorted(quotes, key=_rank_quote)
    for q in quotes:
        sym = str(q.get("symbol") or "").strip()
        qtype = str(q.get("quoteType") or "").upper()
        if not sym or qtype in {"CRYPTOCURRENCY", "OPTION", "FUTURE"}:
            continue
        if sym in seen:
            continue
        seen.add(sym)
        tried.append(sym)
        if _has_history(sym, min_bars=20):
            return _remember(raw, (sym, tried))

    return None, tried
