from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass
class OptionIdea:
    side: str
    expiry: str
    dte: int
    strike: float
    last: float
    bid: float
    ask: float
    mid: float
    volume: int
    open_interest: int
    iv: float
    moneyness: float
    spread_pct: float
    score: float
    thesis: str


def _num(df: pd.DataFrame, name: str, default: float = 0.0) -> pd.Series:
    if name not in df.columns:
        return pd.Series(default, index=df.index, dtype=float)
    return pd.to_numeric(df[name], errors="coerce").fillna(default)


def suggest_options(
    chain: dict[str, Any],
    side: str,
    expected_ret: float,
    atr_pct: float,
    confidence: float,
) -> list[OptionIdea]:
    calls = chain.get("calls")
    puts = chain.get("puts")
    spot = chain.get("spot")
    expiry = chain.get("expiry") or ""
    dte = int(chain.get("dte") or 0)
    if spot is None or not isinstance(calls, pd.DataFrame) or not isinstance(puts, pd.DataFrame):
        return []
    if calls.empty and puts.empty:
        return []

    use = calls if side == "CALL" else puts
    if use is None or use.empty:
        use = calls if not calls.empty else puts
        side = "CALL" if use is calls else "PUT"
    df = use.copy()
    df["strike"] = _num(df, "strike")
    df["last"] = _num(df, "lastPrice")
    df["bid"] = _num(df, "bid")
    df["ask"] = _num(df, "ask")
    df["volume"] = _num(df, "volume").astype(int)
    df["oi"] = _num(df, "openInterest").astype(int)
    df["iv"] = _num(df, "impliedVolatility")
    df["mid"] = np.where((df["bid"] > 0) & (df["ask"] > 0), (df["bid"] + df["ask"]) / 2.0, df["last"])
    df = df[df["mid"] > 0]
    if df.empty:
        return []

    expected_move = max(abs(expected_ret) * (dte / 5.0) ** 0.5, atr_pct * np.sqrt(max(dte, 1) / 21.0), 0.02)
    if side == "CALL":
        target = float(spot) * (1.0 + expected_move * (0.6 + 0.5 * confidence))
        band_lo, band_hi = float(spot) * 0.98, float(spot) * 1.12
    else:
        target = float(spot) * (1.0 - expected_move * (0.6 + 0.5 * confidence))
        band_lo, band_hi = float(spot) * 0.88, float(spot) * 1.02

    df = df[(df["strike"] >= band_lo) & (df["strike"] <= band_hi)]
    if df.empty:
        df = use.copy()
        df["strike"] = _num(df, "strike")
        df["last"] = _num(df, "lastPrice")
        df["bid"] = _num(df, "bid")
        df["ask"] = _num(df, "ask")
        df["volume"] = _num(df, "volume").astype(int)
        df["oi"] = _num(df, "openInterest").astype(int)
        df["iv"] = _num(df, "impliedVolatility")
        df["mid"] = np.where((df["bid"] > 0) & (df["ask"] > 0), (df["bid"] + df["ask"]) / 2.0, df["last"])
        df = df[df["mid"] > 0]

    df["moneyness"] = df["strike"] / float(spot) - 1.0
    df["spread_pct"] = np.where(df["mid"] > 0, (df["ask"] - df["bid"]).clip(lower=0) / df["mid"], 1.0)
    df["dist_target"] = (df["strike"] - target).abs() / float(spot)

    vol_score = np.log1p(df["volume"]) / max(np.log1p(df["volume"].max()), 1e-6)
    oi_score = np.log1p(df["oi"]) / max(np.log1p(df["oi"].max()), 1e-6)
    tight = 1.0 - np.clip(df["spread_pct"] / 0.25, 0, 1)
    df["score"] = 0.35 * (1.0 - np.clip(df["dist_target"] / 0.08, 0, 1)) + 0.25 * vol_score + 0.20 * oi_score + 0.20 * tight

    ideas: list[OptionIdea] = []
    ranked = df.sort_values("score", ascending=False).head(5)
    for i, (_, row) in enumerate(ranked.iterrows()):
        mny = float(row["moneyness"])
        loc = "ITM" if ((side == "CALL" and mny < -0.005) or (side == "PUT" and mny > 0.005)) else "OTM" if abs(mny) > 0.008 else "ATM"
        if i == 0:
            tag = "Primary — best liquidity vs expected move"
        elif loc == "ATM" or abs(mny) < 0.03:
            tag = "Alternate — closer to spot (higher delta)"
        else:
            tag = "Alternate — further OTM (cheaper, needs a real move)"
        iv = float(row["iv"])
        iv_txt = f"{iv:.0%}" if 0.05 <= iv <= 3.0 else "n/a"
        thesis = (
            f"{tag}. {loc} {side} {row['strike']:.2f} exp {expiry} ({dte} DTE). "
            f"Spot {spot:.2f}, expected move ~{expected_move:.1%}. Mid {row['mid']:.2f}, "
            f"IV {iv_txt}, vol {int(row['volume'])}, OI {int(row['oi'])}, spread {row['spread_pct']:.0%}."
        )
        ideas.append(
            OptionIdea(
                side=side,
                expiry=str(expiry),
                dte=dte,
                strike=float(row["strike"]),
                last=float(row["last"]),
                bid=float(row["bid"]),
                ask=float(row["ask"]),
                mid=float(row["mid"]),
                volume=int(row["volume"]),
                open_interest=int(row["oi"]),
                iv=float(row["iv"]),
                moneyness=mny,
                spread_pct=float(row["spread_pct"]),
                score=float(row["score"]),
                thesis=thesis,
            )
        )
    return ideas
