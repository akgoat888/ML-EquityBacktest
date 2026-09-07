from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from aieq.config import Settings, DEFAULT


def _make_xgb():
    try:
        from xgboost import XGBClassifier, XGBRegressor

        clf = XGBClassifier(
            n_estimators=350,
            max_depth=3,
            learning_rate=0.03,
            subsample=0.80,
            colsample_bytree=0.80,
            min_child_weight=6,
            reg_lambda=6.0,
            reg_alpha=0.8,
            objective="binary:logistic",
            eval_metric="logloss",
            tree_method="hist",
            n_jobs=1,
            random_state=DEFAULT.random_state,
            verbosity=0,
        )
        reg = XGBRegressor(
            n_estimators=300,
            max_depth=3,
            learning_rate=0.03,
            subsample=0.80,
            colsample_bytree=0.80,
            min_child_weight=6,
            reg_lambda=6.0,
            reg_alpha=0.8,
            objective="reg:squarederror",
            tree_method="hist",
            n_jobs=1,
            random_state=DEFAULT.random_state,
            verbosity=0,
        )
        return clf, reg, "xgboost"
    except Exception:
        from sklearn.ensemble import GradientBoostingClassifier, GradientBoostingRegressor  # type: ignore

        clf = GradientBoostingClassifier(
            n_estimators=180,
            max_depth=3,
            learning_rate=0.05,
            subsample=0.8,
            random_state=DEFAULT.random_state,
        )
        reg = GradientBoostingRegressor(
            n_estimators=160,
            max_depth=3,
            learning_rate=0.05,
            subsample=0.8,
            random_state=DEFAULT.random_state,
        )
        return clf, reg, "sklearn_gbm"


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
    # Mann–Whitney / Wilcoxon rank-sum, ties count as 0.5
    order = np.argsort(np.concatenate([neg, pos]), kind="mergesort")
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, len(order) + 1, dtype=float)
    n_neg = len(neg)
    n_pos = len(pos)
    pos_ranks = ranks[n_neg:]
    u = float(pos_ranks.sum() - n_pos * (n_pos + 1) / 2.0)
    return u / (n_pos * n_neg)


def _importance_map(model: Any, names: list[str]) -> dict[str, float]:
    imp = None
    if hasattr(model, "feature_importances_"):
        imp = np.asarray(model.feature_importances_, dtype=float)
    if imp is None or len(imp) != len(names):
        return {}
    total = float(imp.sum()) or 1.0
    pairs = sorted(zip(names, (imp / total).tolist()), key=lambda x: x[1], reverse=True)
    return dict(pairs[:20])


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
    last_reg = None
    backend = "xgboost"
    names = list(X.columns)

    start = min_train
    while do_oos and start + 5 < n:
        train_end = start
        test_end = min(n, start + test)
        # Purge last `embargo` train rows so labels don't overlap the test window.
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

        clf, reg, backend = _make_xgb()
        clf.fit(X_tr, y_tr)
        reg.fit(X_tr, r_tr)
        last_clf, last_reg = clf, reg

        if hasattr(clf, "predict_proba"):
            p_up = clf.predict_proba(X_te)[:, 1]
        else:
            p_up = clf.predict(X_te).astype(float)
        exp_ret = np.asarray(reg.predict(X_te), dtype=float)

        fold = pd.DataFrame(
            {
                "p_up": p_up,
                "exp_ret": exp_ret,
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

    # Live model: train on all labeled rows (features through T, labels known through T-horizon).
    live_p, live_r = 0.5, 0.0
    live_model = None
    if len(X) >= 150 and y_cls.nunique() >= 2:
        clf, reg, backend = _make_xgb()
        clf.fit(X, y_cls)
        reg.fit(X, y_reg)
        live_model = clf
        last_clf = clf
        last_reg = reg
        row = live_row if live_row is not None else X.iloc[[-1]]
        row = row.reindex(columns=names).fillna(0.0)
        if hasattr(clf, "predict_proba"):
            live_p = float(clf.predict_proba(row)[0, 1])
        else:
            live_p = float(clf.predict(row)[0])
        live_r = float(reg.predict(row)[0])

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
