from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from aieq.config import Settings, DEFAULT


@dataclass
class BacktestReport:
    equity: pd.Series
    returns: pd.Series
    positions: pd.Series
    trades: pd.DataFrame
    metrics: dict[str, float] = field(default_factory=dict)
    benchmark: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))


def _max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    dd = equity / peak - 1.0
    return float(dd.min()) if len(dd) else 0.0


def _sharpe(returns: pd.Series, periods: int = 252) -> float:
    r = returns.dropna()
    if r.std() == 0 or len(r) < 5:
        return 0.0
    return float(np.sqrt(periods) * r.mean() / r.std())


def _sortino(returns: pd.Series, periods: int = 252) -> float:
    r = returns.dropna()
    downside = r[r < 0]
    if len(downside) == 0 or downside.std() == 0:
        return 0.0
    return float(np.sqrt(periods) * r.mean() / downside.std())


def _cagr(equity: pd.Series, periods: int = 252) -> float:
    if len(equity) < 2 or equity.iloc[0] <= 0:
        return 0.0
    years = len(equity) / periods
    if years <= 0:
        return 0.0
    return float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1)


def summarize(equity: pd.Series, returns: pd.Series, positions: pd.Series, trades: pd.DataFrame) -> dict[str, float]:
    r = returns.dropna()
    wins = trades.loc[trades["pnl"] > 0, "pnl"] if not trades.empty and "pnl" in trades.columns else pd.Series(dtype=float)
    losses = trades.loc[trades["pnl"] < 0, "pnl"] if not trades.empty and "pnl" in trades.columns else pd.Series(dtype=float)
    gross_win = float(wins.sum()) if len(wins) else 0.0
    gross_loss = float(losses.abs().sum()) if len(losses) else 0.0
    dd = _max_drawdown(equity)
    cagr = _cagr(equity)
    calmar = float(cagr / abs(dd)) if dd < 0 else 0.0
    hit = float((r > 0).mean()) if len(r) else 0.0
    return {
        "cagr": cagr,
        "total_return": float(equity.iloc[-1] / equity.iloc[0] - 1) if len(equity) else 0.0,
        "sharpe": _sharpe(r),
        "sortino": _sortino(r),
        "max_drawdown": dd,
        "calmar": calmar,
        "hit_rate": hit,
        "profit_factor": float(gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0,
        "n_trades": float(len(trades)),
        "avg_trade": float(trades["pnl"].mean()) if not trades.empty else 0.0,
        "exposure": float(positions.abs().mean()) if len(positions) else 0.0,
        "long_bias": float((positions > 0).mean()) if len(positions) else 0.0,
    }


def _position_series(p_up: pd.Series, settings: Settings, long_only: bool) -> pd.Series:
    """Hysteresis positions from model P(up): reduces flip-flopping and avoids shorting single names."""
    entry = float(settings.long_threshold)
    exit_ = float(settings.exit_threshold)
    short_in = float(settings.short_threshold)
    states: list[float] = []
    state = 0.0
    for p in p_up:
        prob = float(p) if pd.notna(p) else 0.5
        if long_only:
            if state == 0.0 and prob >= entry:
                state = 1.0
            elif state == 1.0 and prob <= exit_:
                state = 0.0
        else:
            if state == 0.0:
                if prob >= entry:
                    state = 1.0
                elif prob <= short_in:
                    state = -1.0
            elif state == 1.0 and prob <= exit_:
                state = 0.0
            elif state == -1.0 and prob >= entry:
                state = 0.0
        states.append(state)
    return pd.Series(states, index=p_up.index)


def run_backtest(
    close: pd.Series,
    p_up: pd.Series,
    settings: Settings = DEFAULT,
    long_only: bool = True,
) -> BacktestReport:
    """Signal at close t, position from t+1. Long-only by default for single-name equity research."""
    aligned = pd.concat({"close": close, "p_up": p_up}, axis=1).dropna()
    if aligned.empty:
        empty = pd.Series(dtype=float)
        return BacktestReport(empty, empty, empty, pd.DataFrame(), {}, empty)

    desired = _position_series(aligned["p_up"], settings, long_only=long_only)
    # Trade next bar — no look-ahead.
    pos = desired.shift(1).fillna(0.0)

    ret = aligned["close"].pct_change().fillna(0.0)
    turnover = pos.diff().abs().fillna(pos.abs())
    cost = turnover * settings.round_trip_cost
    strat = pos * ret - cost

    equity = (1.0 + strat).cumprod()
    equity.iloc[0] = 1.0

    benchmark = (1.0 + ret).cumprod()
    benchmark.iloc[0] = 1.0

    trades_rows = []
    cur = 0.0
    entry_i = None
    entry_px = None
    px_series = aligned["close"]
    for i, (ts, p) in enumerate(pos.items()):
        if p != cur:
            if cur != 0 and entry_i is not None:
                exit_px = float(px_series.iloc[i])
                pnl = (exit_px / entry_px - 1.0) * cur
                trades_rows.append(
                    {
                        "entry": entry_i,
                        "exit": ts,
                        "side": "LONG" if cur > 0 else "SHORT",
                        "entry_px": entry_px,
                        "exit_px": exit_px,
                        "pnl": pnl - settings.round_trip_cost,
                    }
                )
            if p != 0:
                entry_i = ts
                entry_px = float(px_series.iloc[i])
            else:
                entry_i = None
                entry_px = None
            cur = p
    trades = pd.DataFrame(trades_rows)
    metrics = summarize(equity, strat, pos, trades)
    metrics["buy_hold_return"] = float(benchmark.iloc[-1] / benchmark.iloc[0] - 1) if len(benchmark) else 0.0
    metrics["excess_vs_bh"] = metrics["total_return"] - metrics["buy_hold_return"]
    metrics["long_only"] = 1.0 if long_only else 0.0
    return BacktestReport(
        equity=equity,
        returns=strat,
        positions=pos,
        trades=trades,
        metrics=metrics,
        benchmark=benchmark,
    )
