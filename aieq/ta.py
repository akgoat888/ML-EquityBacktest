"""Fibonacci swing signals confirmed by the 50-day, the 200-day, and volume.

A buy is an uptrend pullback into the 38.2–61.8% retracement of the last
confirmed rally, with price still above the 200-day and volume not distributing.
A sell is a downtrend rally into that same band, or a break of the swing low
that also loses the 50-day. Extension prints on climax volume are trims.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import numpy as np
import pandas as pd

from aieq.config import Settings

FIB_RETRACEMENTS: tuple[tuple[float, str], ...] = (
    (0.0, "0%"),
    (0.236, "23.6%"),
    (0.382, "38.2%"),
    (0.5, "50%"),
    (0.618, "61.8%"),
    (0.786, "78.6%"),
    (1.0, "100%"),
)
FIB_EXTENSIONS: tuple[tuple[float, str], ...] = (
    (1.272, "127.2%"),
    (1.618, "161.8%"),
)
PERIODS = {"2y", "5y", "10y", "max"}
NOTE = (
    "Buy and sell come from the last confirmed swing. "
    "A buy needs price above the 200-day, the 50-day above the 200-day, "
    "and price in the 38.2–61.8% retracement with volume not distributing. "
    "A sell needs the mirror image in a downtrend, or a lost swing low under the 50-day. "
    "This is a rules signal, not a guarantee."
)


def _finite(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(out):
        return None
    return out


def _round(value: Any, places: int = 4) -> float | None:
    num = _finite(value)
    if num is None:
        return None
    return round(num, places)


def _atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = 14) -> np.ndarray:
    prev = np.roll(close, 1)
    prev[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))
    out = np.full(len(close), np.nan)
    if len(close) < n:
        return out
    out[n - 1] = float(np.mean(tr[:n]))
    alpha = 1.0 / n
    for i in range(n, len(close)):
        out[i] = out[i - 1] + alpha * (tr[i] - out[i - 1])
    return out


def _sma(values: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(values), np.nan)
    if n <= 0 or len(values) < n:
        return out
    csum = np.cumsum(values)
    out[n - 1] = csum[n - 1] / n
    if len(values) > n:
        out[n:] = (csum[n:] - csum[:-n]) / n
    return out


def zigzag_pivots(high: np.ndarray, low: np.ndarray, atr: np.ndarray) -> list[tuple[int, float, str]]:
    """Confirmed swing highs and lows. A pivot prints only after a reversal."""
    n = len(high)
    start = next((i for i in range(n) if np.isfinite(atr[i]) and atr[i] > 0), None)
    if start is None or n - start < 30:
        return []

    def need(i: int, price: float) -> float:
        raw = atr[i] if np.isfinite(atr[i]) and atr[i] > 0 else atr[start]
        return max(1.25 * float(raw), abs(price) * 0.025)

    pivots: list[tuple[int, float, str]] = []
    hi_i, hi = start, float(high[start])
    lo_i, lo = start, float(low[start])
    direction = 0
    ext_i = start
    ext = float(high[start])

    for i in range(start + 1, n):
        h = float(high[i])
        l = float(low[i])
        if direction == 0:
            if h >= hi:
                hi, hi_i = h, i
            if l <= lo:
                lo, lo_i = l, i
            if hi - lo >= need(i, (hi + lo) / 2.0) and hi_i != lo_i:
                if lo_i < hi_i:
                    pivots.append((lo_i, lo, "L"))
                    direction = 1
                    ext, ext_i = hi, hi_i
                else:
                    pivots.append((hi_i, hi, "H"))
                    direction = -1
                    ext, ext_i = lo, lo_i
            continue
        if direction == 1:
            if h >= ext:
                ext, ext_i = h, i
            elif ext - l >= need(i, ext) and i > ext_i:
                pivots.append((ext_i, ext, "H"))
                direction = -1
                ext, ext_i = l, i
        elif l <= ext:
            ext, ext_i = l, i
        elif h - ext >= need(i, ext) and i > ext_i:
            pivots.append((ext_i, ext, "L"))
            direction = 1
            ext, ext_i = h, i
    return pivots


def _impulses(pivots: list[tuple[int, float, str]], bull: bool) -> list[tuple[tuple[int, float, str], tuple[int, float, str]]]:
    found = []
    want_end = "H" if bull else "L"
    want_start = "L" if bull else "H"
    for i, pivot in enumerate(pivots):
        if pivot[2] != want_end:
            continue
        for j in range(i - 1, -1, -1):
            if pivots[j][2] == want_start:
                found.append((pivots[j], pivot))
                break
    return found


def _pick_impulse(
    pivots: list[tuple[int, float, str]],
    *,
    bull: bool,
    price: float,
    atr: float,
) -> tuple[tuple[int, float, str], tuple[int, float, str]] | None:
    floor = max(price * 0.05, 2.5 * atr)
    soft = max(price * 0.035, 1.5 * atr)
    legs = _impulses(pivots, bull)
    for start, end in reversed(legs):
        if abs(end[1] - start[1]) >= floor and end[0] > start[0]:
            return start, end
    for start, end in reversed(legs):
        if abs(end[1] - start[1]) >= soft and end[0] > start[0]:
            return start, end
    return None


def fib_price(start: float, end: float, ratio: float, *, bull: bool) -> float:
    """Map a Fibonacci ratio onto price.

    In an uptrend, 0% is the swing high and 100% is the swing low.
    Extensions sit beyond the high. The downtrend is the mirror.
    """
    span = abs(end - start)
    if bull:
        high = end
        if ratio <= 1:
            return high - ratio * span
        return high + (ratio - 1.0) * span
    low = end
    if ratio <= 1:
        return low + ratio * span
    return low - (ratio - 1.0) * span


def _volume_state(
    close: np.ndarray,
    volume: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    *,
    impulse_start: int,
    impulse_end: int,
    bull: bool,
) -> dict[str, Any]:
    vol = np.asarray(volume, dtype=float)
    vol_sma = _sma(np.where(np.isfinite(vol), vol, 0.0), 20)
    last_vol = _finite(vol[-1]) if len(vol) else None
    base = _finite(vol_sma[-1]) if len(vol_sma) else None
    ratio = (last_vol / base) if last_vol is not None and base not in (None, 0.0) else None

    chg = np.diff(close)
    recent_v = vol[1:][-10:]
    recent_c = chg[-10:]
    up = float(recent_v[recent_c > 0].sum()) if len(recent_v) else 0.0
    down = float(recent_v[recent_c < 0].sum()) if len(recent_v) else 0.0
    ud = (up / down) if down > 0 else (None if up == 0 else 9.9)

    imp = vol[impulse_start : impulse_end + 1]
    ret = vol[impulse_end + 1 :]
    imp = imp[np.isfinite(imp) & (imp > 0)]
    ret = ret[np.isfinite(ret) & (ret > 0)]
    dryup = float(np.mean(ret) / np.mean(imp)) if len(imp) and len(ret) >= 2 and np.mean(imp) > 0 else None

    bar_range = float(high[-1] - low[-1])
    loc = ((float(close[-1]) - float(low[-1])) / bar_range) if bar_range > 0 else 0.5
    climax = ratio is not None and ratio >= 2.0
    # A retracement is a run of red days, so up/down volume alone is not
    # distribution. Distribution is heavier volume on that leg than on the impulse.
    heavy = dryup is not None and dryup >= 1.25
    light = dryup is not None and dryup <= 0.9
    state = "Neutral"
    if climax and loc <= 0.28:
        state = "Climax down"
    elif climax and loc >= 0.72:
        state = "Climax up"
    elif light:
        state = "Dry pullback"
    elif heavy or (dryup is None and ud is not None and ud <= 0.8):
        state = "Distribution"
    elif ud is not None and ud >= 1.25:
        state = "Accumulation"
    return {
        "volume_state": state,
        "volume_ratio": _round(ratio, 2),
        "up_down_volume": _round(ud, 2),
        "pullback_volume": _round(dryup, 2),
        "close_loc": _round(loc, 2),
    }


def _ladder(start: float, end: float, *, bull: bool, volume_state: str, close: float) -> list[dict[str, Any]]:
    """Buy/sell label on every Fibonacci price, before the live filters."""
    if bull:
        roles = {
            0.0: ("HOLD", "Swing high. Resistance until price is back through it."),
            0.236: ("HOLD", "Shallow pullback. Not a buy."),
            0.382: ("BUY", "First buy of the 38.2–61.8% zone."),
            0.5: ("BUY", "Mid buy of the 38.2–61.8% zone."),
            0.618: ("BUY", "Golden buy. Preferred retracement."),
            0.786: ("BUY", "Last buy. Off if volume is distributing."),
            1.0: ("SELL", "Swing low. A close below is invalidation."),
            1.272: ("SELL", "127.2% extension. Trim into strength."),
            1.618: ("SELL", "161.8% extension. Trim into strength."),
        }
    else:
        roles = {
            0.0: ("HOLD", "Swing low. Do not chase the breakdown."),
            0.236: ("HOLD", "Bounce has not reached supply."),
            0.382: ("SELL", "First sell of the 38.2–61.8% rally."),
            0.5: ("SELL", "Mid sell of the 38.2–61.8% rally."),
            0.618: ("SELL", "Golden sell. Preferred rally to fade."),
            0.786: ("HOLD", "Bounce is deep. Selling here is late."),
            1.0: ("BUY", "Prior swing high. A close back through it cancels the downswing."),
            1.272: ("HOLD", "127.2% extension under the low. Downside target, not a fresh short."),
            1.618: ("HOLD", "161.8% extension under the low. Downside target, not a fresh short."),
        }
    buy_off = bull and volume_state == "Distribution"
    sell_off = (not bull) and volume_state == "Climax up"
    rows = []
    for ratio, label in FIB_RETRACEMENTS + FIB_EXTENSIONS:
        action, role = roles[ratio]
        note = role
        if buy_off and action == "BUY":
            action = "HOLD"
            note = "Listed as a buy, but volume is distributing so the level is off."
        elif sell_off and action == "SELL":
            action = "HOLD"
            note = "Listed as a sell, but the rally is a buying climax so the level is off."
        price = fib_price(start, end, ratio, bull=bull)
        kind = "extension" if ratio > 1 else "retracement"
        dist = ((price - close) / close) if close else None
        rows.append(
            {
                "ratio": ratio,
                "label": label,
                "price": _round(price, 4),
                "action": action,
                "kind": kind,
                "distance": _round(dist, 4),
                "note": note,
                "active": False,
            }
        )
    if rows:
        nearest = min(rows, key=lambda row: abs((row["price"] or 0) - close))
        nearest["active"] = True
    return rows


def _zone_name(ratio: float | None) -> str:
    if ratio is None:
        return "—"
    if ratio < 0:
        return "Above the swing"
    if ratio < 0.236:
        return "0–23.6% shallow"
    if ratio < 0.382:
        return "23.6–38.2%"
    if ratio <= 0.618:
        return "38.2–61.8% golden"
    if ratio <= 0.786:
        return "61.8–78.6% deep"
    if ratio <= 1:
        return "78.6–100% last stand"
    return "Beyond the swing"


def decide_signal(
    *,
    close: float,
    atr: float,
    ma50: float | None,
    ma200: float | None,
    ratio: float | None,
    volume_state: str,
    bars: int,
    span: float | None,
) -> dict[str, Any]:
    """Confluence of Fibonacci zone, 50/200 stack, and volume."""
    notes: list[str] = []
    if bars < 60 or ma50 is None or ratio is None or span is None or span <= 0:
        return {
            "action": "HOLD",
            "rating": "HOLD",
            "trend": "UNKNOWN",
            "zone": _zone_name(ratio),
            "confidence": 20,
            "why": "Not enough history for a 50-day, a 200-day, and a confirmed swing.",
        }

    above50 = close > ma50
    above200 = ma200 is not None and close > ma200
    golden = ma200 is not None and ma50 > ma200
    death = ma200 is not None and ma50 < ma200
    if ma200 is None:
        trend = "MIXED"
        notes.append("200-day is not ready. No high-conviction call.")
    elif golden and above200:
        trend = "UP"
    elif death and not above200:
        trend = "DOWN"
    else:
        trend = "MIXED"

    pad = 0.35 * atr if atr and atr > 0 else abs(close) * 0.004
    # ratio bands, with a small pad expressed in ratio space via ATR / span
    pad_r = (pad / span) if span else 0.0
    golden_zone = ratio is not None and (0.382 - pad_r) <= ratio <= (0.618 + pad_r)
    deep_zone = ratio is not None and (0.618 + pad_r) < ratio <= (0.786 + pad_r)
    broken = ratio is not None and ratio > (1.0 + pad_r)
    extended = ratio is not None and ratio < 0.236
    through_extension = ratio is not None and ratio < -0.2

    vol_blocks_buy = volume_state in {"Distribution", "Climax down"}
    vol_confirms_buy = volume_state in {"Dry pullback", "Accumulation", "Climax up"}
    vol_blocks_sell = volume_state == "Climax up"
    vol_confirms_sell = volume_state in {"Dry pullback", "Distribution", "Neutral"}

    action = "HOLD"
    rating = "HOLD"

    if trend == "UP":
        if broken and not above50:
            action, rating = "SELL", "SELL"
            notes.append("Lost the swing low and the 50-day. The rally structure is broken.")
        elif broken:
            notes.append("Pushed through the 100% level, but the 50-day still holds.")
        elif golden_zone and not vol_blocks_buy:
            action = "BUY"
            strong = golden and vol_confirms_buy and 0.45 <= ratio <= 0.68
            rating = "HIGH BUY" if strong else "BUY"
            notes.append("Uptrend pullback into the 38.2–61.8% buy zone.")
        elif golden_zone and vol_blocks_buy:
            notes.append("Price is in the buy zone, but volume is distributing. Stand aside.")
        elif deep_zone and volume_state == "Distribution" and not above50:
            action, rating = "SELL", "SELL"
            notes.append("Deep retracement on distribution and price is under the 50-day.")
        elif deep_zone and not vol_blocks_buy and above200:
            action, rating = "BUY", "BUY"
            notes.append("Last-chance buy between 61.8% and 78.6%, still above the 200-day.")
        elif through_extension and volume_state == "Climax up":
            action, rating = "SELL", "SELL"
            notes.append("Through the 127.2% extension on climax volume. Trim.")
        elif extended:
            notes.append("Uptrend, but price has not pulled back to the 38.2% buy level.")
        else:
            notes.append("Uptrend. Wait for the 38.2–61.8% zone.")
    elif trend == "DOWN":
        if broken and above50 and above200:
            action, rating = "BUY", "BUY"
            notes.append("Reclaimed the swing high, the 50-day, and the 200-day.")
        elif broken and above50:
            notes.append("Reclaimed the swing high, still under the 200-day.")
        elif golden_zone and not vol_blocks_sell:
            action = "SELL"
            strong = death and (not above50) and vol_confirms_sell and 0.45 <= ratio <= 0.68
            rating = "HIGH SELL" if strong else "SELL"
            notes.append("Downtrend rally into the 38.2–61.8% sell zone.")
        elif golden_zone and vol_blocks_sell:
            notes.append("Price is in the sell zone, but the rally is a buying climax. Stand aside.")
        elif volume_state == "Climax down" and extended:
            notes.append("Selling climax near the lows. Do not chase the short.")
        elif extended:
            notes.append("Downtrend. Wait for a rally into the 38.2–61.8% sell zone.")
        else:
            notes.append("Downtrend. The bounce is outside the sell zone.")
    else:
        if golden_zone and above200 and above50 and not vol_blocks_buy and ma200 is not None:
            action, rating = "BUY", "BUY"
            notes.append("At a Fibonacci buy level and above both averages, but the 50-day has not cleared the 200-day.")
        elif golden_zone and ma200 is not None and not above200 and not above50 and not vol_blocks_sell:
            action, rating = "SELL", "SELL"
            notes.append("Under both averages and into Fibonacci supply.")
        else:
            notes.append("The 50-day and 200-day disagree. No trade until they stack with a Fibonacci level.")

    if action == "BUY" and ma200 is not None and not above200:
        action, rating = "HOLD", "HOLD"
        notes.append("Fibonacci says buy, but price is under the 200-day.")
    if rating == "HIGH BUY" and not (golden and above200 and vol_confirms_buy):
        rating = "BUY"
    if action == "SELL" and golden and above200 and not (broken and not above50) and volume_state != "Climax up":
        action, rating = "HOLD", "HOLD"
        notes.append("Sell was vetoed: the 50-day is still above the 200-day and price is above the 200-day.")
    if rating == "HIGH SELL" and not (death and not above200 and vol_confirms_sell):
        rating = "SELL"

    vol_note = {
        "Dry pullback": "Volume dried up on the retracement.",
        "Accumulation": "Up-day volume is leading.",
        "Distribution": "Down-day volume is leading.",
        "Climax up": "Latest bar is a buying climax.",
        "Climax down": "Latest bar is a selling climax.",
        "Neutral": "Volume is neutral.",
    }.get(volume_state)
    if vol_note:
        notes.append(vol_note)

    checks = [
        trend in {"UP", "DOWN"},
        bool(golden_zone or (action == "SELL" and broken) or (action == "BUY" and deep_zone)),
        (action == "BUY" and vol_confirms_buy)
        or (action == "SELL" and vol_confirms_sell)
        or (action == "HOLD" and volume_state in {"Neutral", "Distribution", "Climax up", "Climax down"}),
        ma200 is not None and bars >= 200,
        span >= close * 0.08,
    ]
    confidence = int(round(100 * sum(1 for item in checks if item) / len(checks)))
    if action == "HOLD":
        confidence = min(confidence, 55)
    elif rating in {"HIGH BUY", "HIGH SELL"}:
        confidence = max(confidence, 75)

    score_base = {"HIGH BUY": 90, "BUY": 70, "HOLD": 50, "SELL": 30, "HIGH SELL": 10}[rating]
    return {
        "action": action,
        "rating": rating,
        "trend": trend,
        "zone": _zone_name(ratio),
        "confidence": confidence,
        "score": score_base + confidence / 100.0,
        "why": " ".join(notes),
    }


def _dates(index: pd.Index) -> list[str]:
    if isinstance(index, pd.DatetimeIndex):
        return [ts.strftime("%Y-%m-%d") for ts in index]
    return [str(item)[:10] for item in index]


def analyze_frame(frame: pd.DataFrame, *, with_candles: bool = False) -> dict[str, Any]:
    """Signal for one OHLCV frame. Missing inputs come back as HOLD, not an exception."""
    empty = {
        "ok": False,
        "action": "HOLD",
        "rating": "HOLD",
        "score": 50.0,
        "confidence": 0,
        "trend": "UNKNOWN",
        "zone": "—",
        "why": "No price history.",
        "levels": [],
        "close": None,
        "sma_50": None,
        "sma_200": None,
        "dist_sma_50": None,
        "dist_sma_200": None,
        "ma_state": "—",
        "volume_state": "Neutral",
        "volume_ratio": None,
        "fib_ratio": None,
        "swing_low": None,
        "swing_high": None,
        "swing_low_on": None,
        "swing_high_on": None,
        "atr": None,
        "bars": 0,
        "as_of": None,
    }
    if frame is None or frame.empty:
        return empty
    needed = {"open", "high", "low", "close"}
    cols = {str(c).lower() for c in frame.columns}
    if not needed.issubset(cols):
        empty["why"] = "Price history is missing open, high, low, or close."
        return empty

    df = frame.copy()
    df.columns = [str(c).lower() for c in df.columns]
    for col in ("open", "high", "low", "close", "volume"):
        if col not in df.columns:
            df[col] = np.nan
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["high", "low", "close"])
    df = df[(df["close"] > 0) & (df["high"] > 0) & (df["low"] > 0)]
    if df.empty:
        return empty

    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    volume = df["volume"].to_numpy(dtype=float)
    dates = _dates(df.index)
    atr = _atr(high, low, close)
    ma50 = _sma(close, 50)
    ma200 = _sma(close, 200)
    last = float(close[-1])
    last_atr = _finite(atr[-1]) or last * 0.02
    last_ma50 = _finite(ma50[-1])
    last_ma200 = _finite(ma200[-1])

    bull_bias = True
    if last_ma50 is not None and last_ma200 is not None:
        bull_bias = not (last_ma50 < last_ma200 and last < last_ma200)
        if last_ma50 > last_ma200 and last > last_ma200:
            bull_bias = True
    pivots = zigzag_pivots(high, low, atr)
    picked = _pick_impulse(pivots, bull=bull_bias, price=last, atr=last_atr)
    if picked is None:
        picked = _pick_impulse(pivots, bull=not bull_bias, price=last, atr=last_atr)
        if picked is not None:
            bull_bias = not bull_bias

    levels: list[dict[str, Any]] = []
    ratio = None
    swing_low = swing_high = None
    swing_low_on = swing_high_on = None
    span = None
    vol = {
        "volume_state": "Neutral",
        "volume_ratio": None,
        "up_down_volume": None,
        "pullback_volume": None,
    }
    if picked is not None:
        start, end = picked
        span = abs(end[1] - start[1])
        if bull_bias:
            swing_low, swing_high = start[1], end[1]
            swing_low_on, swing_high_on = dates[start[0]], dates[end[0]]
            ratio = (end[1] - last) / span if span else None
        else:
            swing_high, swing_low = start[1], end[1]
            swing_high_on, swing_low_on = dates[start[0]], dates[end[0]]
            ratio = (last - end[1]) / span if span else None
        vol = _volume_state(
            close,
            volume,
            high,
            low,
            impulse_start=start[0],
            impulse_end=end[0],
            bull=bull_bias,
        )
        levels = _ladder(start[1], end[1], bull=bull_bias, volume_state=vol["volume_state"], close=last)

    decision = decide_signal(
        close=last,
        atr=last_atr,
        ma50=last_ma50,
        ma200=last_ma200,
        ratio=ratio,
        volume_state=str(vol["volume_state"]),
        bars=len(df),
        span=span,
    )
    if last_ma50 is not None and last_ma200 is not None:
        if last_ma50 > last_ma200:
            ma_state = "50-day above 200-day"
        else:
            ma_state = "50-day below 200-day"
    elif last_ma50 is not None:
        ma_state = "50-day only"
    else:
        ma_state = "—"

    payload: dict[str, Any] = {
        "ok": bool(levels),
        "action": decision["action"],
        "rating": decision["rating"],
        "score": decision.get("score", 50),
        "confidence": decision["confidence"],
        "trend": decision["trend"],
        "zone": decision["zone"],
        "why": decision["why"],
        "levels": levels,
        "close": _round(last, 4),
        "sma_50": _round(last_ma50, 4),
        "sma_200": _round(last_ma200, 4),
        "dist_sma_50": _round((last / last_ma50 - 1.0) if last_ma50 else None, 4),
        "dist_sma_200": _round((last / last_ma200 - 1.0) if last_ma200 else None, 4),
        "ma_state": ma_state,
        "volume_state": vol["volume_state"],
        "volume_ratio": vol["volume_ratio"],
        "up_down_volume": vol.get("up_down_volume"),
        "pullback_volume": vol.get("pullback_volume"),
        "fib_ratio": _round(ratio, 4),
        "swing_low": _round(swing_low, 4),
        "swing_high": _round(swing_high, 4),
        "swing_low_on": swing_low_on,
        "swing_high_on": swing_high_on,
        "direction": "up" if bull_bias else "down",
        "atr": _round(last_atr, 4),
        "bars": int(len(df)),
        "as_of": dates[-1] if dates else None,
        "note": NOTE,
    }
    if with_candles:
        tail = slice(-260, None)
        payload["candles"] = [
            {
                "date": dates[i],
                "close": _round(close[i], 4),
                "sma50": _round(ma50[i], 4),
                "sma200": _round(ma200[i], 4),
            }
            for i in range(len(close))[tail]
        ]
    return payload


def _error_row(ticker: str, message: str) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "ok": False,
        "action": "HOLD",
        "rating": "HOLD",
        "score": 0,
        "confidence": 0,
        "trend": "UNKNOWN",
        "zone": "—",
        "why": message,
        "levels": [],
        "close": None,
        "sma_50": None,
        "sma_200": None,
        "volume_state": "Neutral",
        "volume_ratio": None,
        "fib_ratio": None,
    }


def analyze_symbol(ticker: str, *, period: str = "5y", with_candles: bool = True) -> dict[str, Any]:
    from aieq.symbols import resolve_symbol

    queried = ticker.strip()
    resolved, tried = resolve_symbol(queried)
    if not resolved:
        raise ValueError(f"No listing for {queried!r}. Tried: {', '.join(tried) or 'nothing'}.")
    from aieq.data import fetch_ohlcv
    from aieq.memcache import get_mem, put_mem

    period = period if period in PERIODS else "5y"
    key = f"ta_v3_{resolved}_{period}_{int(with_candles)}"
    hit = get_mem(key, 20 * 60)
    if isinstance(hit, dict):
        out = dict(hit)
        out["queried"] = queried
        return out
    frame = fetch_ohlcv(resolved, Settings(period=period))
    if frame is None or frame.empty:
        raise ValueError(f"No price history for {resolved}.")
    payload = analyze_frame(frame, with_candles=with_candles)
    payload["ticker"] = resolved
    payload["queried"] = queried
    put_mem(key, payload)
    return payload


def _analyze_known(ticker: str, period: str) -> dict[str, Any]:
    from aieq.data import fetch_ohlcv
    from aieq.memcache import get_mem, put_mem

    key = f"ta_v3_{ticker}_{period}_0"
    hit = get_mem(key, 20 * 60)
    if isinstance(hit, dict):
        return hit
    try:
        frame = fetch_ohlcv(ticker, Settings(period=period))
    except Exception as exc:
        return _error_row(ticker, str(exc))
    if frame is None or frame.empty:
        return _error_row(ticker, "No price history.")
    try:
        payload = analyze_frame(frame, with_candles=False)
    except Exception as exc:
        return _error_row(ticker, str(exc))
    payload["ticker"] = ticker
    put_mem(key, payload)
    return payload


def scan_universe(name: str, *, period: str = "5y", cursor: int = 0, limit: int = 8) -> dict[str, Any]:
    from aieq.universe import universe_tickers

    period = period if period in PERIODS else "5y"
    tickers = universe_tickers(name or "mega")
    total = len(tickers)
    cursor = max(0, int(cursor))
    limit = min(12, max(1, int(limit)))
    batch = tickers[cursor : cursor + limit]
    rows: list[dict[str, Any]] = []
    if batch:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_analyze_known, ticker, period): ticker for ticker in batch}
            by_ticker: dict[str, dict[str, Any]] = {}
            for future in as_completed(futures):
                ticker = futures[future]
                try:
                    by_ticker[ticker] = future.result()
                except Exception as exc:
                    by_ticker[ticker] = _error_row(ticker, str(exc))
        rows = [by_ticker[ticker] for ticker in batch]
    next_cursor = cursor + len(batch)
    return {
        "universe": name or "mega",
        "period": period,
        "cursor": cursor,
        "next_cursor": next_cursor,
        "total": total,
        "done": next_cursor >= total,
        "n": len(rows),
        "rows": rows,
        "note": NOTE,
    }
