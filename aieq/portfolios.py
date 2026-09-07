"""Horizon-aware model portfolios: four risk tiers per 1 / 3 / 5 / 10 year horizon.

Each name gets an expected annualised return for the chosen horizon plus a
confidence score. Short horizons lean on street targets, momentum and the desk
rating; long horizons lean on fundamental growth, quality, valuation and realised
long-run CAGR. Sleeves are then built per risk tier with sector caps and weight caps.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any
import json
import math
import time

import numpy as np
import pandas as pd

from aieq.config import CACHE_DIR, Settings
from aieq.data import fetch_fundamentals, fetch_ohlcv
from aieq.pipeline import load_board
from aieq.universe import universe_tickers

SKIP_ETFS = {
    "SPY", "QQQ", "IWM", "DIA", "GLD", "SLV", "TLT", "HYG", "SMH",
    "XLE", "XLF", "XLK", "XLV", "XLI", "XLP", "XLY", "XLB", "XLU", "XLRE",
}
# Ballast that only the Extremely-low tier may hold.
BALLAST = ["SPY", "TLT", "GLD"]
HORIZONS = (1, 3, 5, 10)
CACHE_HOURS = 6.0
ENRICH_CACHE_HOURS = 6.0
LONG_RUN_GROWTH = 0.06
MARKET_PRIOR = 0.07
BALLAST_PRIOR = {"SPY": 0.07, "TLT": 0.04, "GLD": 0.05}

TIERS: list[dict[str, Any]] = [
    {
        "id": "high",
        "label": "High risk",
        "tagline": "Concentrated growth, accepts drawdowns",
        "n": 8, "cap": 0.18, "max_sector": 3, "color": "#ff8a5b",
    },
    {
        "id": "medium",
        "label": "Medium risk",
        "tagline": "Growth with a volatility budget",
        "n": 12, "cap": 0.12, "max_sector": 3, "color": "#e3b341",
    },
    {
        "id": "low",
        "label": "Low risk",
        "tagline": "Quality compounders, lower beta",
        "n": 14, "cap": 0.10, "max_sector": 3, "color": "#6cb6ff",
    },
    {
        "id": "xlow",
        "label": "Extremely low risk",
        "tagline": "Defensive names plus index / bond / gold ballast",
        "n": 16, "cap": 0.09, "max_sector": 4, "color": "#3ddc97",
    },
]


# --------------------------------------------------------------------------- stats

def _ann_vol(close: pd.Series, n: int = 252) -> float:
    r = close.tail(n + 1).pct_change().dropna()
    if len(r) < 20:
        return 0.0
    return float(r.std(ddof=1) * math.sqrt(252))


def _ret_n(close: pd.Series, n: int) -> float | None:
    if close is None or len(close) < 8:
        return None
    n = min(int(n), len(close) - 1)
    if n < 5:
        return None
    a = float(close.iloc[-1])
    b = float(close.iloc[-1 - n])
    if b == 0:
        return None
    return a / b - 1.0


def _cagr(close: pd.Series, years: float) -> float | None:
    n = int(round(years * 252))
    if close is None or len(close) < max(60, int(n * 0.6)):
        return None
    n = min(n, len(close) - 1)
    a = float(close.iloc[-1])
    b = float(close.iloc[-1 - n])
    if b <= 0 or a <= 0:
        return None
    yrs = n / 252.0
    return (a / b) ** (1.0 / yrs) - 1.0


def _max_dd(close: pd.Series, n: int = 756) -> float | None:
    s = close.tail(n).dropna()
    if len(s) < 40:
        return None
    peak = s.cummax()
    dd = (s / peak - 1.0).min()
    return float(dd)


def _f(val: Any) -> float | None:
    try:
        if val in (None, "", "None"):
            return None
        f = float(val)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- enrich

def _enrich(row: dict[str, Any], ballast: bool = False) -> dict[str, Any] | None:
    ticker = str(row.get("ticker") or "").strip().upper()
    if not ticker:
        return None
    if ticker in SKIP_ETFS and not ballast:
        return None
    df = fetch_ohlcv(ticker, Settings(period="10y"))
    close = df["close"] if df is not None and not df.empty and "close" in df.columns else pd.Series(dtype=float)
    years_hist = len(close) / 252.0 if len(close) else 0.0
    try:
        fund = fetch_fundamentals(ticker)
    except Exception:
        fund = {}
    price = _f(row.get("price")) or (float(close.iloc[-1]) if len(close) else 0.0)
    target = _f(fund.get("target_mean"))
    street = (target / price - 1.0) if target and price else None
    rev = _f(fund.get("revenue_growth"))
    earn = _f(fund.get("earnings_growth"))
    div_y = _f(fund.get("dividend_yield")) or 0.0
    if div_y > 0.25:  # Yahoo now reports percent units (0.9 == 0.9%); no sane yield exceeds 25%
        div_y = div_y / 100.0
    div_y = _clip(div_y, 0.0, 0.12)
    out = {
        "ticker": ticker,
        "name": row.get("name") or fund.get("name") or ticker,
        "sector": row.get("sector") or fund.get("sector") or ("Ballast" if ballast else "—"),
        "price": round(price, 4) if price else None,
        "rating": row.get("rating") or "HOLD",
        "score": _f(row.get("score")),
        "confidence": _f(row.get("confidence")),
        "p_up": _f(row.get("p_up")),
        "ballast": bool(ballast),
        "vol": round(_ann_vol(close), 4) if len(close) else None,
        "vol_5y": round(_ann_vol(close, 1260), 4) if len(close) else None,
        "ret_12m": _ret_n(close, 252),
        "ret_3m": _ret_n(close, 63),
        "cagr_3y": _cagr(close, 3),
        "cagr_5y": _cagr(close, 5),
        "cagr_10y": _cagr(close, 10),
        "max_dd_3y": _max_dd(close, 756),
        "years_hist": round(years_hist, 2),
        "street_upside": street,
        "rev_growth": rev,
        "earn_growth": earn,
        "roe": _f(fund.get("roe")),
        "margin": _f(fund.get("profit_margin")),
        "fwd_pe": _f(fund.get("forward_pe")),
        "peg": _f(fund.get("peg")),
        "beta": _f(fund.get("beta")),
        "div_yield": div_y,
        "mcap": _f(fund.get("market_cap")),
    }
    for k in ("ret_12m", "ret_3m", "cagr_3y", "cagr_5y", "cagr_10y", "max_dd_3y", "street_upside",
              "rev_growth", "earn_growth", "roe", "margin", "fwd_pe", "peg", "beta"):
        if out[k] is not None:
            out[k] = round(float(out[k]), 4)
    return out


def _enrich_cache_path(universe: str):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() else "_" for c in universe.strip().lower()) or "global"
    return CACHE_DIR / f"portfolio_names_{safe}.json"


def _board_rows(universe: str) -> list[dict[str, Any]]:
    df, _, uni = load_board(universe)
    rows: list[dict[str, Any]] = []
    if df is not None and not df.empty:
        rows = df.to_dict(orient="records")
        if universe and uni and uni != universe:
            rows = []
    if rows:
        return rows
    return [{"ticker": t, "rating": "HOLD", "score": 0.0, "name": t, "sector": "—"} for t in universe_tickers(universe)]


def _enriched_names(universe: str, refresh: bool) -> list[dict[str, Any]]:
    from aieq.store import doc_age_hours, get_doc, put_doc

    path = _enrich_cache_path(universe)
    if not refresh:
        age = doc_age_hours("portfolio_names", universe)
        if age is not None and age <= ENRICH_CACHE_HOURS:
            try:
                data = get_doc("portfolio_names", universe)
                rows = data.get("names") if isinstance(data, dict) else data
                if isinstance(rows, list) and rows:
                    return rows
            except Exception:
                pass
        if path.exists():
            age_h = (time.time() - path.stat().st_mtime) / 3600.0
            if age_h <= ENRICH_CACHE_HOURS:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(data, list) and data:
                        return data
                except Exception:
                    pass
    raw = _board_rows(universe)
    jobs = [(r, False) for r in raw if str(r.get("ticker") or "").upper() not in BALLAST]
    jobs += [({"ticker": b, "name": b, "rating": "HOLD", "score": 0.0}, True) for b in BALLAST]
    out: list[dict[str, Any]] = []
    done: set[str] = set()
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(_enrich, r, b) for r, b in jobs]
        for fut in as_completed(futs):
            try:
                item = fut.result()
            except Exception:
                item = None
            if item and item["ticker"] not in done:
                done.add(item["ticker"])
                out.append(item)
    put_doc("portfolio_names", universe, {"names": out})
    try:
        path.write_text(json.dumps(out, default=str), encoding="utf-8")
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------- model

def _clip(x: float, lo: float, hi: float) -> float:
    return float(min(max(x, lo), hi))


def _horizon_weights(h: int) -> dict[str, float]:
    if h <= 1:
        return {"street": 0.36, "fund": 0.18, "mom": 0.14, "desk": 0.22, "p_up": 0.10, "quality": 0.0, "value": 0.0, "cagr": 0.0}
    if h <= 3:
        return {"street": 0.20, "fund": 0.28, "mom": 0.06, "desk": 0.10, "p_up": 0.0, "quality": 0.10, "value": 0.10, "cagr": 0.16}
    if h <= 5:
        return {"street": 0.10, "fund": 0.28, "mom": 0.04, "desk": 0.06, "p_up": 0.0, "quality": 0.15, "value": 0.15, "cagr": 0.22}
    return {"street": 0.05, "fund": 0.24, "mom": 0.0, "desk": 0.04, "p_up": 0.0, "quality": 0.20, "value": 0.20, "cagr": 0.27}


def _components(r: dict[str, Any], h: int) -> dict[str, float]:
    comp: dict[str, float] = {}
    s = r.get("street_upside")
    if s is not None:
        s = _clip(s, -0.4, 1.5)
        comp["street"] = (1.0 + s) ** (1.0 / h) - 1.0 if h > 1 else s
    g_parts = [x for x in (r.get("rev_growth"), r.get("earn_growth")) if x is not None]
    if g_parts:
        # earnings growth off a tiny base prints absurd numbers; revenue growth anchors it
        rev = r.get("rev_growth")
        earn = r.get("earn_growth")
        if rev is not None and earn is not None:
            g = 0.6 * _clip(rev, -0.3, 0.6) + 0.4 * _clip(earn, -0.4, 0.6)
        else:
            g = _clip(float(g_parts[0]), -0.3, 0.5)
        g_h = LONG_RUN_GROWTH + (g - LONG_RUN_GROWTH) * (0.75 ** (h - 1))
        comp["fund"] = 0.85 * g_h
    m = r.get("ret_12m")
    if m is not None:
        comp["mom"] = _clip(m, -0.5, 1.5) * 0.45
    sc = r.get("score")
    if sc is not None:
        comp["desk"] = _clip(sc, -1, 1) * 0.55
    p = r.get("p_up")
    if p is not None:
        comp["p_up"] = (p - 0.5) * 0.9
    q = 0.0
    roe = r.get("roe")
    mg = r.get("margin")
    if roe is not None or mg is not None:
        if roe is not None:
            q += _clip((roe - 0.10) * 0.25, -0.03, 0.04)
        if mg is not None:
            q += _clip((mg - 0.10) * 0.20, -0.03, 0.03)
        comp["quality"] = q
    pe = r.get("fwd_pe")
    if pe is not None and pe > 0:
        if pe > 35:
            v = -_clip((pe / 35.0 - 1.0) * 0.05, 0, 0.07)
        elif pe < 14:
            v = _clip((14.0 - pe) / 14.0 * 0.04, 0, 0.04)
        else:
            v = 0.0
        comp["value"] = v
    c_hist = None
    for key, need in (("cagr_10y", 10), ("cagr_5y", 5), ("cagr_3y", 3)):
        if r.get(key) is not None and need >= min(h, 5):
            c_hist = r.get(key)
            break
    if c_hist is None:
        c_hist = r.get("cagr_5y") if r.get("cagr_5y") is not None else r.get("cagr_3y")
    if c_hist is not None:
        comp["cagr"] = LONG_RUN_GROWTH + (_clip(c_hist, -0.2, 0.6) - LONG_RUN_GROWTH) * 0.5
    return comp


def _expected(r: dict[str, Any], h: int) -> tuple[float, float, dict[str, float]]:
    """Return (annualised expected return, confidence 0..1, components)."""
    w = _horizon_weights(h)
    comp = _components(r, h)
    num = den = 0.0
    for k, val in comp.items():
        wt = w.get(k, 0.0)
        if wt <= 0:
            continue
        num += wt * val
        den += wt
    ann = (num / den) if den else 0.0
    ann += float(r.get("div_yield") or 0.0)
    # Shrink toward a long-run equity prior; the further out, the less any signal is worth.
    shrink = {1: 0.60, 3: 0.42, 5: 0.32, 10: 0.22}.get(h, 0.25)
    prior = MARKET_PRIOR
    if r.get("ballast"):
        prior = BALLAST_PRIOR.get(r.get("ticker"), MARKET_PRIOR)
        shrink *= 0.5
    ann = prior + (ann - prior) * shrink
    # Soft cap: compress the excess over the prior with tanh so ordering survives.
    cap = {1: 0.60, 3: 0.32, 5: 0.26, 10: 0.20}.get(h, 0.22)
    span = cap - prior
    excess = ann - prior
    if excess > 0:
        ann = prior + span * math.tanh(excess / span)
    else:
        ann = prior + (0.25 + prior) * math.tanh(excess / (0.25 + prior))

    # confidence: completeness, signal agreement, volatility, history depth, desk confidence
    total_w = sum(v for v in w.values() if v > 0) or 1.0
    completeness = den / total_w
    vals = [comp[k] for k in comp if w.get(k, 0) > 0]
    if len(vals) >= 2:
        spread = float(np.std(vals))
        agreement = _clip(1.0 - spread / 0.35, 0.0, 1.0)
    else:
        agreement = 0.4
    vol = r.get("vol") or 0.30
    vol_conf = _clip(1.0 - (vol - 0.12) / 0.75, 0.05, 1.0)
    hist = _clip((r.get("years_hist") or 0) / max(h, 2), 0.0, 1.0)
    desk_c = r.get("confidence")
    desk_conf = _clip(desk_c, 0, 1) if desk_c is not None else 0.4
    horizon_pen = 1.0 / (1.0 + 0.05 * (h - 1))
    conf = (0.28 * completeness + 0.24 * agreement + 0.22 * vol_conf + 0.14 * hist + 0.12 * desk_conf) * horizon_pen
    if r.get("ballast"):
        conf = max(conf, 0.72 * horizon_pen + 0.15)
    return ann, _clip(conf, 0.03, 0.97), comp


def _score_names(names: list[dict[str, Any]], h: int) -> list[dict[str, Any]]:
    out = []
    for r in names:
        ann, conf, comp = _expected(r, h)
        item = dict(r)
        item["exp_ann"] = round(ann, 4)
        item["exp_total"] = round((1.0 + ann) ** h - 1.0, 4)
        item["conf"] = round(conf, 3)
        item["components"] = {k: round(v, 4) for k, v in comp.items()}
        item["why"] = _why(item, h)
        out.append(item)
    return out


def _why(r: dict[str, Any], h: int) -> str:
    bits = []
    if r.get("street_upside") is not None:
        bits.append(f"street {r['street_upside']:+.0%}")
    g = [x for x in (r.get("rev_growth"), r.get("earn_growth")) if x is not None]
    if g:
        bits.append(f"growth {float(np.mean(g)):+.0%}")
    key = "cagr_10y" if h >= 10 and r.get("cagr_10y") is not None else ("cagr_5y" if r.get("cagr_5y") is not None else "cagr_3y")
    if r.get(key) is not None:
        bits.append(f"{key.split('_')[1]} CAGR {r[key]:+.0%}")
    if r.get("roe") is not None:
        bits.append(f"ROE {r['roe']:.0%}")
    if r.get("fwd_pe"):
        bits.append(f"fwd P/E {r['fwd_pe']:.0f}")
    if r.get("div_yield"):
        bits.append(f"yield {r['div_yield']:.1%}")
    if r.get("vol") is not None:
        bits.append(f"vol {r['vol']:.0%}")
    if r.get("max_dd_3y") is not None:
        bits.append(f"3y DD {r['max_dd_3y']:.0%}")
    bits.append(f"desk {r.get('rating') or 'HOLD'}")
    return " · ".join(bits)


# --------------------------------------------------------------------------- tiers

def _tier_pool(tier: str, rows: list[dict[str, Any]], h: int) -> list[dict[str, Any]]:
    def vol(r):  # noqa: ANN001
        return r.get("vol") if r.get("vol") is not None else 0.35

    def beta(r):  # noqa: ANN001
        return r.get("beta") if r.get("beta") is not None else 1.0

    def dd(r):  # noqa: ANN001
        return r.get("max_dd_3y") if r.get("max_dd_3y") is not None else -0.4

    base = [r for r in rows if not r.get("ballast") and str(r.get("rating")) != "SELL"]
    if tier == "high":
        pool = [r for r in base if r["exp_ann"] > 0.04 and (vol(r) >= 0.32 or beta(r) >= 1.25)]
        pool.sort(key=lambda r: r["exp_ann"] * (0.6 + r["conf"]), reverse=True)
    elif tier == "medium":
        pool = [r for r in base if r["exp_ann"] > 0.03 and 0.20 <= vol(r) < 0.42 and beta(r) < 1.6]
        pool.sort(key=lambda r: r["exp_ann"] * (0.5 + r["conf"]) / max(vol(r), 0.15), reverse=True)
    elif tier == "low":
        pool = [
            r for r in base
            if r["exp_ann"] > 0.0 and vol(r) < 0.30 and beta(r) <= 1.15 and dd(r) > -0.45
            and (r.get("margin") is None or r["margin"] > 0)
        ]
        pool.sort(key=lambda r: (r["exp_ann"] + 0.5 * (r.get("div_yield") or 0)) * (0.4 + r["conf"]) / max(vol(r), 0.12) ** 1.5, reverse=True)
    else:
        pool = [
            r for r in rows
            if str(r.get("rating")) != "SELL" and r["exp_ann"] >= -0.02
            and (r.get("ballast") or (vol(r) < 0.26 and beta(r) <= 1.0 and dd(r) > -0.40))
        ]
        pool.sort(key=lambda r: (r["conf"] + (0.5 if r.get("ballast") else 0.0)) / max(vol(r), 0.08), reverse=True)
    return pool


def _relaxed_pool(tier: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    base = [r for r in rows if str(r.get("rating")) != "SELL" and r["exp_ann"] > -0.02]
    if tier == "high":
        return sorted(base, key=lambda r: r["exp_ann"], reverse=True)
    if tier == "medium":
        return sorted(base, key=lambda r: r["exp_ann"] / max(r.get("vol") or 0.3, 0.15), reverse=True)
    return sorted(base, key=lambda r: r["conf"] / max(r.get("vol") or 0.3, 0.08), reverse=True)


def _diversify(rows: list[dict[str, Any]], n: int, max_sector: int, used: set[str]) -> list[dict[str, Any]]:
    picked: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for row in rows:
        if row["ticker"] in used:
            continue
        sec = str(row.get("sector") or "—")
        if counts.get(sec, 0) >= max_sector:
            continue
        picked.append(row)
        counts[sec] = counts.get(sec, 0) + 1
        if len(picked) >= n:
            break
    if len(picked) < n:
        have = {r["ticker"] for r in picked}
        for row in rows:
            if row["ticker"] in have:
                continue
            picked.append(row)
            have.add(row["ticker"])
            if len(picked) >= n:
                break
    return picked


def _cap_weights(weights: list[float], cap: float) -> list[float]:
    w = list(weights)
    for _ in range(20):
        overflow = 0.0
        room: list[int] = []
        for i, x in enumerate(w):
            if x > cap + 1e-9:
                overflow += x - cap
                w[i] = cap
            elif x < cap - 1e-9:
                room.append(i)
        if overflow < 1e-9 or not room:
            break
        add = overflow / len(room)
        for i in room:
            w[i] += add
    s = sum(w) or 1.0
    return [x / s for x in w]


def _allocate(tier: str, rows: list[dict[str, Any]], cap: float) -> list[dict[str, Any]]:
    if not rows:
        return []
    raw: list[float] = []
    for r in rows:
        g = max(r["exp_ann"], 0.01)
        v = max(r.get("vol") or 0.3, 0.08)
        c = 0.5 + r["conf"]
        if tier == "high":
            raw.append(g * c)
        elif tier == "medium":
            raw.append(g * c / v)
        elif tier == "low":
            raw.append((g + 0.02) * c / v ** 1.5)
        else:
            raw.append(c / v ** 2 * (1.6 if r.get("ballast") else 1.0))
    total = sum(raw) or 1.0
    weights = _cap_weights([x / total for x in raw], cap)
    out = []
    for r, w in zip(rows, weights):
        item = {k: v for k, v in r.items() if k != "components"}
        item["weight"] = round(float(w), 4)
        out.append(item)
    out.sort(key=lambda x: x["weight"], reverse=True)
    return out


def _sleeve(tier: dict[str, Any], holdings: list[dict[str, Any]], h: int) -> dict[str, Any]:
    if not holdings:
        return {**{k: tier[k] for k in ("id", "label", "tagline", "color")}, "n": 0, "holdings": [],
                "exp_ann": 0, "exp_total": 0, "confidence": 0, "risk_vol": 0, "band": {}, "path": [], "thesis": "Not enough names met this tier's filters."}
    w = np.array([x["weight"] for x in holdings])
    ann = float(np.sum(w * np.array([x["exp_ann"] for x in holdings])))
    vols = np.array([x.get("vol") or 0.3 for x in holdings])
    # rough diversification: average pairwise correlation ~0.45 for equities, lower with ballast
    n_ballast = sum(1 for x in holdings if x.get("ballast"))
    rho = 0.45 - 0.15 * (n_ballast / max(len(holdings), 1))
    var = float(np.sum((w * vols) ** 2) + rho * (np.sum(w * vols) ** 2 - np.sum((w * vols) ** 2)))
    vol = math.sqrt(max(var, 0.0))
    conf = float(np.sum(w * np.array([x["conf"] for x in holdings])))
    tier_adj = {"high": 0.88, "medium": 0.95, "low": 1.0, "xlow": 1.05}[tier["id"]]
    conf = _clip(conf * tier_adj, 0.03, 0.97)
    dd = [x.get("max_dd_3y") for x in holdings if x.get("max_dd_3y") is not None]
    est_dd = float(np.average(dd, weights=[x["weight"] for x in holdings if x.get("max_dd_3y") is not None])) * 0.8 if dd else None
    total = (1.0 + ann) ** h - 1.0
    mu = math.log1p(ann)

    def _lo(yr: float) -> float:
        return math.exp(mu * yr - vol * math.sqrt(yr)) - 1.0

    def _hi(yr: float) -> float:
        return math.exp(mu * yr + vol * math.sqrt(yr)) - 1.0

    lo_total, hi_total = _lo(h), _hi(h)
    band = {
        "lo_ann": round((1.0 + lo_total) ** (1.0 / h) - 1.0, 4),
        "hi_ann": round((1.0 + hi_total) ** (1.0 / h) - 1.0, 4),
        "lo_total": round(lo_total, 4),
        "hi_total": round(hi_total, 4),
    }
    path = []
    for yr in range(0, h + 1):
        path.append({
            "year": yr,
            "expected": round(10_000 * (1.0 + ann) ** yr, 2),
            "low": round(10_000 * (1.0 + _lo(yr)), 2),
            "high": round(10_000 * (1.0 + _hi(yr)), 2),
        })
    sectors: dict[str, float] = {}
    for x in holdings:
        sectors[str(x.get("sector") or "—")] = sectors.get(str(x.get("sector") or "—"), 0.0) + x["weight"]
    top = ", ".join(f"{x['ticker']} {x['weight']:.0%}" for x in holdings[:4])
    thesis = (
        f"{tier['label']} · {h}y: {len(holdings)} names, model {ann:+.1%}/yr → {total:+.0%} total, "
        f"blended vol {vol:.0%}, confidence {conf:.0%}. Largest: {top}."
    )
    return {
        **{k: tier[k] for k in ("id", "label", "tagline", "color")},
        "n": len(holdings),
        "exp_ann": round(ann, 4),
        "exp_total": round(total, 4),
        "confidence": round(conf, 3),
        "risk_vol": round(vol, 4),
        "est_drawdown": None if est_dd is None else round(est_dd, 4),
        "band": band,
        "path": path,
        "sectors": [{"sector": k, "weight": round(v, 4)} for k, v in sorted(sectors.items(), key=lambda kv: -kv[1])],
        "holdings": holdings,
        "thesis": thesis,
    }


# --------------------------------------------------------------------------- public

def _cache_path(universe: str, years: int):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() else "_" for c in universe.strip().lower()) or "global"
    return CACHE_DIR / f"portfolios_{safe}_{int(years)}y.json"


def load_portfolios(universe: str, years: int = 1) -> dict[str, Any] | None:
    from aieq.store import doc_age_hours, get_doc

    key = f"{universe}_{int(years)}y"
    age = doc_age_hours("portfolio", key)
    if age is not None and age <= CACHE_HOURS:
        try:
            payload = get_doc("portfolio", key)
            if isinstance(payload, dict) and payload.get("sleeves"):
                return payload
        except Exception:
            pass
    path = _cache_path(universe, years)
    if not path.exists():
        return None
    if (time.time() - path.stat().st_mtime) / 3600.0 > CACHE_HOURS:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


UPGRADE_MIN_EDGE = 0.015  # swap only if the challenger beats the incumbent by ≥1.5pp/yr


def _upgrade_pass(
    tid: str,
    picks: list[dict[str, Any]],
    pool: list[dict[str, Any]],
    used: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Switch out a holding for a same-sector name that returns more at this horizon."""
    held = {p["ticker"] for p in picks}
    swaps: list[dict[str, Any]] = []
    out: list[dict[str, Any]] = []
    for p in picks:
        sector = str(p.get("sector") or "—")
        challengers = [
            c for c in pool
            if c["ticker"] not in held and c["ticker"] not in used
            and str(c.get("sector") or "—") == sector
            and c["exp_ann"] >= p["exp_ann"] + UPGRADE_MIN_EDGE
            and c["conf"] >= p["conf"] - 0.12
        ]
        if challengers:
            best = max(challengers, key=lambda c: c["exp_ann"] * (0.6 + c["conf"]))
            held.discard(p["ticker"])
            held.add(best["ticker"])
            swaps.append({
                "in": best["ticker"], "out": p["ticker"], "sector": sector,
                "in_ann": best["exp_ann"], "out_ann": p["exp_ann"],
                "reason": (
                    f"{best['ticker']} {best['exp_ann']:+.1%}/yr vs {p['ticker']} {p['exp_ann']:+.1%}/yr "
                    f"at this horizon ({sector})"
                ),
            })
            out.append(best)
        else:
            out.append(p)
    return out, swaps


def _build_sleeves(scored: list[dict[str, Any]], years: int) -> dict[str, dict[str, Any]]:
    used: set[str] = set()
    # build defensive tiers first so quality names are not consumed by the growth tiers
    order = ["xlow", "low", "medium", "high"]
    built: dict[str, dict[str, Any]] = {}
    for tid in order:
        tier = next(t for t in TIERS if t["id"] == tid)
        pool = _tier_pool(tid, scored, years)
        picks = _diversify(pool, tier["n"], tier["max_sector"], used)
        if len(picks) < max(4, tier["n"] // 2):
            relaxed = _relaxed_pool(tid, scored)
            picks = _diversify(relaxed, tier["n"], tier["max_sector"], used if len(relaxed) > tier["n"] * 2 else set())
            pool = relaxed
        picks, swaps = _upgrade_pass(tid, picks, pool, used)
        holdings = _allocate(tid, picks, tier["cap"])
        used.update(x["ticker"] for x in holdings)
        sleeve = _sleeve(tier, holdings, years)
        sleeve["upgrades"] = swaps
        built[tid] = sleeve
    return built


def _horizon_switches(
    built: dict[str, dict[str, Any]],
    prev_built: dict[str, dict[str, Any]] | None,
    scored: list[dict[str, Any]],
    years: int,
    prev_years: int | None,
) -> None:
    """Annotate each sleeve with what was switched in/out versus the shorter horizon."""
    by_ticker = {r["ticker"]: r for r in scored}
    for tid, sleeve in built.items():
        switches: list[dict[str, Any]] = list(sleeve.get("upgrades") or [])
        if prev_built and prev_years:
            prev = prev_built.get(tid) or {}
            cur_set = {h["ticker"] for h in sleeve.get("holdings") or []}
            prev_set = {h["ticker"] for h in prev.get("holdings") or []}
            ins = [t for t in cur_set - prev_set]
            outs = [t for t in prev_set - cur_set]
            already = {(s["in"], s["out"]) for s in switches}
            for t_in in sorted(ins, key=lambda t: -(by_ticker.get(t, {}).get("exp_ann") or 0)):
                r_in = by_ticker.get(t_in, {})
                sec = str(r_in.get("sector") or "—")
                match = next((t for t in outs if str(by_ticker.get(t, {}).get("sector") or "—") == sec), None)
                if match is None and outs:
                    match = outs[0]
                if match is None:
                    continue
                outs.remove(match)
                if (t_in, match) in already:
                    continue
                r_out = by_ticker.get(match, {})
                if float(r_in.get("exp_ann") or 0) < float(r_out.get("exp_ann") or 0) + 0.005:
                    continue  # a rotation for sector balance, not an upgrade
                switches.append({
                    "in": t_in, "out": match, "sector": sec,
                    "in_ann": r_in.get("exp_ann"), "out_ann": r_out.get("exp_ann"),
                    "reason": (
                        f"vs the {prev_years}y sleeve: {t_in} {float(r_in.get('exp_ann') or 0):+.1%}/yr "
                        f"replaces {match} {float(r_out.get('exp_ann') or 0):+.1%}/yr over {years}y"
                    ),
                })
        sleeve["switches"] = switches
        sleeve["vs_years"] = prev_years
        sleeve.pop("upgrades", None)


def suggest_portfolios(universe: str = "global", years: int = 1, refresh: bool = False) -> dict[str, Any]:
    universe = (universe or "global").strip().lower()
    years = int(years) if int(years) in HORIZONS else min(HORIZONS, key=lambda h: abs(h - int(years)))
    if not refresh:
        cached = load_portfolios(universe, years)
        if cached:
            return cached
    names = _enriched_names(universe, refresh)
    scored = _score_names(names, years)
    built = _build_sleeves(scored, years)
    idx = HORIZONS.index(years)
    prev_years = HORIZONS[idx - 1] if idx > 0 else None
    prev_built = _build_sleeves(_score_names(names, prev_years), prev_years) if prev_years else None
    _horizon_switches(built, prev_built, scored, years, prev_years)
    sleeves = [built[t["id"]] for t in TIERS]
    payload = {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "universe": universe,
        "years": years,
        "horizons": list(HORIZONS),
        "n_candidates": len(scored),
        "note": (
            f"{years}-year figures are model estimates blending street targets, fundamental growth, "
            "quality, valuation, realised CAGR, momentum and the desk rating — weights shift toward "
            "fundamentals as the horizon lengthens. Confidence reflects data completeness, signal "
            "agreement, volatility and history depth. Not a forecast or advice."
        ),
        "sleeves": sleeves,
    }
    from aieq.store import put_doc

    put_doc("portfolio", f"{universe}_{int(years)}y", payload)
    try:
        _cache_path(universe, years).write_text(json.dumps(payload, default=str), encoding="utf-8")
    except Exception:
        pass
    return payload
