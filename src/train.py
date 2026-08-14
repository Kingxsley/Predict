"""
Train + backtest all models, save production artifacts to models/.

Soccer: per-league Dixon-Coles + Elo, blended and isotonic-calibrated,
backtested against real historical closing odds (Brier, log-loss,
calibration, simulated value-betting ROI vs. the market).

Basketball (NBA): Elo + gradient-boosted margin/total model, backtested
against actual results (no static public odds archive available offline;
see README for plugging in a live odds feed).

Usage:
    python3 src/train.py                          # all default leagues + NBA
    python3 src/train.py --leagues E0 SP1 I1       # just these soccer leagues
    python3 src/train.py --skip-basketball
    python3 src/train.py --skip-soccer
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config
from dixon_coles import DixonColesModel
from elo import EloEngine
from ensemble import best_blend_weight, blend_probs, HomeWinCalibrator, cv_calibration_helps
from backtest import (
    brier_multiclass, log_loss_multiclass, brier_binary, log_loss_binary,
    calibration_table, simulate_value_betting,
)
from basketball_model import build_features, NBAModel, build_live_snapshot, FEATURE_COLS

OUTCOME_MAP = {"H": 0, "D": 1, "A": 2}
SOCCER_TRAIN_LOOKBACK_YEARS = 6.5
SOCCER_TEST_MONTHS = 15  # held out at the end for backtest


def train_one_league(div: str, df: pd.DataFrame) -> dict | None:
    league_df = df[df["div"] == div].sort_values("date").reset_index(drop=True)
    if len(league_df) < 400:
        return None
    max_date = league_df["date"].max()
    lookback_start = max_date - pd.Timedelta(days=int(365.25 * SOCCER_TRAIN_LOOKBACK_YEARS))
    league_df = league_df[league_df["date"] >= lookback_start].reset_index(drop=True)

    test_start = max_date - pd.DateOffset(months=SOCCER_TEST_MONTHS)
    train_df = league_df[league_df["date"] < test_start]
    test_df = league_df[league_df["date"] >= test_start].reset_index(drop=True)
    if len(train_df) < 250 or len(test_df) < 40:
        return None

    # --- fit backtest-period model (train-only, for honest OOS eval) ---
    dc_bt = DixonColesModel(league=div, half_life_days=config.SOCCER_HALF_LIFE_DAYS)
    dc_bt.fit(train_df, as_of=test_start)

    elo_bt = EloEngine(k=config.SOCCER_ELO_K, home_adv=config.SOCCER_ELO_HOME_ADV)
    for row in train_df.itertuples(index=False):
        elo_bt.update_soccer(row.HomeTeam, row.AwayTeam, row.result, season=row.date.year)

    # walk test period sequentially: predict THEN update elo (live-like)
    dc_probs, elo_probs, outcomes, mkt_probs, odds_arr = [], [], [], [], []
    for row in test_df.itertuples(index=False):
        dc_probs.append(dc_bt.predict_1x2(row.HomeTeam, row.AwayTeam))
        elo_probs.append(elo_bt.win_draw_probs(row.HomeTeam, row.AwayTeam))
        outcomes.append(OUTCOME_MAP[row.result])
        mkt_probs.append((row.mkt_prob_home, row.mkt_prob_draw, row.mkt_prob_away))
        odds_arr.append((row.odd_home, row.odd_draw, row.odd_away))
        elo_bt.update_soccer(row.HomeTeam, row.AwayTeam, row.result, season=row.date.year)

    dc_probs = np.array(dc_probs)
    elo_probs = np.array(elo_probs)
    outcomes = np.array(outcomes)
    mkt_probs = np.array(mkt_probs)
    odds_arr = np.array(odds_arr, dtype=float)

    # split test period in half: first half picks blend weight + calibrates,
    # second half is the truly held-out report (avoids double-dipping)
    half = len(test_df) // 2
    if half < 20:
        return None

    alpha, _ = best_blend_weight(dc_probs[:half], elo_probs[:half], outcomes[:half])
    blend_calib = blend_probs(dc_probs[:half], elo_probs[:half], alpha)
    home_ind_calib = (outcomes[:half] == 0).astype(float)
    use_calibration = cv_calibration_helps(blend_calib[:, 0], home_ind_calib)
    calibrator = HomeWinCalibrator()
    if use_calibration:
        calibrator.fit(blend_calib[:, 0], home_ind_calib)
    # else: calibrator stays unfitted -> transform() is a no-op passthrough

    # ---- final report on second half (untouched by blend/calibration fit) ----
    dc_h, elo_h = dc_probs[half:], elo_probs[half:]
    out_h, mkt_h, odds_h = outcomes[half:], mkt_probs[half:], odds_arr[half:]
    valid = np.all(np.isfinite(mkt_h), axis=1) & np.all(np.isfinite(odds_h), axis=1)

    blended_h = blend_probs(dc_h, elo_h, alpha)
    calibrated_h = calibrator.transform(blended_h)

    report = {
        "division": div,
        "league": config.SOCCER_LEAGUES.get(div, div),
        "n_train": int(len(train_df)),
        "n_test_holdout": int(len(out_h)),
        "blend_alpha": alpha,
        "calibration_applied": use_calibration,
        "dixon_coles_home_adv": dc_bt.home_adv_,
        "dixon_coles_rho": dc_bt.rho_,
        "brier": {
            "dixon_coles_only": brier_multiclass(dc_h, out_h),
            "elo_only": brier_multiclass(elo_h, out_h),
            "blended": brier_multiclass(blended_h, out_h),
            "blended_calibrated": brier_multiclass(calibrated_h, out_h),
            "market": brier_multiclass(mkt_h[valid], out_h[valid]) if valid.any() else None,
        },
        "log_loss": {
            "dixon_coles_only": log_loss_multiclass(dc_h, out_h),
            "elo_only": log_loss_multiclass(elo_h, out_h),
            "blended": log_loss_multiclass(blended_h, out_h),
            "blended_calibrated": log_loss_multiclass(calibrated_h, out_h),
            "market": log_loss_multiclass(mkt_h[valid], out_h[valid]) if valid.any() else None,
        },
    }

    if valid.any():
        # simulate value betting per outcome column (home/draw/away) using
        # calibrated model prob vs. market fair prob, paid at actual odds
        sims = {}
        for i, name in enumerate(["home", "draw", "away"]):
            sims[name] = simulate_value_betting(
                calibrated_h[valid, i], mkt_h[valid, i], odds_h[valid, i],
                (out_h[valid] == i).astype(int),
                edge_threshold=0.04,
            )
        report["value_betting_sim"] = sims

    # --- refit production model on ALL available data (through max_date) ---
    dc_prod = DixonColesModel(league=div, half_life_days=config.SOCCER_HALF_LIFE_DAYS)
    dc_prod.fit(league_df, as_of=max_date)
    elo_prod = EloEngine(k=config.SOCCER_ELO_K, home_adv=config.SOCCER_ELO_HOME_ADV)
    for row in league_df.itertuples(index=False):
        elo_prod.update_soccer(row.HomeTeam, row.AwayTeam, row.result, season=row.date.year)

    artifact = {
        "division": div,
        "league": config.SOCCER_LEAGUES.get(div, div),
        "dc_model": dc_prod,
        "elo_ratings": dict(elo_prod.ratings),
        "elo_config": {"k": config.SOCCER_ELO_K, "home_adv": config.SOCCER_ELO_HOME_ADV},
        "blend_alpha": alpha,
        "calibrator": calibrator,
        "trained_as_of": str(max_date.date()),
    }
    out_path = config.MODELS_DIR / "soccer"
    out_path.mkdir(exist_ok=True)
    with open(out_path / f"{div}.pkl", "wb") as f:
        pickle.dump(artifact, f)

    return report


def train_soccer(leagues: list[str]):
    print(f"Loading soccer data from {config.SOCCER_PROCESSED} ...")
    df = pd.read_parquet(config.SOCCER_PROCESSED)
    reports = []
    for div in leagues:
        t0 = time.time()
        rep = train_one_league(div, df)
        dt = time.time() - t0
        if rep is None:
            print(f"  [{div}] skipped (insufficient data)")
            continue
        print(f"  [{div}] {rep['league']}: fit in {dt:.1f}s | "
              f"Brier blend+cal={rep['brier']['blended_calibrated']:.4f} "
              f"vs market={rep['brier']['market']}")
        reports.append(rep)
    with open(config.REPORTS_DIR / "soccer_backtest.json", "w") as f:
        json.dump(reports, f, indent=2, default=float)
    print(f"Saved soccer backtest report -> {config.REPORTS_DIR / 'soccer_backtest.json'}")
    return reports


def train_basketball():
    print(f"Loading NBA data from {config.BASKETBALL_PROCESSED} ...")
    df = pd.read_parquet(config.BASKETBALL_PROCESSED)
    feats, _ = build_features(df)
    feats_valid = feats.dropna(subset=FEATURE_COLS + ["margin", "total"]).reset_index(drop=True)

    max_date = feats_valid["date"].max()
    test_start = max_date - pd.DateOffset(months=8)
    train_df = feats_valid[feats_valid["date"] < test_start]
    test_df = feats_valid[feats_valid["date"] >= test_start].reset_index(drop=True)
    half = len(test_df) // 2

    model_bt = NBAModel().fit(train_df)
    X_test = test_df[FEATURE_COLS]
    pred_margin = model_bt.margin_model.predict(X_test)
    pred_total = model_bt.total_model.predict(X_test)
    from scipy.stats import norm
    p_home = 0.65 * (1 - norm.cdf(0, loc=pred_margin, scale=model_bt.residual_std_)) + \
             0.35 * (1 / (1 + 10 ** (-test_df["elo_diff"].values / 400.0)))

    calib_idx = slice(0, half)
    hold_idx = slice(half, len(test_df))
    win_calibrator = None
    y_calib = test_df["home_win"].values[calib_idx]
    if half > 20 and cv_calibration_helps(p_home[calib_idx], y_calib):
        from ensemble import _logit
        from sklearn.linear_model import LogisticRegression
        win_calibrator = LogisticRegression()
        win_calibrator.fit(_logit(p_home[calib_idx]).reshape(-1, 1), y_calib)
        p_home_cal_hold = win_calibrator.predict_proba(_logit(p_home[hold_idx]).reshape(-1, 1))[:, 1]
    else:
        p_home_cal_hold = p_home[hold_idx]

    y_hold = test_df["home_win"].values[hold_idx]
    report = {
        "n_train": int(len(train_df)),
        "n_test_holdout": int(len(y_hold)),
        "residual_std_margin": model_bt.residual_std_,
        "brier": {
            "elo_only": brier_binary(1 / (1 + 10 ** (-test_df["elo_diff"].values[hold_idx] / 400.0)), y_hold),
            "margin_model_blend": brier_binary(p_home[hold_idx], y_hold),
            "calibrated": brier_binary(p_home_cal_hold, y_hold),
        },
        "log_loss": {
            "elo_only": log_loss_binary(1 / (1 + 10 ** (-test_df["elo_diff"].values[hold_idx] / 400.0)), y_hold),
            "margin_model_blend": log_loss_binary(p_home[hold_idx], y_hold),
            "calibrated": log_loss_binary(p_home_cal_hold, y_hold),
        },
        "margin_mae": float(np.mean(np.abs(pred_margin[hold_idx] - test_df["margin"].values[hold_idx]))),
        "total_mae": float(np.mean(np.abs(pred_total[hold_idx] - test_df["total"].values[hold_idx]))),
    }
    print(f"NBA backtest: Brier calibrated={report['brier']['calibrated']:.4f} "
          f"(elo-only={report['brier']['elo_only']:.4f}) | "
          f"margin MAE={report['margin_mae']:.2f} pts | total MAE={report['total_mae']:.2f} pts")

    with open(config.REPORTS_DIR / "basketball_backtest.json", "w") as f:
        json.dump(report, f, indent=2, default=float)

    # --- refit production model on ALL data ---
    model_prod = NBAModel().fit(feats_valid)
    elo_prod = EloEngine(k=config.NBA_ELO_K, home_adv=config.NBA_ELO_HOME_ADV,
                          season_regression=config.SEASON_REGRESSION)
    for row in feats_valid.itertuples(index=False):
        elo_prod.update_no_draw(
            row.home_name, row.away_name, row.margin,
            mov_mult_a=config.NBA_ELO_MOV_MULT_A, mov_mult_b=config.NBA_ELO_MOV_MULT_B,
            season=row.season,
        )
    snapshot = build_live_snapshot(feats_valid)

    out_dir = config.MODELS_DIR / "basketball"
    out_dir.mkdir(exist_ok=True)
    artifact = {
        "model": model_prod,
        "elo_ratings": dict(elo_prod.ratings),
        "snapshot": snapshot,
        "win_calibrator": win_calibrator,
        "trained_as_of": str(max_date.date()),
    }
    with open(out_dir / "nba_model.pkl", "wb") as f:
        pickle.dump(artifact, f)
    print(f"Saved NBA production model -> {out_dir / 'nba_model.pkl'}")
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--leagues", nargs="*", default=config.DEFAULT_SOCCER_LEAGUES)
    ap.add_argument("--skip-soccer", action="store_true")
    ap.add_argument("--skip-basketball", action="store_true")
    args = ap.parse_args()

    if not args.skip_soccer:
        t0 = time.time()
        train_soccer(args.leagues)
        print(f"Soccer training total: {time.time()-t0:.1f}s")
    if not args.skip_basketball:
        t0 = time.time()
        train_basketball()
        print(f"Basketball training total: {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
