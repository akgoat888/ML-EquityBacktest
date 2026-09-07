"""Options flags, unusual volume, and financial-statement scoring."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any
import json

import numpy as np
import pandas as pd

from aieq.config import DEFAULT, Settings, ensure_cache_dir
from aieq.data import fetch_intraday, fetch_ohlcv, _month_bounds
from aieq.symbols import resolve_symbol
from aieq.yahoo import gated, yf_ticker

MIN_OPTION_PREMIUM = 1_000_000.0
MIN_DOLLAR_VOLUME = 250_000_000.0
MIN_VOL_RATIO = 2.0
MIN_BLOCK_NOTIONAL = 10_000_000.0
SENTIMENT_LABEL = {
    "HIGH BUY": "Highly bullish",
    "BUY": "Bullish",
    "HOLD": "Hold",
    "SELL": "Sell",
}


def tape_year_month(year: int | None = None, month: int | None = None) -> tuple[int, int]:
    """Yahoo option/intraday tape is only reliable for the current calendar month."""
    now = datetime.now()
    return int(year or now.year), int(month or now.month)
MAX_PRINTS = 80


def _mid(row: pd.Series) -> float:
    last = pd.to_numeric(row.get("lastPrice"), errors="coerce")
    bid = pd.to_numeric(row.get("bid"), errors="coerce")
    ask = pd.to_numeric(row.get("ask"), errors="coerce")
    if pd.notna(bid) and pd.notna(ask) and float(ask) > 0:
        return (float(bid) + float(ask)) / 2.0
    if pd.notna(last) and float(last) > 0:
        return float(last)
    if pd.notna(ask) and float(ask) > 0:
        return float(ask)
    return 0.0


def _classify_intent(last: float, bid: float, ask: float, mid: float | None = None) -> tuple[str | None, str]:
    """Last vs bid/ask mid: above → BUY, below → SELL. Falls back to stored mid if quotes are 0."""
    if last <= 0:
        return None, "No last print on the Yahoo quote."
    quote_mid = 0.0
    if bid > 0 and ask >= bid:
        quote_mid = (bid + ask) / 2.0
    elif mid is not None and mid > 0:
        quote_mid = float(mid)
    if quote_mid <= 0:
        return None, "No bid/ask (or mid) to compare against last."
    if last > quote_mid:
        return "BUY", f"Last {last:.2f} is above mid {quote_mid:.2f}."
    if last < quote_mid:
        return "SELL", f"Last {last:.2f} is below mid {quote_mid:.2f}."
    return None, f"Last {last:.2f} is exactly at mid {quote_mid:.2f}."


def _print_action(row: pd.Series) -> tuple[str | None, str]:
    last = _finite(row.get("lastPrice"))
    if last <= 0:
        last = _finite(row.get("last"))
    return _classify_intent(
        last,
        _finite(row.get("bid")),
        _finite(row.get("ask")),
        _finite(row.get("mid")) or None,
    )


def _needs_quotes(row: dict[str, Any]) -> bool:
    return _finite(row.get("last")) <= 0 or (_finite(row.get("bid")) <= 0 and _finite(row.get("ask")) <= 0)


def _chain_quote_map(ticker: str, expiry: str) -> dict[tuple[str, float], tuple[float, float, float]]:
    out: dict[tuple[str, float], tuple[float, float, float]] = {}
    try:
        chain = gated(lambda: yf_ticker(ticker).option_chain(expiry))
    except Exception:
        return out
    for side, df in (("CALL", getattr(chain, "calls", None)), ("PUT", getattr(chain, "puts", None))):
        if df is None or getattr(df, "empty", True):
            continue
        for _, row in df.iterrows():
            strike = round(_finite(row.get("strike")), 4)
            out[(side, strike)] = (
                _finite(row.get("lastPrice")),
                _finite(row.get("bid")),
                _finite(row.get("ask")),
            )
    return out


def hydrate_option_quotes(rows: list[dict[str, Any]]) -> bool:
    """Fill last/bid/ask from Yahoo for tape rows that were cached without quotes."""
    pending: dict[tuple[str, str], list[int]] = {}
    for i, row in enumerate(rows):
        if not _needs_quotes(row):
            continue
        ticker = str(row.get("ticker") or "").upper()
        expiry = str(row.get("expiry") or "")[:10]
        if ticker and len(expiry) == 10:
            pending.setdefault((ticker, expiry), []).append(i)
    if not pending:
        return False
    changed = False

    def _one(item: tuple[tuple[str, str], list[int]]):
        (ticker, expiry), idxs = item
        return ticker, expiry, idxs, _chain_quote_map(ticker, expiry)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [pool.submit(_one, item) for item in pending.items()]
        for fut in as_completed(futs):
            try:
                ticker, expiry, idxs, qmap = fut.result()
            except Exception:
                continue
            for i in idxs:
                row = rows[i]
                side = str(row.get("side") or "").upper()
                strike = round(_finite(row.get("strike")), 4)
                q = qmap.get((side, strike))
                if not q:
                    continue
                last, bid, ask = q
                row["last"] = round(last, 4)
                row["bid"] = round(bid, 4)
                row["ask"] = round(ask, 4)
                changed = True
    return changed


def _flow_rating(side: str, action: str | None, premium: float) -> str:
    """SELL put / BUY call = bullish; BUY put / SELL call = bearish."""
    if action not in {"BUY", "SELL"}:
        return "HOLD"
    bullish = (side == "CALL" and action == "BUY") or (side == "PUT" and action == "SELL")
    if bullish:
        return "HIGH BUY" if float(premium or 0) >= 10_000_000 else "BUY"
    return "SELL"


def _finite(val: Any, default: float = 0.0) -> float:
    v = pd.to_numeric(val, errors="coerce")
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if not np.isfinite(f):
        return default
    return f


def _trade_date(row: pd.Series) -> str | None:
    """Yahoo lastTradeDate for the contract (best proxy for when it last traded)."""
    raw = row.get("lastTradeDate")
    if raw is None:
        return None
    try:
        ts = pd.Timestamp(raw)
        if pd.isna(ts):
            return None
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        return ts.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return None


def flag_option_prints(
    ticker: str,
    min_premium: float = MIN_OPTION_PREMIUM,
    max_expiries: int = 8,
    year: int | None = None,
    month: int | None = None,
) -> list[dict[str, Any]]:
    """Contracts whose session premium (volume × mid × 100) is at least $1M.

    Scans expiries in the given calendar month (defaults to the current month).
    Yahoo drops prior months shortly after they expire.
    """
    sym = ticker.strip().upper()
    rows: list[dict[str, Any]] = []
    year, month = tape_year_month(year, month)
    try:
        t = yf_ticker(sym)
        expiries = list(gated(lambda: t.options) or [])
    except Exception:
        return rows
    matched = [
        e for e in expiries
        if pd.Timestamp(e).year == year and pd.Timestamp(e).month == month
    ]
    expiries = matched
    today = pd.Timestamp.now().normalize()
    for exp in expiries:
        try:
            chain = t.option_chain(exp)
        except Exception:
            continue
        dte = int((pd.Timestamp(exp) - today).days)
        for side, df in (("CALL", getattr(chain, "calls", None)), ("PUT", getattr(chain, "puts", None))):
            if df is None or df.empty:
                continue
            for _, row in df.iterrows():
                vol = _finite(row.get("volume"))
                if vol <= 0:
                    continue
                mid = _mid(row)
                if mid <= 0:
                    continue
                premium = vol * mid * 100.0
                if premium < min_premium:
                    continue
                strike = _finite(row.get("strike"))
                oi = int(_finite(row.get("openInterest")))
                iv = _finite(row.get("impliedVolatility"))
                traded = _trade_date(row)
                action, action_note = _print_action(row)
                rows.append(
                    {
                        "ticker": sym,
                        "side": side,
                        "expiry": str(exp),
                        "expiry_month": f"{pd.Timestamp(exp).year}-{pd.Timestamp(exp).month:02d}",
                        "dte": dte,
                        "strike": strike,
                        "volume": int(vol),
                        "bid": round(_finite(row.get("bid")), 4),
                        "ask": round(_finite(row.get("ask")), 4),
                        "last": round(_finite(row.get("lastPrice")), 4),
                        "mid": round(mid, 4),
                        "premium": round(premium, 2),
                        "trade_date": traded,
                        "oi": oi,
                        "oi_premium": round(oi * mid * 100.0, 2),
                        "iv": iv,
                        "action": action,
                        "action_note": action_note,
                        "flow_rating": _flow_rating(side, action, premium),
                    }
                )
    rows.sort(key=lambda r: r["premium"], reverse=True)
    return rows


def _month_slice(df: pd.DataFrame, year: int, month: int) -> pd.DataFrame:
    idx = pd.DatetimeIndex(df.index)
    return df[(idx.year == int(year)) & (idx.month == int(month))]


def large_share_prints(
    ticker: str,
    settings: Settings = DEFAULT,
    year: int | None = None,
    month: int | None = None,
) -> dict[str, Any] | None:
    """Month-to-date (or full month) share and dollar volume. Yahoo does not name the buyer."""
    year, month = tape_year_month(year, month)
    df = fetch_ohlcv(ticker, settings)
    if df is None or df.empty or "close" not in df.columns or "volume" not in df.columns:
        return None
    m = _month_slice(df, year, month)
    if m.empty:
        return None
    last = m.iloc[-1]
    close = float(last["close"])
    session_vol = float(last["volume"])
    share_mtd = float(m["volume"].sum())
    dollar_mtd = float((m["close"] * m["volume"]).sum())
    sessions = int(len(m))
    avg_daily_dollar = dollar_mtd / sessions if sessions else 0.0
    avg_vol_20 = float(df["volume"].tail(21).iloc[:-1].mean()) if len(df) > 5 else session_vol
    ratio = (session_vol / avg_vol_20) if avg_vol_20 > 0 else 0.0
    flagged = dollar_mtd >= MIN_DOLLAR_VOLUME * 8 or avg_daily_dollar >= MIN_DOLLAR_VOLUME or ratio >= MIN_VOL_RATIO
    label = datetime(year, month, 1).strftime("%b %Y")
    return {
        "ticker": ticker.strip().upper(),
        "month": f"{year}-{month:02d}",
        "month_label": label,
        "date": str(m.index[-1].date()) if hasattr(m.index[-1], "date") else str(m.index[-1]),
        "close": close,
        "volume": session_vol,
        "share_volume_mtd": share_mtd,
        "sessions": sessions,
        "avg_volume": avg_vol_20,
        "vol_ratio": round(ratio, 2),
        "dollar_volume": round(dollar_mtd, 2),
        "avg_daily_dollar": round(avg_daily_dollar, 2),
        "flagged": flagged,
        "why": (
            f"{label} volume-to-date {share_mtd:,.0f} shares / ${dollar_mtd / 1e6:.1f}M"
            f" across {sessions} sessions"
        ),
    }


def large_block_prints(
    ticker: str,
    year: int | None = None,
    month: int | None = None,
    min_notional: float = MIN_BLOCK_NOTIONAL,
    max_prints: int = MAX_PRINTS,
) -> dict[str, Any] | None:
    """Concentrated bars of at least `min_notional` (default $10M).

    Yahoo has no named tape. A 1m/5m/1h bar of $10M that closes up is treated as a
    one-time buy (bullish); a down bar is treated as a sell.
    """
    year, month = tape_year_month(year, month)
    min_notional = float(min_notional or MIN_BLOCK_NOTIONAL)
    start, end = _month_bounds(year, month)
    df, interval = fetch_intraday(ticker, start, end)
    if df is None or df.empty:
        return None
    m = _month_slice(df, year, month)
    if m.empty:
        m = df

    def _auction(ts) -> bool:
        if interval == "1d" or not hasattr(ts, "hour"):
            return False
        hm = int(ts.hour) * 60 + int(ts.minute)
        return hm <= 9 * 60 + 30 or hm >= 15 * 60 + 55

    notionals = []
    for ts, row in m.iterrows():
        if _auction(ts):
            continue
        c = float(row.get("close") or 0)
        v = float(row.get("volume") or 0)
        if c > 0 and v > 0:
            notionals.append(c * v)
    median_bar = float(np.median(notionals)) if notionals else 0.0
    prints: list[dict[str, Any]] = []
    buy_n = sell_n = 0.0
    buy_notional = sell_notional = 0.0
    for ts, row in m.iterrows():
        if _auction(ts):
            continue
        close = float(row.get("close") or 0)
        vol = float(row.get("volume") or 0)
        if close <= 0 or vol <= 0:
            continue
        notional = close * vol
        if notional < min_notional:
            continue
        open_px = float(row.get("open") or close)
        side = "BUY" if close >= open_px else "SELL"
        stamp = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        when = stamp.strftime("%Y-%m-%d %H:%M") if hasattr(stamp, "strftime") else str(stamp)
        vs = (notional / median_bar) if median_bar else 0.0
        rec = {
            "ticker": ticker.strip().upper(),
            "time": when,
            "side": side,
            "shares": int(vol),
            "open": round(open_px, 4),
            "close": round(close, 4),
            "price": round(close, 4),
            "notional": round(notional, 2),
            "interval": interval,
            "vs_median": round(vs, 2),
            "why": (
                f"${notional / 1e6:.1f}M concentrated {'buy' if side == 'BUY' else 'sell'} "
                f"in a {interval} bar"
                + (f" ({vs:.1f}× typical)" if vs else "")
            ),
        }
        prints.append(rec)
        if side == "BUY":
            buy_n += 1
            buy_notional += notional
        else:
            sell_n += 1
            sell_notional += notional
    prints.sort(key=lambda r: r["notional"], reverse=True)
    unusual_cut = max(min_notional, 2.0 * median_bar) if median_bar else min_notional
    u_buy = sum(p["notional"] for p in prints if p["side"] == "BUY" and p["notional"] >= unusual_cut)
    u_sell = sum(p["notional"] for p in prints if p["side"] == "SELL" and p["notional"] >= unusual_cut)
    u_bn = sum(1 for p in prints if p["side"] == "BUY" and p["notional"] >= unusual_cut)
    u_sn = sum(1 for p in prints if p["side"] == "SELL" and p["notional"] >= unusual_cut)
    net = u_buy - u_sell
    if net >= min_notional:
        stance, rating = "BULLISH", "BUY"
    elif net <= -min_notional:
        stance, rating = "BEARISH", "SELL"
    else:
        stance, rating = "MIXED", "HOLD"
    label = datetime(year, month, 1).strftime("%b %Y")
    largest = prints[0] if prints else None
    mtd = large_share_prints(ticker, year=year, month=month)
    why = (
        f"{label}: {int(u_bn)} unusually large buy vs {int(u_sn)} sell bars "
        f"(≥ ${unusual_cut / 1e6:.0f}M / 2× typical). Net {net / 1e6:+.1f}M. "
        + (
            f"Largest: {largest['why']} at {largest['time']}."
            if largest
            else "No bar cleared the size filter."
        )
    )
    return {
        "ticker": ticker.strip().upper(),
        "month": f"{year}-{month:02d}",
        "month_label": label,
        "interval": interval,
        "min_notional": min_notional,
        "median_bar": round(median_bar, 2),
        "n_prints": len(prints),
        "n_buy": int(u_bn),
        "n_sell": int(u_sn),
        "buy_notional": round(u_buy, 2),
        "sell_notional": round(u_sell, 2),
        "net_notional": round(net, 2),
        "stance": stance,
        "rating": rating,
        "largest_print": largest["notional"] if largest else 0,
        "flagged": bool(abs(net) >= min_notional * 8 or (largest and largest["notional"] >= min_notional * 3)),
        "why": why,
        "mtd": mtd,
        "prints": prints[: max(1, int(max_prints))],
    }


def _col_ts(col: Any) -> pd.Timestamp:
    return pd.Timestamp(col)


def _periods_from_df(df: pd.DataFrame, freq: str) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    items: list[dict[str, Any]] = []
    for c in df.columns:
        d = _col_ts(c)
        row: dict[str, Any] = {"year": int(d.year), "date": str(d.date())}
        if freq == "quarterly":
            row["quarter"] = int(d.quarter)
            row["label"] = f"Q{d.quarter} {d.year}"
        else:
            row["quarter"] = None
            row["label"] = str(d.year)
        items.append(row)
    items.sort(key=lambda x: x["date"], reverse=True)
    return items


def _default_period(periods: list[dict[str, Any]], freq: str) -> tuple[int | None, int]:
    if not periods:
        now = datetime.now()
        if freq == "quarterly":
            return int((now.month - 1) // 3 + 1), int(now.year)
        return None, int(now.year)
    top = periods[0]
    return top.get("quarter"), int(top["year"])


def _slice_for_period(
    df: pd.DataFrame,
    freq: str,
    quarter: int | None,
    year: int | None,
    context: int = 5,
) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    parsed = [(c, _col_ts(c)) for c in df.columns]
    parsed.sort(key=lambda x: x[1], reverse=True)
    y = int(year) if year is not None else parsed[0][1].year

    if freq == "annual":
        idx = next((i for i, (_, d) in enumerate(parsed) if d.year == y), None)
        if idx is None:
            return pd.DataFrame()
        cols = [parsed[i][0] for i in range(idx, min(idx + context, len(parsed)))]
        return df[cols]

    q = int(quarter) if quarter is not None else parsed[0][1].quarter
    idx = next((i for i, (_, d) in enumerate(parsed) if d.quarter == q and d.year == y), None)
    if idx is None:
        return pd.DataFrame()
    cols = [parsed[i][0] for i in range(idx, min(idx + context, len(parsed)))]
    return df[cols]


def _period_label(col: Any, freq: str = "annual") -> str:
    try:
        d = pd.Timestamp(col)
    except Exception:
        return str(col)[:10]
    if freq == "quarterly":
        return f"Q{d.quarter} '{d.year % 100:02d}"
    return str(d.date())[:10]


def _stmt_to_json(df: pd.DataFrame, freq: str = "annual") -> dict[str, Any]:
    if df is None or df.empty:
        return {"columns": [], "rows": [], "freq": freq}
    cols = [_period_label(c, freq) for c in df.columns]
    rows = []
    for idx, row in df.iterrows():
        vals = []
        for v in row.tolist():
            if v is None or (isinstance(v, float) and (pd.isna(v) or v != v)):
                vals.append(None)
            else:
                try:
                    vals.append(float(v))
                except (TypeError, ValueError):
                    vals.append(None)
        rows.append({"item": str(idx), "values": vals})
    return {"columns": cols, "rows": rows, "freq": freq}


def _find_row(stmt: dict[str, Any], *needles: str) -> list[float | None]:
    want = [n.lower() for n in needles]
    for row in stmt.get("rows") or []:
        name = str(row.get("item") or "").lower()
        if any(n in name for n in want):
            return list(row.get("values") or [])
    return []


def _chg(vals: list[float | None]) -> float | None:
    nums = [v for v in vals if v is not None]
    if len(nums) < 2:
        return None
    a, b = nums[0], nums[1]
    if b == 0:
        return None
    return (a - b) / abs(b)


def score_financials(
    income: dict[str, Any],
    balance: dict[str, Any],
    cashflow: dict[str, Any],
    freq: str = "quarterly",
) -> dict[str, Any]:
    notes: list[str] = []
    score = 0.0
    weight = 0.0
    comp = "QoQ" if freq == "quarterly" else "YoY"

    def add(delta: float, w: float, text: str) -> None:
        nonlocal score, weight
        score += delta * w
        weight += w
        notes.append(text)

    rev = _find_row(income, "total revenue", "operating revenue")
    ni = _find_row(income, "net income")
    gp = _find_row(income, "gross profit")
    ocf = _find_row(cashflow, "operating cash flow")
    fcf = _find_row(cashflow, "free cash flow")
    cash = _find_row(balance, "cash and cash equivalents", "cash cash equivalents")
    debt = _find_row(balance, "total debt", "long term debt")
    equity = _find_row(balance, "stockholders equity", "common stock equity")
    ca = _find_row(balance, "current assets")
    cl = _find_row(balance, "current liabilities")

    r = _chg(rev)
    if r is not None:
        add(1.0 if r > 0.03 else (-1.0 if r < -0.03 else 0.1), 0.22, f"Revenue {comp} {r:+.1%}.")
    n = _chg(ni)
    if n is not None:
        add(1.0 if n > 0 else -0.9, 0.18, f"Net income {comp} {n:+.1%}.")
    if ni and ni[0] is not None:
        add(0.6 if ni[0] > 0 else -0.8, 0.12, "Latest net income is positive." if ni[0] > 0 else "Latest net income is negative.")
    g = _chg(gp)
    if g is not None:
        add(0.7 if g > 0 else -0.5, 0.08, f"Gross profit {comp} {g:+.1%}.")
    if fcf and fcf[0] is not None:
        add(0.8 if fcf[0] > 0 else -0.7, 0.16, "Free cash flow is positive." if fcf[0] > 0 else "Free cash flow is negative.")
    elif ocf and ocf[0] is not None:
        add(0.6 if ocf[0] > 0 else -0.6, 0.12, "Operating cash flow is positive." if ocf[0] > 0 else "Operating cash flow is negative.")
    if ca and cl and ca[0] and cl[0] and cl[0] > 0:
        cr = ca[0] / cl[0]
        add(0.5 if cr >= 1.2 else (-0.6 if cr < 1 else 0.0), 0.08, f"Current ratio {cr:.2f}.")
    if debt and equity and debt[0] is not None and equity[0] not in (None, 0):
        de = debt[0] / abs(equity[0])
        add(-0.7 if de > 2 else (0.3 if de < 1 else -0.15), 0.08, f"Debt/equity {de:.2f}.")
    if cash and cash[0] is not None:
        add(0.2 if cash[0] > 0 else 0.0, 0.04, "Cash on the balance sheet.")

    net = score / weight if weight else 0.0
    if net >= 0.25:
        stance, label = "BULLISH", "BUY" if net < 0.45 else "HIGH BUY"
    elif net <= -0.2:
        stance, label = "BEARISH", "SELL"
    else:
        stance, label = "MIXED", "HOLD"
    thesis = f"Statement tape is {stance.lower()} (score {net:+.2f}). " + " ".join(notes[:6])
    return {
        "stance": stance,
        "rating": label,
        "score": round(float(np.clip(net, -1, 1)), 3),
        "thesis": thesis,
        "notes": notes,
    }


def _load_statement_frames(t: Any, freq: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    freq = "quarterly" if str(freq).lower().startswith("q") or str(freq).lower() == "quarterly" else "annual"
    if freq == "quarterly":
        income_attrs = ("quarterly_income_stmt", "quarterly_financials")
        cash_attrs = ("quarterly_cashflow", "quarterly_cash_flow")
        bal_attrs = ("quarterly_balance_sheet",)
    else:
        income_attrs = ("income_stmt", "financials")
        cash_attrs = ("cashflow", "cash_flow")
        bal_attrs = ("balance_sheet",)

    def _pick(attrs: tuple[str, ...]) -> pd.DataFrame:
        for attr in attrs:
            try:
                raw = getattr(t, attr, None)
                if raw is not None and isinstance(raw, pd.DataFrame) and not raw.empty:
                    return raw
            except Exception:
                continue
        return pd.DataFrame()

    return _pick(income_attrs), _pick(cash_attrs), _pick(bal_attrs)


def fetch_statements(
    ticker: str,
    freq: str = "quarterly",
    quarter: int | None = None,
    year: int | None = None,
) -> dict[str, Any]:
    queried = ticker.strip()
    resolved, tried = resolve_symbol(queried)
    if not resolved:
        raise ValueError(f"No listing for {queried!r}. Tried: {', '.join(tried) or 'nothing'}.")
    t = yf_ticker(resolved)
    freq = "quarterly" if str(freq).lower().startswith("q") or str(freq).lower() == "quarterly" else "annual"
    income_raw, cash_raw, bal_raw = _load_statement_frames(t, freq)
    available = _periods_from_df(income_raw if not income_raw.empty else bal_raw, freq)
    q_sel, y_sel = _default_period(available, freq)
    if year is not None:
        y_sel = int(year)
    if freq == "quarterly" and quarter is not None:
        q_sel = int(quarter)

    def _slice_all(q: int | None, y: int) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        return (
            _slice_for_period(income_raw, freq, q, y),
            _slice_for_period(cash_raw, freq, q, y),
            _slice_for_period(bal_raw, freq, q, y),
        )

    income, cash, bal = _slice_all(q_sel, y_sel)

    # A requested period that has not been filed yet falls back to the newest
    # statement on file rather than dead-ending the page.
    fell_back_from = None
    if income.empty and cash.empty and bal.empty and available:
        latest_q, latest_y = _default_period(available, freq)
        if (latest_q, latest_y) != (q_sel, y_sel):
            fell_back_from = f"Q{q_sel} {y_sel}" if freq == "quarterly" else f"FY {y_sel}"
            q_sel, y_sel = latest_q, latest_y
            income, cash, bal = _slice_all(q_sel, y_sel)

    if income.empty and cash.empty and bal.empty:
        hint = ", ".join(p["label"] for p in available[:8]) or "none"
        if freq == "quarterly":
            raise ValueError(
                f"No quarterly statements for Q{q_sel} {y_sel}. "
                f"Available from Yahoo: {hint}."
            )
        raise ValueError(f"No annual statements for {y_sel}. Available: {hint}.")

    inc_j = _stmt_to_json(income, freq)
    cf_j = _stmt_to_json(cash, freq)
    bs_j = _stmt_to_json(bal, freq)
    if freq == "quarterly":
        period_label = f"Q{q_sel} {y_sel}"
    else:
        period_label = f"FY {y_sel}"
    name = resolved
    try:
        info = t.info or {}
        name = info.get("shortName") or info.get("longName") or resolved
    except Exception:
        pass
    return {
        "ticker": resolved,
        "queried": queried,
        "name": name,
        "income": inc_j,
        "cashflow": cf_j,
        "balance": bs_j,
        "freq": freq,
        "quarter": q_sel,
        "year": y_sel,
        "selected": {"quarter": q_sel, "year": y_sel, "label": period_label},
        "available_periods": available[:24],
        "period_label": period_label,
        "fell_back_from": fell_back_from,
        "note": (
            f"{fell_back_from} has not been filed yet — showing {period_label}, "
            "the most recent statement on Yahoo."
            if fell_back_from
            else None
        ),
        "verdict": score_financials(inc_j, bs_j, cf_j, freq=freq),
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


def scan_option_flags(
    tickers: list[str],
    max_workers: int = 6,
    max_expiries: int = 8,
    year: int | None = None,
    month: int | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def _one(sym: str) -> list[dict[str, Any]]:
        try:
            return flag_option_prints(sym, max_expiries=max_expiries, year=year, month=month)
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = [pool.submit(_one, t) for t in tickers]
        for fut in as_completed(futs):
            rows.extend(fut.result() or [])
    rows.sort(key=lambda r: r.get("premium") or 0, reverse=True)
    return rows


def _board_rating_map(universe: str | None = None) -> dict[str, dict[str, Any]]:
    from aieq.pipeline import load_board

    out: dict[str, dict[str, Any]] = {}
    seen: list[str] = []
    for uni in (universe, "sp500", "global", "mega", "nasdaq100", "midcap", "smallcap"):
        if not uni or uni in seen:
            continue
        seen.append(uni)
        try:
            df, _, _ = load_board(uni)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        for row in df.to_dict(orient="records"):
            t = str(row.get("ticker") or "").upper()
            if t and t not in out and row.get("rating"):
                out[t] = row
    return out


def enrich_option_rows(rows: list[dict[str, Any]], universe: str | None = None) -> list[dict[str, Any]]:
    """Attach BUY/SELL from last vs bid/ask + flow sentiment (SELL put / BUY call = bullish)."""
    if not rows:
        return []
    working = [dict(row) for row in rows]
    changed = hydrate_option_quotes(working)
    board = _board_rating_map(universe)
    out = []
    for item in working:
        t = str(item.get("ticker") or "").upper()
        br = board.get(t) or {}
        side = str(item.get("side") or "").upper()
        action, note = _classify_intent(
            _finite(item.get("last")),
            _finite(item.get("bid")),
            _finite(item.get("ask")),
            _finite(item.get("mid")) or None,
        )
        item["action"] = action
        if note:
            item["action_note"] = note
        premium = float(item.get("premium") or 0)
        flow = _flow_rating(side, action, premium)
        item["flow_rating"] = flow
        item["board_rating"] = br.get("rating")
        item["rating"] = flow
        item["sentiment"] = SENTIMENT_LABEL.get(flow, flow) if action else "—"
        item["score"] = br.get("score")
        item["confidence"] = br.get("confidence")
        item["name"] = br.get("name") or item.get("name")
        item["rating_source"] = "flow"
        out.append(item)
    if changed and universe:
        extra = {}
        try:
            extra = {k: v for k, v in load_screen("options", universe).items() if k not in {"rows", "n", "as_of"}}
        except Exception:
            extra = {}
        save_screen("options", universe, out, extra=extra or None)
    return out


def option_sentiment_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"HIGH BUY": 0, "BUY": 0, "HOLD": 0, "SELL": 0}
    for row in rows:
        rating = str(row.get("rating") or "HOLD")
        if rating in counts:
            counts[rating] += 1
    return counts


def scan_volume_flags(
    tickers: list[str],
    max_workers: int = 8,
    settings: Settings | None = None,
    year: int | None = None,
    month: int | None = None,
) -> list[dict[str, Any]]:
    settings = settings or DEFAULT
    rows: list[dict[str, Any]] = []

    def _one(sym: str) -> dict[str, Any] | None:
        try:
            block = large_block_prints(sym, year=year, month=month)
            if not block:
                return large_share_prints(sym, settings, year=year, month=month)
            summary = {k: v for k, v in block.items() if k not in {"prints", "mtd"}}
            summary["prints"] = (block.get("prints") or [])[:3]
            return summary
        except Exception:
            try:
                return large_share_prints(sym, settings, year=year, month=month)
            except Exception:
                return None

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = [pool.submit(_one, t) for t in tickers]
        for fut in as_completed(futs):
            row = fut.result()
            if row:
                rows.append(row)
    rows.sort(key=lambda r: abs(r.get("net_notional") or r.get("dollar_volume") or 0), reverse=True)
    flagged = [r for r in rows if r.get("flagged")]
    return flagged or rows[:40]


def save_screen(kind: str, universe: str, rows: list[dict[str, Any]], extra: dict[str, Any] | None = None) -> None:
    from aieq.store import put_doc, utc_now

    payload = {
        "as_of": utc_now(),
        "universe": universe,
        "n": len(rows),
        "rows": rows,
    }
    if extra:
        payload.update(extra)
    put_doc("screen", f"{kind}_{universe}", payload)
    try:
        path = ensure_cache_dir() / f"{kind}_{universe}.json"
        path.write_text(json.dumps(payload, default=str), encoding="utf-8")
    except OSError:
        pass


def load_screen(kind: str, universe: str) -> dict[str, Any]:
    from aieq.store import get_doc

    empty = {"as_of": None, "universe": universe, "n": 0, "rows": []}
    try:
        payload = get_doc("screen", f"{kind}_{universe}")
        if isinstance(payload, dict) and (payload.get("rows") is not None or payload.get("n") is not None):
            payload.setdefault("universe", universe)
            payload.setdefault("rows", [])
            payload.setdefault("n", len(payload.get("rows") or []))
            return payload
    except Exception:
        pass
    path = ensure_cache_dir() / f"{kind}_{universe}.json"
    if not path.exists():
        return empty
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return empty
