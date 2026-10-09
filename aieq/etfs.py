"""ETFs expected to outrun the S&P 500, with the fund's current book.

The hurdle matches the portfolio sleeves (11.5%/yr). Trailing total-return CAGR
is persisted the same way as ``portfolios._expected_long``, without an extra
dividend add-on, because the published CAGR is already a total return.
S&P 500 trackers are not candidates: they are the hurdle, not a way past it.
Levered and inverse funds are excluded. Near-duplicate books collapse to the
stronger fund.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SPY_ANN = 0.115
CACHE_HOURS = 12.0
_SNAPSHOT = Path(__file__).resolve().parent / "snapshots" / "etfs.json"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# Equity ETFs with a long enough record to judge against the index.
# Broad-market clones of the S&P 500 (SPY, VOO, IVV) are omitted on purpose.
CANDIDATES: list[str] = [
    "QQQ", "VUG", "IWF", "SCHG", "MGK", "SPYG",
    "XLK", "VGT", "IYW", "SMH", "SOXX", "IGV",
    "XLY", "XLC", "XBI", "XLF", "XLE", "XLV", "XLI", "XLP",
    "QUAL", "MTUM", "MOAT", "FDN", "ARKK",
    "SCHD", "VIG", "VTI", "IWM", "VXUS", "EFA", "IEMG",
]


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def expected_ann(cagr_10y: float | None, cagr_5y: float | None) -> tuple[float, float]:
    """Return (annualized expected return, confidence). Rates are decimals."""
    c_hist: float | None = None
    persist = 0.0
    years = 0.0
    if cagr_10y is not None:
        c_hist = float(cagr_10y)
        persist = 0.62
        years = 10.0
    elif cagr_5y is not None:
        c_hist = float(cagr_5y)
        persist = 0.48
        years = 5.0
    if c_hist is None:
        return 0.07, 0.2
    c_hist = _clip(c_hist, -0.05, 0.40)
    base = SPY_ANN + (c_hist - SPY_ANN) * persist
    ann = _clip(SPY_ANN + (base - SPY_ANN) * 0.80, -0.08, 0.28)
    hist = _clip(years / 10.0, 0.0, 1.0)
    conf = _clip(0.55 * hist + 0.35, 0.2, 0.9)
    return ann, conf


def _num_field(blob: str, key: str) -> float | None:
    match = re.search(rf"{re.escape(key)}:(-?\d+(?:\.\d+)?)", blob)
    if not match:
        return None
    return float(match.group(1))


def _pct_field(blob: str, key: str) -> float | None:
    match = re.search(rf'{re.escape(key)}:"(-?\d+(?:\.\d+)?)%"', blob)
    if not match:
        return None
    return float(match.group(1)) / 100.0


def _money_field(blob: str, key: str) -> float | None:
    match = re.search(rf'{re.escape(key)}:"\$?([\d,.]+)([KMBT])?"', blob)
    if not match:
        return None
    num = float(match.group(1).replace(",", ""))
    mult = {None: 1.0, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[match.group(2)]
    return num * mult


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", "replace")


def parse_overview(html: str) -> dict[str, Any]:
    perf = ""
    match = re.search(r"performance:\{([^}]*)\}", html)
    if match:
        perf = match.group(1)
    cagr10 = _num_field(perf, "cagr10y")
    cagr5 = _num_field(perf, "cagr5y")
    name = ""
    titled = re.search(r'content="Get a real-time stock price for the ([A-Z0-9.\-]+) ETF \(([^)]+)\)', html)
    if titled:
        name = titled.group(2).strip()
    aum_blob = ""
    aum_at = html.find('aum:"')
    if aum_at >= 0:
        aum_blob = html[max(0, aum_at - 40) : aum_at + 420]
    return {
        "name": name,
        "cagr_10y": None if cagr10 is None else cagr10 / 100.0,
        "cagr_5y": None if cagr5 is None else cagr5 / 100.0,
        "aum": _money_field(aum_blob, "aum"),
        "expense": _pct_field(aum_blob, "expenseRatio"),
        "dividend_yield": _pct_field(aum_blob, "dividendYield"),
    }


def parse_holdings(html: str) -> dict[str, Any]:
    as_of = ""
    dated = re.search(r"As of</span>\s*([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})", html)
    if not dated:
        dated = re.search(r"Last updated:</span>\s*<span[^>]*>\s*([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})", html)
    if dated:
        as_of = dated.group(1)
    raw = re.findall(
        r'n:"((?:\\.|[^"\\])*)",s:"\$([A-Z0-9.\-]+)",as:"([0-9.]+)%"',
        html,
    )
    holdings = []
    seen: set[str] = set()
    for name, ticker, weight in raw:
        if ticker in seen:
            continue
        seen.add(ticker)
        holdings.append(
            {
                "ticker": ticker,
                "name": name.replace("\\u0026", "&"),
                "weight": round(float(weight) / 100.0, 5),
            }
        )
    sectors = []
    sector_blob = ""
    sector_at = html.find("sectors:[")
    if sector_at >= 0:
        sector_blob = html[sector_at : sector_at + 1200]
    for name, weight in re.findall(r'\{n:"([^"]+)",w:(\d*\.?\d+)\}', sector_blob):
        w = float(weight) / 100.0
        if w <= 0:
            continue
        sectors.append({"sector": name, "weight": round(w, 4)})
    sectors = [row for row in sectors if row["weight"] >= 0.005]
    sectors.sort(key=lambda row: row["weight"], reverse=True)
    return {"as_of": as_of, "n_holdings": len(holdings), "holdings": holdings[:12], "sectors": sectors}


def _overlaps(a: list[str], b: list[str]) -> float:
    left, right = set(a), set(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _dedupe(funds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop a fund when its top holdings are almost the same book as one we kept."""
    kept: list[dict[str, Any]] = []
    ranked = sorted(funds, key=lambda row: (row["exp_ann"], row.get("aum") or 0.0), reverse=True)
    for fund in ranked:
        tops = [h["ticker"] for h in fund.get("holdings") or []][:10]
        if any(_overlaps(tops, [h["ticker"] for h in other.get("holdings") or []][:10]) >= 0.72 for other in kept):
            continue
        kept.append(fund)
    return kept


def _read_snapshot() -> dict[str, Any] | None:
    if not _SNAPSHOT.is_file():
        return None
    try:
        payload = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not payload.get("funds"):
        return None
    return payload


def _fresh(payload: dict[str, Any] | None) -> bool:
    if not payload or not payload.get("funds"):
        return False
    built = str(payload.get("built_at") or "")
    try:
        when = datetime.fromisoformat(built.replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    age_h = (datetime.now(timezone.utc) - when).total_seconds() / 3600.0
    return age_h <= CACHE_HOURS


def build_etf_book() -> dict[str, Any]:
    funds: list[dict[str, Any]] = []
    for ticker in CANDIDATES:
        try:
            overview = parse_overview(_fetch(f"https://stockanalysis.com/etf/{ticker.lower()}/"))
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(0.4)
            continue
        time.sleep(0.35)
        ann, conf = expected_ann(overview.get("cagr_10y"), overview.get("cagr_5y"))
        if ann <= SPY_ANN:
            continue
        try:
            book = parse_holdings(_fetch(f"https://stockanalysis.com/etf/{ticker.lower()}/holdings/"))
        except (urllib.error.URLError, TimeoutError, OSError):
            book = {"as_of": "", "n_holdings": 0, "holdings": [], "sectors": []}
        time.sleep(0.35)
        if not book["holdings"]:
            continue
        edge = ann - SPY_ANN
        cagr = overview.get("cagr_10y") if overview.get("cagr_10y") is not None else overview.get("cagr_5y")
        horizon = "10-year" if overview.get("cagr_10y") is not None else "5-year"
        funds.append(
            {
                "ticker": ticker,
                "name": overview.get("name") or ticker,
                "exp_ann": round(ann, 4),
                "edge_vs_spy": round(edge, 4),
                "confidence": round(conf, 3),
                "cagr_10y": None if overview.get("cagr_10y") is None else round(float(overview["cagr_10y"]), 4),
                "cagr_5y": None if overview.get("cagr_5y") is None else round(float(overview["cagr_5y"]), 4),
                "aum": overview.get("aum"),
                "expense": overview.get("expense"),
                "dividend_yield": overview.get("dividend_yield"),
                "as_of": book["as_of"],
                "n_holdings": book["n_holdings"],
                "holdings": book["holdings"],
                "sectors": book["sectors"],
                "why": (
                    f"{horizon} total return was {float(cagr):.1%}/yr. "
                    f"The same persistence haircut used on the portfolio sleeves still leaves "
                    f"{ann:.1%}/yr, {edge * 100:.1f} pp above the S&P 500's {SPY_ANN:.1%} hurdle."
                ),
            }
        )
    funds = _dedupe(funds)
    return {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "built_at": datetime.now(timezone.utc).isoformat(),
        "spy_ann": SPY_ANN,
        "source": "stockanalysis",
        "n": len(funds),
        "note": (
            f"Only equity ETFs whose persisted trailing return clears the S&P 500's "
            f"{SPY_ANN:.1%}/yr hurdle are listed. Holdings and sectors are the fund's latest reported book."
        ),
        "funds": funds,
    }


def suggest_etfs(refresh: bool = False) -> dict[str, Any]:
    if not refresh:
        try:
            from aieq.store import get_doc

            stored = get_doc("etfs", "book")
            if _fresh(stored):
                return stored
        except Exception:
            stored = None
        snap = _read_snapshot()
        if snap:
            return snap
    payload = build_etf_book()
    if not payload.get("funds"):
        snap = _read_snapshot()
        if snap:
            return snap
    try:
        from aieq.store import put_doc

        put_doc("etfs", "book", payload)
    except Exception:
        pass
    return payload
