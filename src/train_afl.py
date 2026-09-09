"""
Trains the AFL model and backtests it on held-out seasons.

Run:
    python3 src/ingest_afl.py     # first, to build the training frame
    python3 src/train_afl.py

Writes models/afl/afl_model.pkl and reports/afl_backtest.json.

The holdout is the most recent N *seasons*, not a random sample. A random
split would let the model see later matches while predicting earlier ones,
which for a rating-based model is straightforward leakage and would report an
accuracy the live product could never reproduce.
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm
from sklearn.metrics import brier_score_loss, log_loss

sys.path.insert(0, str(Path(__file__).resolve().parent))
import afl_model
import config
from elo import expected_score


def _elo_only_probs(feats: pd.DataFrame) -> np.ndarray:
    return np.array([expected_score(d, 0.0) for d in feats["elo_diff"]])


def backtest(feats: pd.DataFrame, holdout_seasons: int) -> tuple[dict, pd.DataFrame]:
    seasons = sorted(feats["season"].unique())
    if len(seasons) <= holdout_seasons:
        raise SystemExit(f"Need more than {holdout_seasons} seasons to back-test; have {len(seasons)}.")
    test_seasons = seasons[-holdout_seasons:]

    train = feats[~feats["season"].isin(test_seasons)]
    test = feats[feats["season"].isin(test_seasons)].copy()

    model = afl_model.AFLModel().fit(train)

    X_test = test[afl_model.FEATURE_COLS].astype(float)
    pred_margin = model.margin_model.predict(X_test)
    pred_total = model.total_model.predict(X_test)
    s = model.margin_residual_std_

    p_home_margin = 1 - norm.cdf(0.5, loc=pred_margin, scale=s)
    p_home_elo = _elo_only_probs(test)
    p_home_blend = 0.65 * p_home_margin + 0.35 * p_home_elo

    # Draws are excluded from the win-probability scoring rather than scored
    # as a loss for both sides, which would penalise a well-calibrated model
    # for an outcome the two-way market refunds anyway.
    decided = test["margin"] != 0
    y = (test.loc[decided, "margin"] > 0).astype(int).to_numpy()

    def score(p):
        p = np.clip(p[decided.to_numpy()], 1e-6, 1 - 1e-6)
        return {"brier": float(brier_score_loss(y, p)), "log_loss": float(log_loss(y, p))}

    line = config.AFL_TOTAL_LINE
    p_over = 1 - norm.cdf(line, loc=pred_total, scale=model.total_residual_std_)
    y_over = (test["total_points"] > line).astype(int).to_numpy()

    report = {
        "n_train": int(len(train)),
        "n_test_holdout": int(len(test)),
        "train_seasons": [int(s) for s in seasons[:-holdout_seasons]],
        "test_seasons": [int(s) for s in test_seasons],
        "n_draws_in_holdout": int((~decided).sum()),
        "win_probability": {
            "elo_only": score(p_home_elo),
            "margin_model": score(p_home_margin),
            "blended": score(p_home_blend),
        },
        "margin_mae": float(np.mean(np.abs(pred_margin - test["margin"]))),
        "total_mae": float(np.mean(np.abs(pred_total - test["total_points"]))),
        "margin_residual_std": float(model.margin_residual_std_),
        "total_line": line,
        "total_over_under": {
            "brier": float(brier_score_loss(y_over, np.clip(p_over, 1e-6, 1 - 1e-6))),
            "accuracy": float(np.mean((p_over >= 0.5).astype(int) == y_over)),
            "actual_over_rate": float(y_over.mean()),
        },
        "baselines": {
            # What you would score by always backing the home side, and by
            # always predicting the historical average margin. A model that
            # cannot beat these is not adding anything.
            "always_home_accuracy": float(np.mean(y == 1)),
            "home_win_rate_holdout": float(np.mean(y == 1)),
            "mean_margin_mae": float(np.mean(np.abs(train["margin"].mean() - test["margin"]))),
            "mean_total_mae": float(np.mean(np.abs(train["total_points"].mean() - test["total_points"]))),
        },
        "model_accuracy": float(np.mean((p_home_blend >= 0.5).astype(int)[decided.to_numpy()] == y)),
    }
    return report, test


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdout-seasons", type=int, default=3)
    args = ap.parse_args()

    if not config.AFL_PROCESSED.exists():
        raise SystemExit("No AFL data. Run: python3 src/ingest_afl.py")

    games = pd.read_parquet(config.AFL_PROCESSED)
    print(f"Loaded {len(games):,} AFL matches ({games['season'].min()}-{games['season'].max()})")

    print("Building features (Elo walk + rolling form + venue experience)...")
    feats, _ = afl_model.build_features(games)

    print(f"Back-testing on the last {args.holdout_seasons} seasons...")
    report, _ = backtest(feats, args.holdout_seasons)

    print("Fitting the production model on all seasons...")
    full_feats, full_elo = afl_model.build_features(games)
    model = afl_model.AFLModel().fit(full_feats)
    model.elo = full_elo
    model.snapshot = afl_model.build_snapshot(full_feats)
    model.trained_as_of = str(games["date"].max().date())

    config.AFL_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(config.AFL_MODEL_PATH, "wb") as f:
        pickle.dump(model, f)

    report["trained_as_of"] = model.trained_as_of
    report["teams"] = int(games["home_name"].nunique())
    config.REPORTS_DIR.joinpath("afl_backtest.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")

    w = report["win_probability"]
    b = report["baselines"]
    print(f"\nSaved model to {config.AFL_MODEL_PATH}")
    print(f"Holdout: {report['n_test_holdout']} matches, seasons {report['test_seasons']}")
    print(f"  win prob Brier : elo {w['elo_only']['brier']:.4f} | "
          f"margin {w['margin_model']['brier']:.4f} | blended {w['blended']['brier']:.4f}")
    print(f"  accuracy       : {report['model_accuracy']:.3f}  "
          f"(always-home baseline {b['always_home_accuracy']:.3f})")
    print(f"  margin MAE     : {report['margin_mae']:.2f} pts "
          f"(mean-margin baseline {b['mean_margin_mae']:.2f})")
    print(f"  total MAE      : {report['total_mae']:.2f} pts "
          f"(mean-total baseline {b['mean_total_mae']:.2f})")
    print(f"  O/U {report['total_line']} acc : {report['total_over_under']['accuracy']:.3f}")


if __name__ == "__main__":
    main()
