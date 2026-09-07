from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd


BULLISH_PHRASES = (
    "pt raised", "price target raised", "all-time high", "guidance raise",
    "better-than-expected", "top-line beat", "contract win", "beats estimates",
    "tops estimates", "raises guidance", "raised guidance",
)
BEARISH_PHRASES = (
    "guidance cut", "pt cut", "price target cut", "sell rating", "cause of death",
    "passed away", "loses life", "lost his life", "lost her life", "dies at",
    "dead at", "killed in", "shot dead",
)
BULLISH_WORDS = {
    "beat", "beats", "surge", "surges", "rally", "rallies", "upgrade", "upgraded",
    "record", "bullish", "growth", "profit", "profits", "outperform", "buyback",
    "raise", "raised", "raises", "strong", "soar", "soars", "breakout", "upside",
    "expansion", "accelerate", "accelerates", "optimism", "overweight", "initiate",
    "initiated",
}
BEARISH_WORDS = {
    "miss", "misses", "plunge", "plunges", "downgrade", "downgraded", "lawsuit",
    "bearish", "layoff", "layoffs", "cut", "cuts", "warning", "weak", "fraud",
    "probe", "investigation", "crash", "selloff", "underperform", "delay",
    "delayed", "recall", "bankruptcy", "default", "missed", "short", "overvalued",
    "slowdown", "contraction", "disappoint", "fear", "underweight", "reduce",
    "death", "died", "dies", "dying", "killed", "killing", "murder", "murdered",
    "slain", "obituary", "casualty", "casualties", "funeral", "assassination",
    "massacre", "war", "wars", "invasion", "airstrike", "sanctions", "tariff",
    "tariffs", "hostage", "hostages", "ceasefire", "conflict", "missile",
}
_HARD_NEGATIVE = re.compile(
    r"\b(death|died|dies|dying|killed|killings|killing|murder|murdered|slain|"
    r"obituary|casualt(?:y|ies)|funeral|assassination|massacre)\b",
    re.I,
)
_WORD_BOUNDARY = {w: re.compile(rf"\b{re.escape(w)}\b", re.I) for w in BULLISH_WORDS | BEARISH_WORDS}


def _headline_lexicon_hits(title: str) -> tuple[int, int]:
    """Count bullish vs bearish lexicon hits with word boundaries (avoids de-ath, exe-cut)."""
    t = str(title or "").lower().strip()
    if not t:
        return 0, 0
    if _HARD_NEGATIVE.search(t) or any(p in t for p in BEARISH_PHRASES):
        return 0, 3
    bull = sum(1 for p in BULLISH_PHRASES if p in t)
    bear = sum(1 for p in BEARISH_PHRASES if p in t)
    for w, rx in _WORD_BOUNDARY.items():
        if not rx.search(t):
            continue
        if w in BULLISH_WORDS:
            bull += 1
        else:
            bear += 1
    return bull, bear


def headline_sentiment(title: str) -> str:
    """Classify a single headline as positive, negative, or neutral."""
    bull, bear = _headline_lexicon_hits(title)
    if bull > bear:
        return "positive"
    if bear > bull:
        return "negative"
    return "neutral"


@dataclass
class AgentVote:
    name: str
    score: float  # -1 bearish ... +1 bullish
    confidence: float  # 0..1
    reason: str


RATINGS = ("HIGH BUY", "BUY", "HOLD", "SELL")


@dataclass
class Consensus:
    rating: str  # HIGH BUY | BUY | HOLD | SELL
    score: float
    confidence: float
    agreement: float
    thesis: str
    votes: list[AgentVote] = field(default_factory=list)
    ml_weight_used: float = 0.28

    @property
    def side(self) -> str:
        return self.rating


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return float(np.clip(x, lo, hi))


def _last(feat: pd.DataFrame, col: str, default: float = 0.0) -> float:
    if col not in feat.columns or feat.empty:
        return default
    val = feat[col].iloc[-1]
    if pd.isna(val):
        return default
    return float(val)


def score_news(news: list[dict[str, Any]]) -> tuple[float, str, dict[str, Any] | None]:
    finnhub = None
    titles = []
    for item in news:
        if "_finnhub_sentiment" in item:
            finnhub = item["_finnhub_sentiment"]
            continue
        titles.append(str(item.get("title") or "").lower())
    if not titles and not finnhub:
        return 0.0, "No headlines available.", finnhub
    bull = bear = 0
    for t in titles:
        b, r = _headline_lexicon_hits(t)
        bull += b
        bear += r
    raw = 0.0
    if bull + bear > 0:
        raw = (bull - bear) / max(bull + bear, 1)
    if finnhub and isinstance(finnhub, dict):
        s = finnhub.get("sentiment") or {}
        bullish = float(s.get("bullishPercent") or 0)
        bearish = float(s.get("bearishPercent") or 0)
        if bullish or bearish:
            raw = 0.5 * raw + 0.5 * _clip((bullish - bearish) / 100.0)
    reason = f"Headline lexicon {bull} bull / {bear} bear across {len(titles)} stories."
    return _clip(raw), reason, finnhub


def trend_agent(feat: pd.DataFrame) -> AgentVote:
    stack = _last(feat, "sma_stack")
    adx = _last(feat, "adx")
    dist50 = _last(feat, "dist_sma_50")
    dist200 = _last(feat, "dist_sma_200")
    plus_di = _last(feat, "plus_di")
    minus_di = _last(feat, "minus_di")
    di = _clip((plus_di - minus_di) / 40.0)
    score = _clip(0.45 * stack + 0.30 * np.tanh(dist50 * 8) + 0.15 * np.tanh(dist200 * 6) + 0.10 * di)
    conf = float(np.clip(adx / 40.0, 0.25, 0.95))
    regime = "strong trend" if adx >= 25 else "weak / range"
    return AgentVote("Trend", score, conf, f"{regime}, ADX {adx:.1f}, vs 50DMA {dist50:+.1%}, vs 200DMA {dist200:+.1%}.")


def momentum_agent(feat: pd.DataFrame) -> AgentVote:
    rsi14 = _last(feat, "rsi_14", 50)
    macd_h = _last(feat, "macd_hist")
    ret10 = _last(feat, "ret_10")
    ret21 = _last(feat, "ret_21")
    stoch = _last(feat, "stoch_k", 50)
    rsi_s = _clip((rsi14 - 50) / 25.0)
    mom = _clip(0.35 * rsi_s + 0.25 * np.tanh(macd_h * 40) + 0.25 * np.tanh(ret21 * 8) + 0.15 * np.tanh(ret10 * 12))
    # Extreme RSI fades slightly into mean-reversion warning, not a flip.
    if rsi14 > 78 or rsi14 < 22:
        mom *= 0.7
    conf = 0.55 + 0.3 * abs(mom)
    return AgentVote("Momentum", mom, float(np.clip(conf, 0.3, 0.9)), f"RSI {rsi14:.1f}, MACD hist {macd_h:.4f}, 21d {ret21:+.1%}, stoch {stoch:.0f}.")


def mean_reversion_agent(feat: pd.DataFrame) -> AgentVote:
    pct_b = _last(feat, "bb_pct_b", 0.5)
    rsi14 = _last(feat, "rsi_14", 50)
    z_proxy = _clip((0.5 - pct_b) * 2.2)  # below lower band -> buy
    rsi_rev = 0.0
    if rsi14 < 30:
        rsi_rev = (30 - rsi14) / 30
    elif rsi14 > 70:
        rsi_rev = -(rsi14 - 70) / 30
    score = _clip(0.65 * z_proxy + 0.35 * rsi_rev)
    adx = _last(feat, "adx")
    conf = float(np.clip(0.65 - adx / 80.0, 0.15, 0.7))  # less trusted in trends
    return AgentVote("MeanReversion", score, conf, f"Bollinger %B {pct_b:.2f}, RSI {rsi14:.1f} (faded when ADX high).")


def ml_agent(p_up: float, exp_ret: float, oos_sharpe: float, backend: str) -> AgentVote:
    score = _clip((p_up - 0.5) * 2.4 + np.tanh(exp_ret * 12) * 0.25)
    # Confidence tracks both probability extremity and OOS edge.
    edge = float(np.clip((oos_sharpe + 0.3) / 1.8, 0.15, 1.0))
    conf = float(np.clip(0.35 + abs(p_up - 0.5) * 1.4, 0.2, 0.95) * edge)
    return AgentVote(
        "XGBoost",
        score,
        conf,
        f"{backend} P(up 5d)={p_up:.1%}, E[ret]={exp_ret:+.2%}, long-only OOS Sharpe {oos_sharpe:.2f}.",
    )


def options_agent(chain: dict[str, Any]) -> AgentVote:
    calls = chain.get("calls")
    puts = chain.get("puts")
    spot = chain.get("spot")
    if not isinstance(calls, pd.DataFrame) or not isinstance(puts, pd.DataFrame) or calls.empty or puts.empty:
        return AgentVote("OptionsFlow", 0.0, 0.2, "No options chain (illiquid or unavailable).")

    def _col(df: pd.DataFrame, *names: str, default: float = 0.0) -> pd.Series:
        for n in names:
            if n in df.columns:
                return pd.to_numeric(df[n], errors="coerce").fillna(0.0)
        return pd.Series(default, index=df.index)

    call_vol = float(_col(calls, "volume").sum())
    put_vol = float(_col(puts, "volume").sum())
    call_oi = float(_col(calls, "openInterest", "openInterest").sum())
    put_oi = float(_col(puts, "openInterest").sum())
    pcr_vol = put_vol / call_vol if call_vol > 0 else 1.0
    pcr_oi = put_oi / call_oi if call_oi > 0 else 1.0

    # Single-name: follow flow. Heavy call volume = bullish.
    flow = _clip((1.0 - pcr_vol) / 0.8)
    oi_s = _clip((1.0 - pcr_oi) / 1.0)

    call_iv = _col(calls, "impliedVolatility")
    put_iv = _col(puts, "impliedVolatility")
    skew = 0.0
    if spot and len(call_iv) and len(put_iv) and "strike" in calls.columns and "strike" in puts.columns:
        cs = (pd.to_numeric(calls["strike"], errors="coerce") - float(spot)).abs()
        ps = (pd.to_numeric(puts["strike"], errors="coerce") - float(spot)).abs()
        atm_c = calls.loc[cs.nsmallest(min(3, len(cs))).index]
        atm_p = puts.loc[ps.nsmallest(min(3, len(ps))).index]
        civ = pd.to_numeric(atm_c.get("impliedVolatility"), errors="coerce").mean()
        piv = pd.to_numeric(atm_p.get("impliedVolatility"), errors="coerce").mean()
        if pd.notna(civ) and pd.notna(piv) and civ > 0:
            skew = _clip((piv - civ) / max(civ, 0.05))  # put premium = fear
    score = _clip(0.55 * flow + 0.25 * oi_s - 0.20 * skew)
    conf = 0.45 if (call_vol + put_vol) > 200 else 0.3
    return AgentVote(
        "OptionsFlow",
        score,
        conf,
        f"Options used as a buy/sell factor: PCR vol {pcr_vol:.2f}, PCR OI {pcr_oi:.2f}, "
        f"put-call IV skew {skew:+.2f}, call vol {call_vol:.0f} vs put {put_vol:.0f}.",
    )


def sentiment_agent(news: list[dict[str, Any]], fundamentals: dict[str, Any]) -> AgentVote:
    news_score, news_reason, _ = score_news(news)
    rec = fundamentals.get("recommendation_mean")
    rec_s = 0.0
    if rec is not None:
        try:
            rec_s = _clip((3.0 - float(rec)) / 1.5)  # 1 strong buy, 5 sell
        except (TypeError, ValueError):
            rec_s = 0.0
    target = fundamentals.get("target_mean")
    px = fundamentals.get("current_price")
    tgt_s = 0.0
    if target and px:
        try:
            tgt_s = _clip((float(target) / float(px) - 1.0) / 0.25)
        except (TypeError, ValueError, ZeroDivisionError):
            tgt_s = 0.0
    score = _clip(0.70 * news_score + 0.18 * rec_s + 0.12 * tgt_s)
    conf = 0.35 + 0.25 * (1.0 if news else 0.0) + 0.2 * (1.0 if rec is not None else 0.0)
    bits = [news_reason]
    if rec is not None:
        bits.append(f"Street mean rating {rec:.2f} (1=buy).")
    if target and px:
        bits.append(f"Mean target {float(target):.2f} vs {float(px):.2f} ({float(target)/float(px)-1:+.1%}).")
    return AgentVote("Sentiment", score, float(np.clip(conf, 0.2, 0.85)), " ".join(bits))


def macro_agent(feat: pd.DataFrame) -> AgentVote:
    vix = _last(feat, "vix", 18)
    vix_z = _last(feat, "vix_z")
    spy50 = _last(feat, "spy_sma_50")
    spy200 = _last(feat, "spy_sma_200")
    rs = _last(feat, "rs_spy_21")
    vix_s = _clip(-(vix - 18) / 12.0 - 0.3 * vix_z)
    mkt = _clip(np.tanh(spy50 * 8) * 0.6 + np.tanh(spy200 * 6) * 0.4)
    score = _clip(0.40 * vix_s + 0.35 * mkt + 0.25 * np.tanh(rs * 10))
    conf = 0.5 if vix else 0.3
    return AgentVote("Macro", score, conf, f"VIX {vix:.1f} (z={vix_z:+.2f}), SPY vs 50/200 {spy50:+.1%}/{spy200:+.1%}, 21d RS {rs:+.1%}.")


def fundamental_agent(fundamentals: dict[str, Any], feat: pd.DataFrame) -> AgentVote:
    pe = fundamentals.get("forward_pe") or fundamentals.get("trailing_pe")
    growth = fundamentals.get("earnings_growth") or fundamentals.get("revenue_growth")
    pm = fundamentals.get("profit_margin")
    short = fundamentals.get("short_percent")
    dist_high = _last(feat, "dist_52w_high")
    parts = []
    score = 0.0
    n = 0
    if pe:
        try:
            pe_s = _clip((22 - float(pe)) / 18.0)
            score += pe_s
            n += 1
            parts.append(f"PE {float(pe):.1f}")
        except (TypeError, ValueError):
            pass
    if growth is not None:
        try:
            g_s = _clip(float(growth) / 0.25)
            score += g_s
            n += 1
            parts.append(f"growth {float(growth):+.1%}")
        except (TypeError, ValueError):
            pass
    if pm is not None:
        try:
            score += _clip((float(pm) - 0.12) / 0.2)
            n += 1
            parts.append(f"margin {float(pm):.1%}")
        except (TypeError, ValueError):
            pass
    if short is not None:
        try:
            # Crowded short can be fuel; very high short is a risk either way. Mildly bullish squeeze bias.
            s = float(short)
            score += _clip((s - 0.03) / 0.12) * 0.35
            parts.append(f"short {s:.1%}")
        except (TypeError, ValueError):
            pass
    score = _clip(score / max(n, 1) + 0.15 * np.tanh(dist_high * 4))
    conf = 0.25 + 0.12 * n
    return AgentVote("Fundamental", score, float(np.clip(conf, 0.2, 0.7)), ", ".join(parts) or "Sparse fundamentals.")


GEO_RISK_OFF = {
    "war", "invasion", "missile", "airstrike", "bombing", "escalation",
    "sanctions", "tariff", "tariffs", "embargo", "blockade", "nuclear",
    "coup", "hostage", "iran", "hormuz", "red sea", "gaza", "ukraine",
    "taiwan strait", "export control", "chip ban", "drone strike",
    "ceasefire collapse", "strike on iran", "oil shock",
}
GEO_RISK_ON = {
    "ceasefire", "truce", "peace deal", "de-escalat", "diplomatic",
    "sanctions relief", "tariff pause", "trade deal", "talks resume",
    "hostage release", "withdrawal", "armistice",
}
GEO_ENERGY = {"oil", "opec", "brent", "crude", "hormuz", "pipeline", "refinery", "lng"}
GEO_DEFENSE = {"pentagon", "nato", "missile", "defense contractor", "fighter jet", "warship"}
GEO_CHINA_TECH = {"taiwan", "export control", "chip ban", "huawei", "semiconductor tariff"}


def _count_terms(text: str, terms: set[str]) -> int:
    return sum(1 for w in terms if w in text)


def geopolitics_agent(
    geo_news: list[dict[str, Any]],
    company_news: list[dict[str, Any]],
    fundamentals: dict[str, Any],
) -> AgentVote:
    titles = []
    for item in list(geo_news or []) + list(company_news or []):
        if item.get("_finnhub_sentiment"):
            continue
        t = str(item.get("title") or "").strip()
        if t:
            titles.append(t.lower())
    if not titles:
        return AgentVote("Geopolitics", 0.0, 0.2, "No geopolitical headlines available.")

    off = on = energy = defense = china = 0
    hits: list[str] = []
    for t in titles:
        c_off = _count_terms(t, GEO_RISK_OFF)
        c_on = _count_terms(t, GEO_RISK_ON)
        off += c_off
        on += c_on
        energy += _count_terms(t, GEO_ENERGY)
        defense += _count_terms(t, GEO_DEFENSE)
        china += _count_terms(t, GEO_CHINA_TECH)
        if c_off or c_on:
            hits.append(t[:110])

    raw = (on - off) / max(on + off, 1)
    score = _clip(np.tanh(raw) * 0.65)

    sector = str(fundamentals.get("sector") or "").lower()
    industry = str(fundamentals.get("industry") or "").lower()
    blob = f"{sector} {industry}"
    overlay = []
    if any(k in blob for k in ("energy", "oil", "gas")) and energy:
        score = _clip(score + 0.22)
        overlay.append("energy name: supply-risk headlines are a tailwind")
    if any(k in blob for k in ("aerospace", "defense")) and (defense or off):
        score = _clip(score + 0.25)
        overlay.append("defense name: conflict headlines are a tailwind")
    if any(k in blob for k in ("technology", "communication", "semiconductor")) and (china or off):
        score = _clip(score - 0.18)
        overlay.append("tech name: China/Taiwan/tariff risk is a headwind")
    if "financial" in blob and off:
        score = _clip(score - 0.08)
        overlay.append("financials: escalation is a mild headwind")

    conf = float(np.clip(0.25 + 0.04 * min(on + off, 12), 0.2, 0.8))
    tone = "risk-off" if score < -0.08 else "risk-on" if score > 0.08 else "mixed"
    sample = hits[0] if hits else titles[0]
    reason = (
        f"{tone} tape across {len(titles)} world/company headlines "
        f"({on} de-escalation / {off} stress hits). Sample: {sample}."
    )
    if overlay:
        reason += " " + "; ".join(overlay) + "."
    return AgentVote("Geopolitics", score, conf, reason)


def write_why(
    ticker: str,
    name: str,
    rating: str,
    score: float,
    confidence: float,
    agreement: float,
    votes: list[AgentVote],
    price: float | None = None,
) -> str:
    """Plain-language explanation of why this name is High Buy / Buy / Hold / Sell."""
    px = f" at {price:.2f}" if price else ""
    openings = {
        "HIGH BUY": (
            f"{ticker} ({name}) is a HIGH BUY{px}. The desk would accumulate here — "
            "the agents that matter are aligned to the upside, not just one loud bullish input."
        ),
        "BUY": (
            f"{ticker} ({name}) is a BUY{px}. The lean is higher, but conviction is not high enough "
            "to call it a pile-in. Add on weakness rather than chase."
        ),
        "HOLD": (
            f"{ticker} ({name}) is a HOLD{px}. Agents disagree or the edge is too thin to force a side. "
            "Sitting in cash or the existing position is the trade."
        ),
        "SELL": (
            f"{ticker} ({name}) is a SELL{px}. The desk would reduce or exit. Waiting for a bounce "
            "is hoping, not a setup."
        ),
    }
    parts = [openings.get(rating, f"{ticker} is {rating}.")]

    ranked = sorted(votes, key=lambda v: abs(v.score) * v.confidence, reverse=True)
    bullets: list[str] = []
    for v in ranked:
        if abs(v.score) < 0.06:
            continue
        tone = "bullish" if v.score > 0 else "bearish"
        bullets.append(f"{v.name} is {tone} ({v.score:+.2f}). {v.reason}")
        if len(bullets) >= 5:
            break
    if bullets:
        parts.append("What is driving it: " + " ".join(bullets))

    flow = next((v for v in votes if v.name == "OptionsFlow"), None)
    geo = next((v for v in votes if v.name == "Geopolitics"), None)
    extras = []
    if flow:
        extras.append(
            "Options flow is a buy/sell factor, not the verdict: "
            + ("call demand supports buying." if flow.score > 0.08 else "put demand / hedging pressure leans sell." if flow.score < -0.08 else "the chain is mixed, so it does not flip the rating.")
        )
    if geo:
        extras.append(
            "Geopolitics: "
            + ("risk-off tape is a headwind for this name." if geo.score < -0.08 else "the world tape is not fighting the long." if geo.score > 0.08 else "the world tape is noise, not the driver.")
        )
    if extras:
        parts.append(" ".join(extras))

    parts.append(
        f"Net score {score:+.2f} (above ~+0.28 with agreement is High Buy; above +0.10 is Buy; "
        f"below −0.12 is Sell; otherwise Hold). Confidence {confidence:.0%}, agent agreement {agreement:.0%}."
    )
    return " ".join(parts)


def to_rating(score: float, confidence: float, agreement: float) -> tuple[str, str]:
    if score >= 0.28 and confidence >= 0.32 and agreement >= 0.30:
        return "HIGH BUY", "strong accumulate"
    if score >= 0.10:
        return "BUY", "accumulate"
    if score <= -0.12:
        return "SELL", "reduce / exit"
    return "HOLD", "sit tight"


def aggregate(
    votes: list[AgentVote],
    oos_sharpe: float,
    adx: float,
) -> Consensus:
    weights = {
        "XGBoost": 0.26,
        "Trend": 0.14,
        "Momentum": 0.12,
        "OptionsFlow": 0.16,
        "Geopolitics": 0.12,
        "Macro": 0.08,
        "Sentiment": 0.06,
        "Fundamental": 0.03,
        "MeanReversion": 0.03,
    }
    # If the model has no OOS edge, shrink it hard.
    ml_w = weights["XGBoost"]
    if oos_sharpe < 0:
        ml_w *= 0.35
    elif oos_sharpe < 0.4:
        ml_w *= 0.7
    elif oos_sharpe > 1.0:
        ml_w *= 1.15
    weights["XGBoost"] = ml_w
    if adx >= 25:
        weights["Trend"] *= 1.25
        weights["MeanReversion"] *= 0.4
        weights["Momentum"] *= 1.1
    else:
        weights["MeanReversion"] *= 1.6
        weights["Trend"] *= 0.8

    wsum = sum(weights.get(v.name, 0.05) * max(v.confidence, 0.15) for v in votes)
    score = 0.0
    for v in votes:
        w = weights.get(v.name, 0.05) * max(v.confidence, 0.15)
        score += w * v.score
    score = _clip(score / wsum if wsum else 0.0)

    signs = [1 if v.score > 0.05 else -1 if v.score < -0.05 else 0 for v in votes]
    nonzero = [s for s in signs if s != 0]
    if nonzero:
        agreement = abs(sum(nonzero)) / len(nonzero)
    else:
        agreement = 0.0
    conf = float(np.clip(0.35 * abs(score) + 0.45 * agreement + 0.2 * np.mean([v.confidence for v in votes]), 0, 1))

    rating, lean = to_rating(score, conf, agreement)

    ranked = sorted(votes, key=lambda v: abs(v.score) * v.confidence, reverse=True)
    top = ", ".join(f"{v.name} {v.score:+.2f}" for v in ranked[:3])
    flow = next((v for v in votes if v.name == "OptionsFlow"), None)
    geo = next((v for v in votes if v.name == "Geopolitics"), None)
    thesis = (
        f"Desk rating is {rating} ({lean}) at {score:+.2f} with {conf:.0%} confidence "
        f"and {agreement:.0%} agent agreement. Drivers: {top}."
    )
    if flow:
        thesis += f" Options flow factor {flow.score:+.2f}."
    if geo:
        thesis += f" Geopolitics factor {geo.score:+.2f}."
    ml = next((v for v in votes if v.name == "XGBoost"), None)
    if ml and ((ml.score > 0.08 and score < 0) or (ml.score < -0.08 and score > 0)):
        thesis += (
            f" Conflict: XGBoost leans the other way (score {ml.score:+.2f}); "
            f"OOS Sharpe {oos_sharpe:.2f} so its vote weight is {weights['XGBoost']:.2f}."
        )
    return Consensus(
        rating=rating,
        score=score,
        confidence=conf,
        agreement=agreement,
        thesis=thesis,
        votes=votes,
        ml_weight_used=weights["XGBoost"],
    )
