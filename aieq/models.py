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


def _fit_classifier(X, y):
    DMatrix, train = _xgb_train()
    params = {
        "max_depth": 3,
        "eta": 0.03,
        "subsample": 0.80,
        "colsample_bytree": 0.80,
        "min_child_weight": 6,
        "lambda": 6.0,
        "alpha": 0.8,
        "objective": "binary:logistic",
        "eval_metric": "logloss",
        "tree_method": "hist",
        "nthread": 1,
        "seed": DEFAULT.random_state,
        "verbosity": 0,
    }
    return train(params, _matrix(DMatrix, X, y), num_boost_round=350)


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
        fold = pd.DataFrame(
            {
                "p_up": _predict(clf, X_te),
                "exp_ret": _predict(reg, X_te),
                "y": y_te.to_numpy(),
                "fwd_ret": r_te.to_numpy(),
            },
            index=X_te.index,
        )
        oos_rows.append(fold)
        start = test_end

    oos = pd.concat(oos_rows) if oos_rows else pd.DataFrame(columns=["p_up", "exp_ret", "y", "fwd_ret"])

    metrics: dict[str, float] = {}
    if not oos.empty:
        p = oos["p_up"].to_numpy()
        y = oos["y"].to_numpy()
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

    live_p, live_r = 0.5, 0.0
    live_model = None
    if len(X) >= 150 and y_cls.nunique() >= 2:
        clf = _fit_classifier(X, y_cls)
        reg = _fit_regressor(X, y_reg)
        live_model = clf
        last_clf = clf
        row = live_row if live_row is not None else X.iloc[[-1]]
        row = row.reindex(columns=names).fillna(0.0)
        live_p = float(_predict(clf, row)[0])
        live_r = float(_predict(reg, row)[0])

    importance = _importance_map(last_clf, names) if last_clf is not None else {}

    return WalkForwardResult(
        oos=oos,
        feature_importance=importance,
        backend=backend,
        live_p_up=live_p,
        live_expected_ret=live_r,
        metrics=metrics,
        live_model=live_model,
        feature_names=names,
    )
