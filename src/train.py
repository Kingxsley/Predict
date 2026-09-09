"""
Train + backtest all models, save production artifacts to models/.

Soccer: per-league Dixon-Coles + Elo, blended and isotonic-calibrated,
backtested against real historical closing odds (Brier, log-loss,
calibration, simulated value-betting ROI vs. the market).

The AFL model lives in its own script because its data source, feature set
and backtest split are all different: see src/train_afl.py.

Usage:
    python3 src/train.py                          # all default leagues
    python3 src/train.py --leagues E0 SP1 I1       # just these soccer leagues
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--leagues", nargs="*", default=config.DEFAULT_SOCCER_LEAGUES)
    ap.add_argument("--skip-soccer", action="store_true")
    args = ap.parse_args()

    if not args.skip_soccer:
        t0 = time.time()
        train_soccer(args.leagues)
        print(f"Soccer training total: {time.time()-t0:.1f}s")
    print("\nAFL is trained separately: python3 src/ingest_afl.py && python3 src/train_afl.py")


if __name__ == "__main__":
    main()
