"""
Ensemble + calibration layer for soccer: blends the Dixon-Coles scoreline
model with the Elo win/draw/loss heuristic, then applies isotonic
calibration on the home-win probability so that "we said 60%" actually
means ~60% empirically, not just "60% according to the model's internal
math." Basketball's ensemble (Elo x margin-model) is handled inline in
NBAModel.predict since it's simpler (no draw outcome to juggle).
"""
from __future__ import annotations

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import KFold


def best_blend_weight(dc_probs: np.ndarray, elo_probs: np.ndarray,
                       outcomes: np.ndarray, grid=None) -> float:
    """Grid-search alpha for p = alpha*dc + (1-alpha)*elo minimizing
    multiclass log-loss on a validation set. Returns alpha in [0,1]."""
    from backtest import log_loss_multiclass
    if grid is None:
        grid = np.linspace(0.0, 1.0, 21)
    best_alpha, best_ll = 1.0, np.inf
    for alpha in grid:
        blend = alpha * dc_probs + (1 - alpha) * elo_probs
        blend = blend / blend.sum(axis=1, keepdims=True)
        ll = log_loss_multiclass(blend, outcomes)
        if ll < best_ll:
            best_ll, best_alpha = ll, alpha
    return float(best_alpha), float(best_ll)


def blend_probs(dc_probs: np.ndarray, elo_probs: np.ndarray, alpha: float) -> np.ndarray:
    blend = alpha * dc_probs + (1 - alpha) * elo_probs
    return blend / blend.sum(axis=1, keepdims=True)


def _logit(p, eps=1e-6):
    p = np.clip(p, eps, 1 - eps)
    return np.log(p / (1 - p))


class HomeWinCalibrator:
    """Platt scaling (1-D logistic regression on the logit of P(home win))
    only. Draw/away probabilities are rescaled proportionally so the row
    still sums to 1. Platt scaling has just 2 free parameters (slope,
    intercept), so unlike isotonic regression it doesn't overfit on the
    few hundred holdout matches a single season of backtest data provides
    — isotonic was tried first and measurably *hurt* out-of-sample Brier
    score on small per-league validation folds, hence this choice."""

    def __init__(self):
        self.lr = LogisticRegression()
        self.fitted = False

    def fit(self, p_home: np.ndarray, home_win_indicator: np.ndarray):
        x = _logit(p_home).reshape(-1, 1)
        self.lr.fit(x, home_win_indicator)
        self.fitted = True
        return self

    def transform(self, probs: np.ndarray) -> np.ndarray:
        """probs: (N,3) [home,draw,away]. Recalibrates the home column and
        rescales draw/away to preserve sum-to-1."""
        if not self.fitted:
            return probs
        x = _logit(probs[:, 0]).reshape(-1, 1)
        p_home_new = self.lr.predict_proba(x)[:, 1]
        remainder_old = probs[:, 1] + probs[:, 2]
        remainder_new = 1 - p_home_new
        scale = np.where(remainder_old > 1e-9, remainder_new / remainder_old, 0.0)
        out = np.column_stack([
            p_home_new,
            probs[:, 1] * scale,
            probs[:, 2] * scale,
        ])
        out = np.clip(out, 1e-6, None)
        return out / out.sum(axis=1, keepdims=True)


def cv_calibration_helps(p_home: np.ndarray, home_win_indicator: np.ndarray,
                          n_splits: int = 3) -> bool:
    """Out-of-fold check: does Platt scaling actually reduce log-loss on
    this validation slice, or is it just fitting noise? Small per-league
    calibration sets (a few hundred matches) are exactly the regime where
    a naively-applied calibrator can make things worse — this guard is
    what stops that from silently shipping to production."""
    from backtest import log_loss_binary
    n = len(p_home)
    if n < 60:
        return False
    kf = KFold(n_splits=n_splits, shuffle=True, random_state=42)
    raw_ll, cal_ll = [], []
    for train_idx, val_idx in kf.split(p_home):
        raw_ll.append(log_loss_binary(p_home[val_idx], home_win_indicator[val_idx]))
        lr = LogisticRegression()
        lr.fit(_logit(p_home[train_idx]).reshape(-1, 1), home_win_indicator[train_idx])
        p_cal = lr.predict_proba(_logit(p_home[val_idx]).reshape(-1, 1))[:, 1]
        cal_ll.append(log_loss_binary(p_cal, home_win_indicator[val_idx]))
    # require a nontrivial margin, not just "won the coin flip" against CV
    # noise on what's often only a few hundred matches per league
    return float(np.mean(cal_ll)) < float(np.mean(raw_ll)) * 0.995
