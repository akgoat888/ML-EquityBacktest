"""Upcoming: solid businesses whose free cash flow grows at a steady pace.

A softer quarter stays on the list. Walmart can print positive free cash flow
in one quarter and a smaller number in the next. That dip is a flag with the
dollar change, not a reason to drop a business whose annual pace is still up.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

FUNDS = {"SPY", "QQQ", "IWM", "DIA", "SMH", "XLE", "XLF", "XLK", "GLD", "TLT"}
NOTE = (
    "Upcoming is a guide, not a brokerage. It lists operating businesses whose annual "
    "free cash flow is growing at a steady pace. A quarter that comes in lower is flagged "
    "with the dollar decline and kept as a strong buy when that annual pace is intact."
)


def _money(n: float) -> str:
    sign = "-" if n < 0 else ""
    a = abs(float(n))
    if a >= 1e12:
        return f"{sign}${a / 1e12:.1f}T"
    if a >= 1e9:
        return f"{sign}${a / 1e9:.1f}B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:.0f}M"
    return f"{sign}${a:,.0f}"


def _pairs(values: list[Any] | None, labels: list[str] | None, limit: int) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    labels = labels or []
    for i, raw in enumerate(values or []):
        if raw is None:
            continue
        try:
            num = float(raw)
        except (TypeError, ValueError):
            continue
        if num != num:
            continue
        label = str(labels[i]) if i < len(labels) and labels[i] else ""
        out.append((label, num))
        if len(out) >= limit:
            break
    return out


def _pct(n: float) -> str:
    return f"{n:+.0%}"


def _solid(quality: dict[str, Any], latest_annual: float) -> tuple[bool, str]:
    ticker = str(quality.get("ticker") or "").upper()
    quote = str(quality.get("quote_type") or "").upper()
    if ticker in FUNDS or quote in {"ETF", "MUTUALFUND", "INDEX", "MONEYMARKET"}:
        return False, "This is a fund, not an operating business."
    cap = quality.get("market_cap")
    margin = quality.get("profit_margin")
    roe = quality.get("roe")
    margin_ok = isinstance(margin, (int, float)) and float(margin) >= 0.04
    roe_ok = isinstance(roe, (int, float)) and float(roe) >= 0.10
    large = isinstance(cap, (int, float)) and float(cap) >= 10e9
    cash_business = latest_annual >= 1e9 and not isinstance(margin, (int, float)) and not isinstance(roe, (int, float))
    if isinstance(cap, (int, float)) and float(cap) < 10e9 and not cash_business:
        return False, "The business is too small for this list."
    if not (margin_ok or roe_ok or cash_business):
        return False, "Profitability is too thin to call the business solid."
    if not large and not cash_business and not (margin_ok or roe_ok):
        return False, "The business is not large enough to call solid."
    bits: list[str] = []
    if roe_ok:
        bits.append(f"return on equity is {float(roe):.0%}")
    if margin_ok:
        bits.append(f"the profit margin is {float(margin):.0%}")
    elif isinstance(margin, (int, float)) and float(margin) > 0 and roe_ok:
        bits.append(f"the profit margin is a thinner {float(margin):.0%}")
    if not bits and cash_business:
        bits.append(f"annual free cash flow is {_money(latest_annual)}")
    return True, ", and ".join(bits) if bits else "the business is profitable"


def judge_upcoming(
    annual: list[Any] | None,
    quarterly: list[Any] | None = None,
    *,
    quality: dict[str, Any] | None = None,
    annual_labels: list[str] | None = None,
    quarter_labels: list[str] | None = None,
) -> dict[str, Any]:
    """Newest-first free cash flow. A quarterly dip flags the row and keeps it."""
    quality = quality or {}
    years = _pairs(annual, annual_labels, 5)
    quarters = _pairs(quarterly, quarter_labels, 6)
    base: dict[str, Any] = {
        "included": False,
        "rating": "HOLD",
        "score": 0,
        "flagged": False,
        "flag": "",
        "pace": None,
        "fcf_annual": years[0][1] if years else None,
        "fcf_latest": quarters[0][1] if quarters else None,
        "fcf_prior": quarters[1][1] if len(quarters) > 1 else None,
        "fcf_change": None,
        "years": len(years),
        "why": "",
    }
    if len(years) < 4:
        base["why"] = "Not enough annual free cash flow history to judge the pace."
        return base
    latest_annual = years[0][1]
    if latest_annual <= 0:
        base["why"] = "The latest annual free cash flow is not positive."
        return base
    chrono = list(reversed(years))
    positive = sum(1 for _, value in years if value > 0)
    if positive < len(years) - 1:
        base["why"] = "Free cash flow is not consistently positive."
        return base
    ups = 0
    steps = len(chrono) - 1
    for i in range(1, len(chrono)):
        if chrono[i][1] > chrono[i - 1][1]:
            ups += 1
    oldest = chrono[0][1]
    cagr = None
    if oldest > 0 and latest_annual > 0 and steps > 0:
        cagr = (latest_annual / oldest) ** (1 / steps) - 1
    steady = cagr is not None and cagr > 0 and ups >= max(1, steps - 1)
    if not steady:
        base["why"] = "Annual free cash flow is not growing at a steady pace."
        base["pace"] = None if cagr is None else round(cagr, 4)
        return base
    ok, solid = _solid(quality, latest_annual)
    if not ok:
        base["why"] = solid
        base["pace"] = round(cagr, 4)
        return base

    flagged = False
    flag = ""
    change = None
    quarter_bit = "The latest reported quarter did not give back free cash flow."
    if len(quarters) >= 2:
        q_label, q_val = quarters[0]
        p_label, p_val = quarters[1]
        change = q_val - p_val
        if change < 0:
            flagged = True
            span = f" from {p_label} to {q_label}" if q_label and p_label else " from the prior quarter"
            flag = f"Free cash flow decreased by {_money(abs(change))}{span}."
            if q_val > 0:
                quarter_bit = f"{flag} The quarter is still positive."
            else:
                quarter_bit = f"{flag} The quarter turned negative."
            quarter_bit += " One quarter does not cancel a steady annual pace, so this stays a strong buy."
    pace_txt = f"Free cash flow has grown at about {_pct(cagr)} a year across {len(years)} annual reports."
    why = f"Strong buy. {pace_txt} The business is solid: {solid}. {quarter_bit}"
    return {
        "included": True,
        "rating": "HIGH BUY",
        "score": 90 if flagged else 96,
        "flagged": flagged,
        "flag": flag,
        "pace": round(float(cagr), 4),
        "fcf_annual": latest_annual,
        "fcf_latest": quarters[0][1] if quarters else None,
        "fcf_prior": quarters[1][1] if len(quarters) > 1 else None,
        "fcf_change": None if change is None else float(change),
        "years": len(years),
        "why": why,
    }


def _points(df: Any, freq: str) -> tuple[list[float], list[str]]:
    import pandas as pd

    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return [], []
    match = None
    for idx in df.index:
        if "free cash flow" in str(idx).lower():
            match = idx
            break
    if match is None:
        return [], []
    row = df.loc[match]
    if isinstance(row, pd.DataFrame):
        row = row.iloc[0]
    found: list[tuple[Any, str, float]] = []
    for col, raw in row.items():
        try:
            stamp = pd.Timestamp(col)
        except Exception:
            continue
        if raw is None or (isinstance(raw, float) and pd.isna(raw)):
            continue
        try:
            num = float(raw)
        except (TypeError, ValueError):
            continue
        if num != num:
            continue
        label = f"Q{int(stamp.quarter)} {int(stamp.year)}" if freq == "quarterly" else str(int(stamp.year))
        found.append((stamp, label, num))
    found.sort(key=lambda item: item[0], reverse=True)
    return [item[2] for item in found], [item[1] for item in found]


def _quality(ticker: str) -> dict[str, Any]:
    from aieq.data import fetch_fundamentals

    raw = fetch_fundamentals(ticker) or {}
    quote = ""
    try:
        from aieq.data import _safe_info

        quote = str((_safe_info(ticker) or {}).get("quoteType") or "")
    except Exception:
        quote = ""
    return {
        "ticker": ticker,
        "market_cap": raw.get("market_cap"),
        "profit_margin": raw.get("profit_margin"),
        "roe": raw.get("roe"),
        "quote_type": quote,
        "name": raw.get("name") or ticker,
    }


def analyze_symbol(ticker: str) -> dict[str, Any]:
    from aieq.memcache import get_mem, put_mem
    from aieq.screens import _load_statement_frames
    from aieq.symbols import resolve_symbol
    from aieq.yahoo import yf_ticker

    queried = ticker.strip()
    resolved, tried = resolve_symbol(queried)
    if not resolved:
        raise ValueError(f"No listing for {queried!r}. Tried: {', '.join(tried) or 'nothing'}.")
    key = f"upcoming_v1_{resolved}"
    hit = get_mem(key, 6 * 3600)
    if isinstance(hit, dict):
        out = dict(hit)
        out["queried"] = queried
        return out
    client = yf_ticker(resolved)
    _, annual_cf, _ = _load_statement_frames(client, "annual")
    _, quarter_cf, _ = _load_statement_frames(client, "quarterly")
    annual, annual_labels = _points(annual_cf, "annual")
    quarterly, quarter_labels = _points(quarter_cf, "quarterly")
    quality = _quality(resolved)
    row = judge_upcoming(
        annual,
        quarterly,
        quality=quality,
        annual_labels=annual_labels,
        quarter_labels=quarter_labels,
    )
    row["ticker"] = resolved
    row["queried"] = queried
    row["name"] = quality.get("name") or resolved
    put_mem(key, row)
    return row


def _one(ticker: str) -> dict[str, Any]:
    try:
        return analyze_symbol(ticker)
    except Exception as exc:
        return {"ticker": ticker, "included": False, "why": str(exc), "rating": "HOLD", "flagged": False}


def scan_universe(name: str, *, cursor: int = 0, limit: int = 4) -> dict[str, Any]:
    from aieq.universe import universe_tickers

    tickers = universe_tickers(name or "mega")
    total = len(tickers)
    cursor = max(0, int(cursor))
    limit = min(6, max(1, int(limit)))
    batch = tickers[cursor : cursor + limit]
    rows: list[dict[str, Any]] = []
    if batch:
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(_one, ticker): ticker for ticker in batch}
            by_ticker: dict[str, dict[str, Any]] = {}
            for future in as_completed(futures):
                ticker = futures[future]
                try:
                    by_ticker[ticker] = future.result()
                except Exception as exc:
                    by_ticker[ticker] = {"ticker": ticker, "included": False, "why": str(exc)}
        rows = [by_ticker[ticker] for ticker in batch if by_ticker.get(ticker, {}).get("included")]
    next_cursor = cursor + len(batch)
    return {
        "universe": name or "mega",
        "cursor": cursor,
        "next_cursor": next_cursor,
        "total": total,
        "done": next_cursor >= total,
        "n": len(rows),
        "checked": len(batch),
        "rows": rows,
        "note": NOTE,
    }
