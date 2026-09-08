from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json
import threading

import numpy as np
import pandas as pd

from aieq.backtest import BacktestReport, run_backtest
from aieq.config import CACHE_DIR, DEFAULT, Settings, ensure_cache_dir
from aieq.consensus import (
    AgentVote,
    Consensus,
    aggregate,
    fundamental_agent,
    geopolitics_agent,
    headline_sentiment,
    macro_agent,
    mean_reversion_agent,
    ml_agent,
    momentum_agent,
    options_agent,
    sentiment_agent,
    trend_agent,
    write_why,
)
from aieq.data import fetch_benchmarks, fetch_fundamentals, fetch_geo_news, fetch_news, fetch_ohlcv, fetch_options_chain, format_company_blurb
from aieq.features import build_features, latest_feature_row, make_xy
from aieq.models import walk_forward
from aieq.options import OptionIdea, suggest_options
from aieq.symbols import ALIASES, resolve_symbol


@dataclass
class AnalysisResult:
    ticker: str
    queried: str
    name: str
    as_of: str
    price: float
    consensus: Consensus
    backtest: BacktestReport
    options: list[OptionIdea]
    news: list[dict[str, Any]]
    geo_news: list[dict[str, Any]]
    fundamentals: dict[str, Any]
    feature_importance: dict[str, float]
    model_metrics: dict[str, float]
    backend: str
    atr_pct: float
    ohlcv: pd.DataFrame = field(repr=False, default_factory=pd.DataFrame)
    features: pd.DataFrame = field(repr=False, default_factory=pd.DataFrame)

    def to_summary(self) -> dict[str, Any]:
        c = self.consensus
        flow = next((v for v in c.votes if v.name == "OptionsFlow"), None)
        geo = next((v for v in c.votes if v.name == "Geopolitics"), None)
        why = write_why(
            self.ticker,
            self.name,
            c.rating,
            c.score,
            c.confidence,
            c.agreement,
            c.votes,
            price=self.price,
        )
        return {
            "ticker": self.ticker,
            "queried": self.queried,
            "name": self.name,
            "sector": self.fundamentals.get("sector") or "—",
            "price": self.price,
            "rating": c.rating,
            "score": round(c.score, 4),
            "confidence": round(c.confidence, 4),
            "agreement": round(c.agreement, 4),
            "p_up": self.model_metrics.get("live_p_up"),
            "exp_ret": self.model_metrics.get("live_expected_ret"),
            "oos_sharpe": self.backtest.metrics.get("sharpe"),
            "oos_cagr": self.backtest.metrics.get("cagr"),
            "max_dd": self.backtest.metrics.get("max_drawdown"),
            "options_factor": None if flow is None else round(flow.score, 3),
            "geo_factor": None if geo is None else round(geo.score, 3),
            "why": why,
            "thesis": c.thesis,
            "as_of": self.as_of,
            "drivers": [
                {"agent": v.name, "score": round(v.score, 3), "reason": v.reason}
                for v in sorted(c.votes, key=lambda x: abs(x.score), reverse=True)
            ],
            "spark": _spark_points(self.ohlcv, 90),
        }


def _rules_model(feat: pd.DataFrame, backend: str = "rules_only"):
    from aieq.models import WalkForwardResult

    rsi = float(feat["rsi_14"].iloc[-1]) if "rsi_14" in feat.columns and pd.notna(feat["rsi_14"].iloc[-1]) else 50.0
    p_up = float(min(max(0.5 + (rsi - 50.0) / 100.0, 0.05), 0.95))
    return WalkForwardResult(
        oos=pd.DataFrame(),
        feature_importance={},
        backend=backend,
        live_p_up=p_up,
        live_expected_ret=float(feat["ret_5"].iloc[-1]) if "ret_5" in feat.columns else 0.0,
        metrics={},
    )


def analyze(
    ticker: str,
    settings: Settings | None = None,
    deep: bool = True,
    board: bool = False,
    skip_resolve: bool = False,
    shared_benches: dict[str, Any] | None = None,
    shared_geo: list[dict[str, Any]] | None = None,
) -> AnalysisResult:
    settings = settings or DEFAULT
    queried = ticker.strip()
    alias = ALIASES.get(queried.upper()) or ALIASES.get(queried.upper().replace(" ", ""))
    if skip_resolve:
        ticker = (alias or queried).upper()
    else:
        resolved, tried = resolve_symbol(queried)
        if not resolved:
            hint = ""
            q = queried.upper().replace(" ", "")
            if q in {"TSMC", "TSM C"} or "TSMC" in queried.upper():
                hint = " Taiwan Semiconductor trades as TSM (NYSE ADR) or 2330.TW (Taiwan)."
            raise ValueError(
                f"Not enough market data for {queried!r}. Tried: {', '.join(tried) or 'nothing'}."
                f"{hint} Use the Yahoo/exchange symbol."
            )
        ticker = resolved
    ohlcv = fetch_ohlcv(ticker, settings)
    if ohlcv.empty or len(ohlcv) < 40:
        raise ValueError(
            f"Not enough price history for {queried} (resolved {ticker}, {0 if ohlcv.empty else len(ohlcv)} bars)."
        )

    benches = shared_benches if shared_benches is not None else fetch_benchmarks(settings)
    feat = build_features(ohlcv, spy=benches.get("SPY"), vix=benches.get("^VIX"), qqq=benches.get("QQQ"))
    X, y_cls, y_reg = make_xy(feat, horizon=settings.horizon)
    live_row = latest_feature_row(feat, feature_cols=list(X.columns))

    # Board refresh: skip XGBoost unless Deep walk-forward is on. Single-name deep-dive still trains.
    if board and not deep:
        wf = _rules_model(feat, backend="board_scan")
    elif len(X) > 180:
        try:
            wf = walk_forward(X, y_cls, y_reg, settings, live_row=live_row, do_oos=deep)
        except Exception:
            wf = _rules_model(feat, backend="rules_fallback")
    else:
        wf = _rules_model(feat)

    bt = BacktestReport(pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=float), pd.DataFrame(), {})
    if wf.oos is not None and not wf.oos.empty:
        bt = run_backtest(
            ohlcv["close"].reindex(wf.oos.index),
            wf.oos["p_up"],
            settings,
            long_only=True,
        )

    oos_sharpe = float(bt.metrics.get("sharpe") or 0.0)
    fundamentals = fetch_fundamentals(ticker, settings)
    if board and not deep:
        news: list[dict[str, Any]] = []
        geo_news = list(shared_geo or [])
        chain: dict[str, Any] = {}
    else:
        news = fetch_news(ticker, settings)
        geo_news = shared_geo if shared_geo is not None else fetch_geo_news(settings)
        chain = fetch_options_chain(ticker, settings)

    votes: list[AgentVote] = [
        trend_agent(feat),
        momentum_agent(feat),
        mean_reversion_agent(feat),
        ml_agent(wf.live_p_up, wf.live_expected_ret, oos_sharpe, wf.backend),
        options_agent(chain),
        sentiment_agent(news, fundamentals),
        geopolitics_agent(geo_news, news, fundamentals),
        macro_agent(feat),
        fundamental_agent(fundamentals, feat),
    ]
    adx = float(feat["adx"].iloc[-1]) if "adx" in feat.columns and pd.notna(feat["adx"].iloc[-1]) else 15.0
    consensus = aggregate(votes, oos_sharpe, adx)
    atr_pct = float(feat["atr_pct"].iloc[-1]) if "atr_pct" in feat.columns and pd.notna(feat["atr_pct"].iloc[-1]) else 0.02
    opt_side = "CALL" if consensus.score >= 0 else "PUT"
    ideas = suggest_options(chain, opt_side, wf.live_expected_ret, atr_pct, consensus.confidence)

    model_metrics = dict(wf.metrics)
    model_metrics["live_p_up"] = wf.live_p_up
    model_metrics["live_expected_ret"] = wf.live_expected_ret

    news_out = []
    for n in [x for x in news if "_finnhub_sentiment" not in x][:12]:
        row = dict(n)
        if row.get("title"):
            row["sentiment"] = headline_sentiment(str(row["title"]))
        news_out.append(row)
    geo_out = []
    for n in [x for x in geo_news if x.get("title")][:12]:
        row = dict(n)
        row["sentiment"] = headline_sentiment(str(row.get("title") or ""))
        geo_out.append(row)
    return AnalysisResult(
        ticker=ticker,
        queried=queried,
        name=str(fundamentals.get("name") or ticker),
        as_of=str(ohlcv.index[-1].date()),
        price=float(ohlcv["close"].iloc[-1]),
        consensus=consensus,
        backtest=bt,
        options=ideas,
        news=news_out,
        geo_news=geo_out,
        fundamentals=fundamentals,
        feature_importance=wf.feature_importance,
        model_metrics=model_metrics,
        backend=wf.backend,
        atr_pct=atr_pct,
        ohlcv=ohlcv,
        features=feat,
    )


def _sorted_board(rows: list[dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    order = {"HIGH BUY": 0, "BUY": 1, "HOLD": 2, "SELL": 3}
    df["ord"] = df["rating"].map(order).fillna(9)
    df = df.sort_values(["ord", "score"], ascending=[True, False]).drop(columns=["ord"])
    return df.reset_index(drop=True)


def scan_universe(
    tickers: list[str] | None = None,
    settings: Settings | None = None,
    max_workers: int = 8,
    deep: bool = False,
    universe: str | None = None,
    on_progress: Any | None = None,
) -> pd.DataFrame:
    settings = settings or DEFAULT
    if tickers is None:
        from aieq.universe import universe_tickers

        tickers = universe_tickers(universe or "global")
    tickers = list(dict.fromkeys(ALIASES.get(str(t).upper(), str(t)).upper() for t in tickers))
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    total = len(tickers)
    done = 0
    lock = threading.Lock()
    uni = universe or "global"
    benches = fetch_benchmarks(settings)
    geo = fetch_geo_news(settings)

    def _flush() -> None:
        try:
            with lock:
                snapshot = list(rows)
            out = _sorted_board(snapshot)
            if not out.empty:
                save_board(out, universe=uni)
        except Exception:
            pass

    def _one(sym: str) -> dict[str, Any] | None:
        try:
            res = analyze(
                sym,
                settings=settings,
                deep=deep,
                board=True,
                skip_resolve=True,
                shared_benches=benches,
                shared_geo=geo,
            )
            return res.to_summary()
        except Exception as exc:
            errors.append(f"{sym}: {exc}")
            return None

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(_one, t): t for t in tickers}
        for fut in as_completed(futs):
            row = fut.result()
            with lock:
                if row:
                    rows.append(row)
                done += 1
                finished = done
            if on_progress:
                try:
                    on_progress(finished, total)
                except Exception:
                    pass
            if finished % 8 == 0:
                _flush()

    out = _sorted_board(rows)
    if not out.empty:
        save_board(out, universe=uni)
    return out


def score_board_batch(
    tickers: list[str],
    universe: str,
    deep: bool = False,
    max_workers: int = 3,
) -> pd.DataFrame:
    """Score a small batch and merge into the saved board for `universe`."""
    settings = DEFAULT
    tickers = list(dict.fromkeys(ALIASES.get(str(t).upper(), str(t)).upper() for t in tickers if t))
    benches = fetch_benchmarks(settings)
    geo = fetch_geo_news(settings)
    fresh: list[dict[str, Any]] = []

    def _one(sym: str) -> dict[str, Any] | None:
        try:
            res = analyze(
                sym,
                settings=settings,
                deep=deep,
                board=True,
                skip_resolve=True,
                shared_benches=benches,
                shared_geo=geo,
            )
            return res.to_summary()
        except Exception:
            return None

    workers = max(1, min(int(max_workers), len(tickers) or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in as_completed([pool.submit(_one, t) for t in tickers]):
            row = fut.result()
            if row:
                fresh.append(row)

    prev, _, _ = load_board(universe)
    by: dict[str, dict[str, Any]] = {}
    if prev is not None and not prev.empty:
        for rec in prev.to_dict(orient="records"):
            key = str(rec.get("ticker") or "").upper()
            if key:
                by[key] = rec
    for rec in fresh:
        key = str(rec.get("ticker") or "").upper()
        if key:
            by[key] = rec
    out = _sorted_board(list(by.values()))
    if not out.empty:
        save_board(out, universe=universe)
    return out


BOARD_PATH = CACHE_DIR / "universe_board.json"
_board_lock = threading.Lock()
_board_mem: dict[str, tuple[pd.DataFrame, str | None, str | None]] = {}


def _spark_points(ohlcv: pd.DataFrame, n: int = 90) -> list[dict[str, Any]]:
    if ohlcv is None or ohlcv.empty or "close" not in ohlcv.columns:
        return []
    tail = ohlcv["close"].dropna().tail(int(n))
    out: list[dict[str, Any]] = []
    for i, v in tail.items():
        out.append(
            {
                "date": str(i.date()) if hasattr(i, "date") else str(i),
                "close": float(v),
            }
        )
    return out


def _board_path(universe: str | None) -> Path:
    if not universe:
        return BOARD_PATH
    safe = "".join(c if c.isalnum() else "_" for c in universe.strip().lower()) or "global"
    return CACHE_DIR / f"universe_board_{safe}.json"


def board_market_session(rows: list[dict[str, Any]]) -> str | None:
    """Latest close date stored on the board (from sparklines)."""
    dates: list[str] = []
    for row in rows:
        spark = row.get("spark")
        if isinstance(spark, str):
            try:
                spark = json.loads(spark)
            except Exception:
                spark = []
        if not spark:
            continue
        d = str((spark[-1] or {}).get("date") or "")[:10]
        if len(d) == 10:
            dates.append(d)
    return max(dates) if dates else None


def _jsonable_row(row: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for k, v in row.items():
        if v is None:
            clean[k] = None
        elif isinstance(v, float) and (pd.isna(v) or v != v):
            clean[k] = None
        elif isinstance(v, (pd.Timestamp, datetime)):
            clean[k] = str(v)
        else:
            try:
                if pd.isna(v):
                    clean[k] = None
                    continue
            except (TypeError, ValueError):
                pass
            clean[k] = v
    return clean


def _board_mem_key(universe: str | None) -> str:
    return "".join(c if c.isalnum() else "_" for c in (universe or "default").strip().lower()) or "default"


def _remember_board(df: pd.DataFrame, as_of: str | None, universe: str | None) -> None:
    if df is None or df.empty:
        return
    key = _board_mem_key(universe)
    snap = (df.copy() if df is not None else pd.DataFrame(), as_of, universe)
    with _board_lock:
        _board_mem[key] = snap
        if universe:
            _board_mem["default"] = snap


def save_board(df: pd.DataFrame, universe: str | None = None) -> None:
    rows = [_jsonable_row(r) for r in df.to_dict(orient="records")]
    payload = {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "session": board_market_session(rows),
        "n": int(len(df)),
        "universe": universe,
        "rows": rows,
    }
    text = json.dumps(payload, default=str)
    from aieq.store import put_doc

    key = _board_mem_key(universe)
    put_doc("board", key, payload)
    put_doc("board", "default", payload)
    _remember_board(df, payload["as_of"], universe)
    try:
        root = ensure_cache_dir()
        _board_path(universe).write_text(text, encoding="utf-8")
        (root / "universe_board.json").write_text(text, encoding="utf-8")
    except OSError:
        pass


def _read_board_file(path) -> tuple[pd.DataFrame, str | None, str | None]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("rows") or []
    return pd.DataFrame(rows), payload.get("as_of"), payload.get("universe")


def load_board(universe: str | None = None) -> tuple[pd.DataFrame, str | None, str | None]:
    from aieq.store import get_doc

    key = _board_mem_key(universe)
    with _board_lock:
        hit = _board_mem.get(key)
        if hit is not None:
            df, as_of, uni = hit
            if universe and uni and uni != universe:
                pass
            else:
                return df.copy(), as_of, uni or universe
    try:
        if universe:
            named = get_doc("board", key)
            if named:
                rows = named.get("rows") or []
                out = pd.DataFrame(rows), named.get("as_of"), named.get("universe")
                _remember_board(out[0], out[1], out[2] or universe)
                return out
        latest = get_doc("board", "default")
        if latest:
            uni = latest.get("universe")
            if universe and uni and uni != universe:
                return pd.DataFrame(), None, universe
            rows = latest.get("rows") or []
            out = pd.DataFrame(rows), latest.get("as_of"), uni or universe
            _remember_board(out[0], out[1], out[2])
            return out
    except Exception:
        pass
    named = _board_path(universe)
    try:
        if universe and named.exists() and named != BOARD_PATH:
            loaded = _read_board_file(named)
            _remember_board(*loaded)
            return loaded
        if BOARD_PATH.exists():
            df, as_of, uni = _read_board_file(BOARD_PATH)
            if universe and uni and uni != universe:
                return pd.DataFrame(), None, universe
            _remember_board(df, as_of, uni or universe)
            return df, as_of, uni or universe
    except Exception:
        pass
    return pd.DataFrame(), None, universe


def _json_clean(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _json_clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_clean(x) for x in obj]
    if obj is None:
        return None
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    if isinstance(obj, (np.integer, int)) and not isinstance(obj, bool):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        if pd.isna(obj) or obj != obj:
            return None
        return float(obj)
    if isinstance(obj, str):
        return obj
    try:
        if pd.isna(obj):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(obj, "item"):
        try:
            return _json_clean(obj.item())
        except Exception:
            return str(obj)
    if isinstance(obj, (pd.Timestamp, datetime)):
        return str(obj)
    return obj


def result_to_jsonable(res: AnalysisResult) -> dict[str, Any]:
    c = res.consensus
    why = write_why(
        res.ticker,
        res.name,
        c.rating,
        c.score,
        c.confidence,
        c.agreement,
        c.votes,
        price=res.price,
    )
    eq = res.backtest.equity
    equity = []
    equity_bh = []
    if eq is not None and len(eq):
        equity = [
            {"date": str(i.date()) if hasattr(i, "date") else str(i), "value": float(v)}
            for i, v in eq.items()
        ]
    bh = res.backtest.benchmark
    if bh is not None and len(bh):
        equity_bh = [
            {"date": str(i.date()) if hasattr(i, "date") else str(i), "value": float(v)}
            for i, v in bh.items()
        ]
    candles = []
    if res.ohlcv is not None and not res.ohlcv.empty:
        tail = res.ohlcv.tail(252)
        for i, row in tail.iterrows():
            candles.append(
                {
                    "date": str(i.date()) if hasattr(i, "date") else str(i),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]) if "volume" in row else 0.0,
                }
            )
    payload = {
        "ticker": res.ticker,
        "queried": res.queried,
        "name": res.name,
        "as_of": res.as_of,
        "price": res.price,
        "backend": res.backend,
        "why": why,
        "consensus": {
            "rating": c.rating,
            "score": c.score,
            "confidence": c.confidence,
            "agreement": c.agreement,
            "thesis": c.thesis,
            "ml_weight_used": c.ml_weight_used,
            "votes": [asdict(v) for v in c.votes],
        },
        "model": res.model_metrics,
        "backtest": res.backtest.metrics,
        "equity": equity,
        "equity_bh": equity_bh,
        "company_blurb": format_company_blurb(res.fundamentals),
        "candles": candles,
        "options": [asdict(o) for o in res.options],
        "fundamentals": {k: v for k, v in res.fundamentals.items() if not isinstance(v, (pd.Series, pd.DataFrame))},
        "feature_importance": res.feature_importance,
        "news": res.news,
        "geo_news": res.geo_news,
    }
    return _json_clean(payload)
