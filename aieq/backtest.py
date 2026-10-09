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
    """Sortino vs a 0% target. Downside deviation uses every bar, with gains counted as zero."""
    r = returns.dropna().to_numpy(dtype=float)
    if len(r) < 5:
        return 0.0
    downside = np.minimum(r, 0.0)
    dev = float(np.sqrt(np.mean(downside ** 2)))
    if dev == 0.0:
        return 0.0
    return float(np.sqrt(periods) * r.mean() / dev)


def _calendar_years(index: pd.Index) -> float:
    if len(index) < 2:
        return 0.0
    try:
        start = pd.Timestamp(index[0])
        end = pd.Timestamp(index[-1])
        if start.tzinfo is not None:
            start = start.tz_convert("UTC").tz_localize(None)
        if end.tzinfo is not None:
            end = end.tz_convert("UTC").tz_localize(None)
        days = (end.normalize() - start.normalize()).days
        return max(int(days), 1) / 365.25
    except Exception:
        return 0.0


def _cagr(equity: pd.Series, periods: int = 252) -> float:
    if len(equity) < 2 or equity.iloc[0] <= 0:
        return 0.0
    if equity.iloc[-1] <= 0:
        return -1.0
    years = _calendar_years(equity.index)
    if years <= 0:
        years = len(equity) / periods
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
    in_market = returns.where(positions.abs() > 1e-9).dropna()
    day_hit = float((in_market > 0).mean()) if len(in_market) else 0.0
    if not trades.empty and "pnl" in trades.columns:
        hit = float((trades["pnl"] > 0).mean())
    else:
        hit = day_hit
    return {
        "cagr": cagr,
        "total_return": float(equity.iloc[-1] / equity.iloc[0] - 1) if len(equity) else 0.0,
        "sharpe": _sharpe(r),
        "sortino": _sortino(r),
        "max_drawdown": dd,
        "calmar": calmar,
        "hit_rate": hit,
        "day_hit_rate": day_hit,
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


def _legs(open_: pd.Series, close: pd.Series) -> pd.Series:
    """Return from this bar's open to the next open. The last bar is marked at its close."""
    nxt = open_.shift(-1)
    leg = nxt / open_ - 1.0
    last_open = float(open_.iloc[-1])
    last_close = float(close.iloc[-1])
    if last_open > 0 and np.isfinite(last_open) and np.isfinite(last_close):
        leg.iloc[-1] = last_close / last_open - 1.0
    else:
        leg.iloc[-1] = 0.0
    return leg.replace([np.inf, -np.inf], np.nan).fillna(0.0)


def _trades(held: pd.Series, open_: pd.Series, close: pd.Series, one_way: float) -> pd.DataFrame:
    """Round trips at the open fill. An open position is marked at the last close, without an exit cost."""
    rows: list[dict] = []
    cur = 0.0
    entry_i = None
    entry_px = None
    for i, (ts, p) in enumerate(held.items()):
        p = float(p)
        if abs(p - cur) <= 1e-9:
            continue
        fill = float(open_.iloc[i])
        if abs(cur) > 1e-9 and entry_px not in (None, 0) and fill > 0:
            rows.append(
                {
                    "entry": entry_i,
                    "exit": ts,
                    "side": "LONG" if cur > 0 else "SHORT",
                    "entry_px": entry_px,
                    "exit_px": fill,
                    "pnl": (fill / entry_px - 1.0) * cur - 2.0 * one_way,
                    "closed": 1.0,
                }
            )
        if abs(p) > 1e-9 and fill > 0:
            entry_i = ts
            entry_px = fill
        else:
            entry_i = None
            entry_px = None
        cur = p
    if abs(cur) > 1e-9 and entry_px not in (None, 0):
        mark = float(close.iloc[-1])
        if mark > 0:
            rows.append(
                {
                    "entry": entry_i,
                    "exit": held.index[-1],
                    "side": "LONG" if cur > 0 else "SHORT",
                    "entry_px": entry_px,
                    "exit_px": mark,
                    "pnl": (mark / entry_px - 1.0) * cur - one_way,
                    "closed": 0.0,
                }
            )
    return pd.DataFrame(rows)


def run_backtest(
    close: pd.Series,
    p_up: pd.Series,
    settings: Settings = DEFAULT,
    long_only: bool = True,
    open_: pd.Series | None = None,
) -> BacktestReport:
    """Signal at the close, filled at the next open.

    A probability known at the close of bar t is not allowed to trade that close.
    The order goes out for the next session's open and earns the path from that
    open to the following open (last bar marked at its close). Commission and
    slippage are charged on the shares turned over at the fill, once per side.
    Buy-and-hold uses the same window: in at the first achievable open, out at
    the last close, with no signal and no costs.
    """
    parts: dict[str, pd.Series] = {"close": close, "p_up": p_up}
    if open_ is not None:
        parts["open"] = open_.reindex(close.index)
    aligned = pd.concat(parts, axis=1)
    need = ["close", "p_up"] + (["open"] if open_ is not None else [])
    aligned = aligned.dropna(subset=need)
    if open_ is not None:
        aligned = aligned.loc[(aligned["open"] > 0) & (aligned["close"] > 0)]
    if aligned.empty or len(aligned) < 2:
        empty = pd.Series(dtype=float)
        return BacktestReport(empty, empty, empty, pd.DataFrame(), {}, empty)

    desired = _position_series(aligned["p_up"], settings, long_only=long_only)
    one_way = float(settings.one_way_cost)
    use_open = open_ is not None and "open" in aligned.columns
    if use_open:
        # Decision at close t-1 is the position that starts at open t.
        held = desired.shift(1).fillna(0.0)
        ret = _legs(aligned["open"], aligned["close"])
        fills = aligned["open"]
        marks = aligned["close"]
    else:
        # No opens: fill at the next close, so the signal bar's own close-to-close move is skipped.
        held = desired.shift(2).fillna(0.0)
        ret = aligned["close"].pct_change().fillna(0.0)
        fills = aligned["close"].shift(1).fillna(aligned["close"])
        marks = aligned["close"]

    turnover = held.diff().abs()
    turnover.iloc[0] = abs(float(held.iloc[0]))
    strat = held * ret - turnover * one_way
    equity = (1.0 + strat).cumprod()

    # Same dates a strategy could actually be invested: flat on the signal bar, long from the first fill.
    bh_pos = pd.Series(0.0, index=held.index)
    if len(bh_pos) > 1:
        bh_pos.iloc[1:] = 1.0
    benchmark = (1.0 + bh_pos * ret).cumprod()

    trades = _trades(held, fills, marks, one_way)
    metrics = summarize(equity, strat, held, trades)
    metrics["buy_hold_return"] = float(benchmark.iloc[-1] / benchmark.iloc[0] - 1) if len(benchmark) else 0.0
    metrics["excess_vs_bh"] = metrics["total_return"] - metrics["buy_hold_return"]
    metrics["long_only"] = 1.0 if long_only else 0.0
    metrics["one_way_cost"] = one_way
    return BacktestReport(
        equity=equity,
        returns=strat,
        positions=held,
        trades=trades,
        metrics=metrics,
        benchmark=benchmark,
    )
