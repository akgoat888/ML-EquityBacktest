from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from aieq.config import Settings, DEFAULT


def _xgb_train():
    """Native booster API — XGBClassifier pulls sklearn, which is not on Vercel."""
    from xgboost.core import DMatrix
    from xgboost.training import train

    return DMatrix, train


def _matrix(DMatrix, X, y=None):
    names = list(X.columns) if hasattr(X, "columns") else None
    data = np.asarray(X, dtype=np.float32)
    if y is None:
        return DMatrix(data, feature_names=names)
    return DMatrix(data, label=np.asarray(y, dtype=np.float32), feature_names=names)


# Compact, stable inputs for the logistic baseline and the meta-labeler.
LOGIT_COLS = (
    "ret_5", "ret_21", "ret_63", "dist_sma_50", "dist_sma_200",
    "rsi_14", "macd_hist", "rs_spy_21", "beta_63", "vix_z", "vol_z", "atr_pct",
)


def _fit_classifier(X, y, *, max_depth: int = 3, num_boost_round: int = 350, min_child_weight: float = 6):
    DMatrix, train = _xgb_train()
    params = {
        "max_depth": max_depth,
        "eta": 0.03,
        "subsample": 0.80,
        "colsample_bytree": 0.80,
        "min_child_weight": min_child_weight,
        "lambda": 6.0,
        "alpha": 0.8,
        "objective": "binary:logistic",
        "eval_metric": "logloss",
        "tree_method": "hist",
        "nthread": 1,
        "seed": DEFAULT.random_state,
        "verbosity": 0,
    }
    return train(params, _matrix(DMatrix, X, y), num_boost_round=num_boost_round)


def _fit_regressor(X, y):
    DMatrix, train = _xgb_train()
    params = {
        "max_depth": 3,
        "eta": 0.03,
        "subsample": 0.80,
        "colsample_bytree": 0.80,
        "min_child_weight": 6,
        "lambda": 6.0,
        "alpha": 0.8,
        "objective": "reg:squarederror",
        "tree_method": "hist",
        "nthread": 1,
        "seed": DEFAULT.random_state,
        "verbosity": 0,
    }
    return train(params, _matrix(DMatrix, X, y), num_boost_round=300)


def _predict(model, X) -> np.ndarray:
    DMatrix, _ = _xgb_train()
    return np.asarray(model.predict(_matrix(DMatrix, X)), dtype=float)


@dataclass
class WalkForwardResult:
    oos: pd.DataFrame
    feature_importance: dict[str, float]
    backend: str
    live_p_up: float
    live_expected_ret: float
    metrics: dict[str, float] = field(default_factory=dict)
    live_model: Any = None
    feature_names: list[str] = field(default_factory=list)


def _accuracy_score(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    if len(y_true) == 0:
        return 0.0
    return float((np.asarray(y_true) == np.asarray(y_pred)).mean())


def _log_loss(y_true: np.ndarray, p: np.ndarray) -> float:
    y = np.asarray(y_true, dtype=float)
    prob = np.clip(np.asarray(p, dtype=float), 1e-6, 1.0 - 1e-6)
    if len(y) == 0:
        return float("nan")
    return float(-(y * np.log(prob) + (1.0 - y) * np.log(1.0 - prob)).mean())


def _roc_auc_score(y_true: np.ndarray, scores: np.ndarray) -> float:
    y = np.asarray(y_true)
    s = np.asarray(scores, dtype=float)
    pos = s[y == 1]
    neg = s[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    order = np.argsort(np.concatenate([neg, pos]), kind="mergesort")
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, len(order) + 1, dtype=float)
    n_neg = len(neg)
    n_pos = len(pos)
    pos_ranks = ranks[n_neg:]
    u = float(pos_ranks.sum() - n_pos * (n_pos + 1) / 2.0)
    return u / (n_pos * n_neg)


def _importance_map(model: Any, names: list[str]) -> dict[str, float]:
    scores: dict[str, float] = {}
    if model is None:
        return {}
    try:
        raw = model.get_score(importance_type="gain")
        for key, val in (raw or {}).items():
            name = key
            if isinstance(key, str) and key.startswith("f") and key[1:].isdigit():
                idx = int(key[1:])
                if 0 <= idx < len(names):
                    name = names[idx]
            scores[str(name)] = float(val)
    except Exception:
        return {}
    if not scores:
        return {}
    total = float(sum(scores.values())) or 1.0
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return {k: v / total for k, v in ranked[:20]}


def _sigmoid(z: np.ndarray) -> np.ndarray:
    z = np.clip(np.asarray(z, dtype=float), -20.0, 20.0)
    return 1.0 / (1.0 + np.exp(-z))


def _logit(p: np.ndarray) -> np.ndarray:
    prob = np.clip(np.asarray(p, dtype=float), 1e-4, 1.0 - 1e-4)
    return np.log(prob / (1.0 - prob))


def _brier(y_true: np.ndarray, p: np.ndarray) -> float:
    y = np.asarray(y_true, dtype=float)
    prob = np.asarray(p, dtype=float)
    if len(y) == 0:
        return float("nan")
    return float(np.mean((prob - y) ** 2))


def _standardize_fit(Z: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mu = np.nanmean(Z, axis=0)
    sd = np.nanstd(Z, axis=0)
    mu = np.where(np.isfinite(mu), mu, 0.0)
    sd = np.where((~np.isfinite(sd)) | (sd < 1e-8), 1.0, sd)
    out = np.nan_to_num((Z - mu) / sd, nan=0.0, posinf=0.0, neginf=0.0)
    return out, mu, sd


def _standardize_apply(Z: np.ndarray, mu: np.ndarray, sd: np.ndarray) -> np.ndarray:
    return np.nan_to_num((Z - mu) / sd, nan=0.0, posinf=0.0, neginf=0.0)


def _fit_logistic(Z: np.ndarray, y: np.ndarray, l2: float = 1.0) -> np.ndarray | None:
    """L2-regularized logistic regression (IRLS). No sklearn."""
    y = np.asarray(y, dtype=float)
    Z = np.asarray(Z, dtype=float)
    if len(y) < 40 or Z.ndim != 2 or len(np.unique(y)) < 2:
        return None
    n, d = Z.shape
    Xb = np.column_stack([np.ones(n), Z])
    w = np.zeros(d + 1)
    reg = l2 * np.diag(np.r_[0.0, np.ones(d)])
    for _ in range(30):
        p = _sigmoid(Xb @ w)
        hess_w = np.clip(p * (1.0 - p), 1e-4, None)
        grad = Xb.T @ (p - y) + l2 * np.r_[0.0, w[1:]]
        H = Xb.T @ (Xb * hess_w[:, None]) + reg
        try:
            step = np.linalg.solve(H, grad)
        except np.linalg.LinAlgError:
            break
        w = w - step
        if float(np.max(np.abs(step))) < 1e-6:
            break
    return w


def _predict_logistic(w: np.ndarray | None, Z: np.ndarray) -> np.ndarray:
    if w is None or len(Z) == 0:
        return np.full(len(Z), 0.5)
    Xb = np.column_stack([np.ones(len(Z)), np.asarray(Z, dtype=float)])
    return _sigmoid(Xb @ w)


def _logit_matrix(frame: pd.DataFrame) -> tuple[np.ndarray | None, list[str]]:
    cols = [c for c in LOGIT_COLS if c in frame.columns]
    if len(cols) < 4:
        return None, cols
    return frame[cols].to_numpy(dtype=float), cols


def _blend_weights(y: np.ndarray, p_xgb: np.ndarray, p_logit: np.ndarray) -> float:
    """Share of the blend given to XGBoost. Logistic only earns weight when its OOS loss is competitive."""
    ll_x = _log_loss(y, p_xgb)
    ll_l = _log_loss(y, p_logit)
    if not np.isfinite(ll_x):
        return 0.75
    if not np.isfinite(ll_l) or ll_l > ll_x * 1.02:
        return 0.85
    inv_x = 1.0 / max(ll_x, 1e-3)
    inv_l = 1.0 / max(ll_l, 1e-3)
    return float(np.clip(inv_x / (inv_x + inv_l), 0.55, 0.9))


def _fit_platt(p: np.ndarray, y: np.ndarray) -> np.ndarray | None:
    return _fit_logistic(_logit(p).reshape(-1, 1), y, l2=0.5)


def _apply_platt(p: np.ndarray, w: np.ndarray | None) -> np.ndarray:
    if w is None:
        return np.asarray(p, dtype=float)
    return _predict_logistic(w, _logit(p).reshape(-1, 1))


def _meta_matrix(frame: pd.DataFrame, p: np.ndarray, exp: np.ndarray) -> np.ndarray:
    cols = [c for c in LOGIT_COLS if c in frame.columns]
    extra = np.column_stack([
        np.asarray(p, dtype=float),
        np.abs(np.asarray(p, dtype=float) - 0.5),
        np.asarray(exp, dtype=float),
    ])
    if not cols:
        return np.nan_to_num(extra, nan=0.0, posinf=0.0, neginf=0.0)
    base = frame[cols].to_numpy(dtype=float)
    return np.nan_to_num(np.column_stack([base, extra]), nan=0.0, posinf=0.0, neginf=0.0)


def _named_frame(arr: np.ndarray) -> pd.DataFrame:
    data = np.asarray(arr, dtype=float)
    if data.ndim == 1:
        data = data.reshape(1, -1)
    return pd.DataFrame(data, columns=[f"m{i}" for i in range(data.shape[1])])


def _shrink_toward_half(p: np.ndarray, meta: np.ndarray) -> np.ndarray:
    """Pull weak calls toward 50% when the meta-label says the direction call is unreliable."""
    scale = np.clip((np.asarray(meta, dtype=float) - 0.50) / 0.22, 0.0, 1.0)
    prob = np.asarray(p, dtype=float)
    return 0.5 + (prob - 0.5) * scale


def _safe_auc(y_true: np.ndarray, p: np.ndarray) -> float:
    try:
        if len(np.unique(y_true)) < 2:
            return float("nan")
        return float(_roc_auc_score(y_true, p))
    except Exception:
        return float("nan")


def walk_forward(
    X: pd.DataFrame,
    y_cls: pd.Series,
    y_reg: pd.Series,
    settings: Settings = DEFAULT,
    live_row: pd.DataFrame | None = None,
    do_oos: bool = True,
) -> WalkForwardResult:
    n = len(X)
    min_train = settings.min_train_bars
    test = settings.test_bars
    embargo = max(settings.embargo_bars, settings.horizon)

    if n < min_train + test + 20:
        min_train = max(252, n // 2)

    oos_rows: list[pd.DataFrame] = []
    last_clf = None
    backend = "xgboost"
    names = list(X.columns)

    start = min_train
    while do_oos and start + 5 < n:
        train_end = start
        test_end = min(n, start + test)
        tr_end_eff = max(60, train_end - embargo)
        X_tr, y_tr, r_tr = X.iloc[:tr_end_eff], y_cls.iloc[:tr_end_eff], y_reg.iloc[:tr_end_eff]
        X_te = X.iloc[start:test_end]
        y_te = y_cls.iloc[start:test_end]
        r_te = y_reg.iloc[start:test_end]
        if len(X_tr) < 120 or len(X_te) < 3:
            break
        if y_tr.nunique() < 2:
            start = test_end
            continue

        clf = _fit_classifier(X_tr, y_tr)
        reg = _fit_regressor(X_tr, r_tr)
        last_clf = clf
        p_xgb = _predict(clf, X_te)
        p_logit = np.full(len(X_te), np.nan)
        Z_tr, cols = _logit_matrix(X_tr)
        if Z_tr is not None:
            Z_tr_s, mu, sd = _standardize_fit(Z_tr)
            logit_w = _fit_logistic(Z_tr_s, y_tr.to_numpy())
            if logit_w is not None:
                Z_te = _standardize_apply(X_te[cols].to_numpy(dtype=float), mu, sd)
                p_logit = _predict_logistic(logit_w, Z_te)
        fold = pd.DataFrame(
            {
                "p_xgb": p_xgb,
                "p_logit": p_logit,
                "exp_ret": _predict(reg, X_te),
                "y": y_te.to_numpy(),
                "fwd_ret": r_te.to_numpy(),
            },
            index=X_te.index,
        )
        oos_rows.append(fold)
        start = test_end

    oos = pd.concat(oos_rows) if oos_rows else pd.DataFrame(columns=["p_xgb", "p_logit", "exp_ret", "y", "fwd_ret"])

    metrics: dict[str, float] = {}
    platt_w: np.ndarray | None = None
    meta_model = None
    blend_w = 0.8
    use_meta = False
    backend = "xgboost"

    if not oos.empty:
        y = oos["y"].to_numpy(dtype=float)
        p_xgb = np.clip(oos["p_xgb"].to_numpy(dtype=float), 1e-4, 1.0 - 1e-4)
        p_logit_raw = oos["p_logit"].to_numpy(dtype=float)
        metrics["oos_brier_xgb"] = _brier(y, p_xgb)
        metrics["oos_auc_xgb"] = _safe_auc(y, p_xgb)
        if np.isfinite(p_logit_raw).mean() > 0.8:
            p_logit = np.where(np.isfinite(p_logit_raw), p_logit_raw, p_xgb)
            p_logit = np.clip(p_logit, 1e-4, 1.0 - 1e-4)
            blend_w = _blend_weights(y, p_xgb, p_logit)
            backend = "xgboost+logit"
        else:
            p_logit = p_xgb
        p_blend = blend_w * p_xgb + (1.0 - blend_w) * p_logit
        metrics["blend_xgb_weight"] = float(blend_w)
        metrics["oos_brier"] = _brier(y, p_blend)

        # Platt scaling only if it improves Brier on a later OOS slice.
        p_cal = p_blend
        n_oos = len(y)
        split = int(n_oos * 0.7)
        if split >= 50 and n_oos - split >= 25 and len(np.unique(y[:split])) >= 2:
            hold_w = _fit_platt(p_blend[:split], y[:split])
            if hold_w is not None:
                raw_b = _brier(y[split:], p_blend[split:])
                cal_b = _brier(y[split:], _apply_platt(p_blend[split:], hold_w))
                if np.isfinite(cal_b) and cal_b < raw_b - 0.001:
                    platt_w = _fit_platt(p_blend, y)
                    p_cal = _apply_platt(p_blend, platt_w)
                    metrics["oos_brier_cal"] = _brier(y, p_cal)
                    backend += "+platt"
        if "oos_brier_cal" not in metrics:
            metrics["oos_brier_cal"] = metrics["oos_brier"]
            p_cal = p_blend

        # Meta-label: P(the direction call is right). Shrink when the holdout says it has skill.
        p_adj = p_cal
        correct = ((p_cal >= 0.5).astype(int) == y.astype(int)).astype(float)
        if split >= 50 and n_oos - split >= 25 and len(np.unique(correct[:split])) >= 2:
            meta_X = _meta_matrix(X.reindex(oos.index), p_cal, oos["exp_ret"].to_numpy(dtype=float))
            try:
                hold_meta = _fit_classifier(
                    _named_frame(meta_X[:split]),
                    pd.Series(correct[:split]),
                    max_depth=2,
                    num_boost_round=80,
                    min_child_weight=10,
                )
                hold_p = _predict(hold_meta, _named_frame(meta_X[split:]))
                hold_acc = float(((hold_p >= 0.5).astype(int) == correct[split:].astype(int)).mean())
                metrics["meta_holdout_acc"] = hold_acc
                if hold_acc >= 0.53:
                    meta_model = _fit_classifier(
                        _named_frame(meta_X),
                        pd.Series(correct),
                        max_depth=2,
                        num_boost_round=80,
                        min_child_weight=10,
                    )
                    meta_oos = _predict(meta_model, _named_frame(meta_X))
                    p_adj = _shrink_toward_half(p_cal, meta_oos)
                    use_meta = True
                    backend += "+meta"
                    metrics["meta_oos_mean"] = float(np.mean(meta_oos))
            except Exception:
                meta_model = None

        oos = oos.copy()
        oos["p_up"] = p_adj
        p = np.asarray(p_adj, dtype=float)
        pred = (p >= 0.5).astype(int)
        metrics["oos_accuracy"] = float(_accuracy_score(y, pred))
        metrics["oos_auc"] = _safe_auc(y, p)
        try:
            metrics["oos_logloss"] = float(_log_loss(y, p))
        except Exception:
            metrics["oos_logloss"] = float("nan")
        try:
            ic = float(pd.Series(p).corr(oos["fwd_ret"], method="spearman"))
            metrics["oos_ic"] = ic if ic == ic else 0.0
        except Exception:
            metrics["oos_ic"] = 0.0
        metrics["n_oos"] = float(len(oos))
        metrics["meta_used"] = 1.0 if use_meta else 0.0

    live_p, live_r = 0.5, 0.0
    live_meta = float("nan")
    live_model = None
    if len(X) >= 150 and y_cls.nunique() >= 2:
        clf = _fit_classifier(X, y_cls)
        reg = _fit_regressor(X, y_reg)
        live_model = clf
        last_clf = clf
        row = live_row if live_row is not None else X.iloc[[-1]]
        row = row.reindex(columns=names).fillna(0.0)
        p_xgb_live = float(_predict(clf, row)[0])
        live_r = float(_predict(reg, row)[0])
        p_logit_live = p_xgb_live
        Z_all, cols = _logit_matrix(X)
        if Z_all is not None:
            Z_s, mu, sd = _standardize_fit(Z_all)
            logit_w = _fit_logistic(Z_s, y_cls.to_numpy())
            if logit_w is not None:
                Z_row = _standardize_apply(row.reindex(columns=cols).to_numpy(dtype=float), mu, sd)
                p_logit_live = float(_predict_logistic(logit_w, Z_row)[0])
        live_p = blend_w * p_xgb_live + (1.0 - blend_w) * p_logit_live
        live_p = float(_apply_platt(np.array([live_p]), platt_w)[0])
        if use_meta and meta_model is not None:
            try:
                meta_row = _meta_matrix(row, np.array([live_p]), np.array([live_r]))
                live_meta = float(_predict(meta_model, _named_frame(meta_row))[0])
                live_p = float(_shrink_toward_half(np.array([live_p]), np.array([live_meta]))[0])
                metrics["meta_p"] = live_meta
            except Exception:
                metrics["meta_used"] = 0.0

    if "p_up" not in getattr(oos, "columns", []):
        oos = oos.copy()
        oos["p_up"] = np.nan

    importance = _importance_map(last_clf, names) if last_clf is not None else {}

    return WalkForwardResult(
        oos=oos,
        feature_importance=importance,
        backend=backend,
        live_p_up=float(live_p),
        live_expected_ret=float(live_r),
        metrics=metrics,
        live_model=live_model,
        feature_names=names,
    )
