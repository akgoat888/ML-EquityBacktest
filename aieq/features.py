from __future__ import annotations

import numpy as np
import pandas as pd


def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev = close.shift(1)
    return pd.concat([(high - low), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    line = ema_fast - ema_slow
    sig = line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame({"macd": line, "macd_signal": sig, "macd_hist": line - sig})


def stochastic(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14, d: int = 3) -> pd.DataFrame:
    ll = low.rolling(n).min()
    hh = high.rolling(n).max()
    k = 100.0 * (close - ll) / (hh - ll).replace(0.0, np.nan)
    return pd.DataFrame({"stoch_k": k, "stoch_d": k.rolling(d).mean()})


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    return _true_range(high, low, close).ewm(alpha=1 / n, adjust=False).mean()


def adx(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.DataFrame:
    up = high.diff()
    down = -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = _true_range(high, low, close)
    atr_s = tr.ewm(alpha=1 / n, adjust=False).mean()
    plus_di = 100.0 * pd.Series(plus_dm, index=close.index).ewm(alpha=1 / n, adjust=False).mean() / atr_s.replace(0, np.nan)
    minus_di = 100.0 * pd.Series(minus_dm, index=close.index).ewm(alpha=1 / n, adjust=False).mean() / atr_s.replace(0, np.nan)
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan) * 100.0
    return pd.DataFrame({"adx": dx.ewm(alpha=1 / n, adjust=False).mean(), "plus_di": plus_di, "minus_di": minus_di})


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std()
    upper = mid + k * sd
    lower = mid - k * sd
    width = (upper - lower) / mid.replace(0, np.nan)
    pct_b = (close - lower) / (upper - lower).replace(0, np.nan)
    return pd.DataFrame({"bb_mid": mid, "bb_upper": upper, "bb_lower": lower, "bb_width": width, "bb_pct_b": pct_b})


def build_features(
    ohlcv: pd.DataFrame,
    spy: pd.DataFrame | None = None,
    vix: pd.DataFrame | None = None,
    qqq: pd.DataFrame | None = None,
) -> pd.DataFrame:
    df = ohlcv.copy()
    c, h, l, o, v = df["close"], df["high"], df["low"], df["open"], df["volume"]

    for w in (1, 2, 3, 5, 10, 21, 63, 126):
        df[f"ret_{w}"] = c.pct_change(w, fill_method=None)

    df["log_ret"] = np.log(c / c.shift(1))
    df["intraday_ret"] = c / o.replace(0, np.nan) - 1.0
    df["gap"] = o / c.shift(1) - 1.0
    df["hl_range"] = (h - l) / c.replace(0, np.nan)
    df["close_loc"] = (c - l) / (h - l).replace(0, np.nan)

    for w in (10, 20, 50, 100, 200):
        sma = c.rolling(w).mean()
        df[f"sma_{w}"] = sma
        df[f"dist_sma_{w}"] = c / sma.replace(0, np.nan) - 1.0

    df["sma_stack"] = (
        (df["sma_10"] > df["sma_20"]).astype(float)
        + (df["sma_20"] > df["sma_50"]).astype(float)
        + (df["sma_50"] > df["sma_200"]).astype(float)
        - 1.5
    ) / 1.5

    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    df["dist_ema_12"] = c / ema12 - 1.0
    df["dist_ema_26"] = c / ema26 - 1.0
    df = pd.concat([df, macd(c)], axis=1)
    df["rsi_14"] = rsi(c, 14)
    df["rsi_7"] = rsi(c, 7)
    df = pd.concat([df, stochastic(h, l, c)], axis=1)
    df["atr_14"] = atr(h, l, c, 14)
    df["atr_pct"] = df["atr_14"] / c
    df = pd.concat([df, adx(h, l, c)], axis=1)
    df = pd.concat([df, bollinger(c)], axis=1)

    vol_mean = v.rolling(20).mean()
    vol_std = v.rolling(20).std()
    df["vol_z"] = (v - vol_mean) / vol_std.replace(0, np.nan)
    df["vol_ratio"] = v / vol_mean.replace(0, np.nan)
    obv = (np.sign(c.diff().fillna(0)) * v).cumsum()
    df["obv_slope"] = obv.pct_change(10, fill_method=None).replace([np.inf, -np.inf], np.nan)

    for w in (10, 21, 63):
        df[f"rvol_{w}"] = df["log_ret"].rolling(w).std() * np.sqrt(252)

    up = (c.diff() > 0).astype(int)
    down = (c.diff() < 0).astype(int)
    df["up_days"] = up * (up.groupby((up != up.shift()).cumsum()).cumcount() + 1)
    df["down_days"] = down * (down.groupby((down != down.shift()).cumsum()).cumcount() + 1)

    rolling_max = c.rolling(252, min_periods=60).max()
    rolling_min = c.rolling(252, min_periods=60).min()
    df["dist_52w_high"] = c / rolling_max - 1.0
    df["dist_52w_low"] = c / rolling_min - 1.0

    idx = pd.to_datetime(df.index)
    df["dow_sin"] = np.sin(2 * np.pi * idx.dayofweek / 5.0)
    df["dow_cos"] = np.cos(2 * np.pi * idx.dayofweek / 5.0)
    df["month_sin"] = np.sin(2 * np.pi * idx.month / 12.0)
    df["month_cos"] = np.cos(2 * np.pi * idx.month / 12.0)

    if spy is not None and not spy.empty:
        spy_c = spy["close"].reindex(df.index).ffill()
        spy_ret = spy_c.pct_change(fill_method=None)
        df["spy_ret_1"] = spy_ret
        df["spy_ret_5"] = spy_c.pct_change(5, fill_method=None)
        df["spy_ret_21"] = spy_c.pct_change(21, fill_method=None)
        df["rs_spy_21"] = df["ret_21"] - df["spy_ret_21"]
        df["rs_spy_63"] = df["ret_63"] - spy_c.pct_change(63, fill_method=None)
        cov = df["log_ret"].rolling(63).cov(np.log(spy_c / spy_c.shift(1)))
        var = np.log(spy_c / spy_c.shift(1)).rolling(63).var()
        df["beta_63"] = cov / var.replace(0, np.nan)
        df["spy_sma_50"] = spy_c / spy_c.rolling(50).mean() - 1.0
        df["spy_sma_200"] = spy_c / spy_c.rolling(200).mean() - 1.0

    if qqq is not None and not qqq.empty:
        qqq_c = qqq["close"].reindex(df.index).ffill()
        df["rs_qqq_21"] = df["ret_21"] - qqq_c.pct_change(21, fill_method=None)

    if vix is not None and not vix.empty:
        vix_c = vix["close"].reindex(df.index).ffill()
        df["vix"] = vix_c
        df["vix_chg_5"] = vix_c.pct_change(5, fill_method=None)
        df["vix_z"] = (vix_c - vix_c.rolling(63).mean()) / vix_c.rolling(63).std().replace(0, np.nan)
        df["vix_sma_20"] = vix_c / vix_c.rolling(20).mean() - 1.0

    df["ret_skew_21"] = df["log_ret"].rolling(21).skew()
    df["ret_kurt_21"] = df["log_ret"].rolling(21).kurt()

    return df


FEATURE_COLS = [
    "ret_1", "ret_2", "ret_3", "ret_5", "ret_10", "ret_21", "ret_63", "ret_126",
    "log_ret", "intraday_ret", "gap", "hl_range", "close_loc",
    "dist_sma_10", "dist_sma_20", "dist_sma_50", "dist_sma_100", "dist_sma_200",
    "sma_stack", "dist_ema_12", "dist_ema_26",
    "macd", "macd_signal", "macd_hist",
    "rsi_14", "rsi_7", "stoch_k", "stoch_d",
    "atr_pct", "adx", "plus_di", "minus_di",
    "bb_width", "bb_pct_b",
    "vol_z", "vol_ratio", "obv_slope",
    "rvol_10", "rvol_21", "rvol_63",
    "up_days", "down_days",
    "dist_52w_high", "dist_52w_low",
    "dow_sin", "dow_cos", "month_sin", "month_cos",
    "spy_ret_1", "spy_ret_5", "spy_ret_21", "rs_spy_21", "rs_spy_63",
    "beta_63", "spy_sma_50", "spy_sma_200",
    "rs_qqq_21", "vix", "vix_chg_5", "vix_z", "vix_sma_20",
    "ret_skew_21", "ret_kurt_21",
]


def make_xy(feat: pd.DataFrame, horizon: int = 5, feature_cols: list[str] | None = None) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    cols = [c for c in (feature_cols or FEATURE_COLS) if c in feat.columns]
    X = feat[cols].copy().replace([np.inf, -np.inf], np.nan)
    X = X.loc[:, X.notna().mean() > 0.65]
    fwd = feat["close"].shift(-horizon) / feat["close"] - 1.0
    y_reg = fwd.rename("fwd_ret")
    y_cls = (fwd > 0).astype(int).rename("fwd_up")
    valid = X.dropna().index.intersection(y_reg.dropna().index)
    return X.loc[valid], y_cls.loc[valid], y_reg.loc[valid]


def latest_feature_row(feat: pd.DataFrame, feature_cols: list[str] | None = None) -> pd.DataFrame:
    cols = [c for c in (feature_cols or FEATURE_COLS) if c in feat.columns]
    row = feat[cols].replace([np.inf, -np.inf], np.nan).ffill().iloc[[-1]]
    return row.fillna(0.0)
