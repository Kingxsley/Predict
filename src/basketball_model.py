"""
NBA prediction model: blends an Elo power-rating engine with a gradient-
boosted margin/total-points regressor trained on rolling team-form
features. All features are strictly backward-looking (computed with the
pre-game history only) so the walk-forward backtest is leakage-free —
this is the single most common mistake in amateur sports models and the
main reason naive "ML on box scores" projects fail to beat simple power
ratings out of sample.

Pipeline:
  1. Walk the season chronologically, updating an EloEngine game by game
     (pre-game Elo is a feature; post-game update happens after).
  2. Compute rolling-window offensive/defensive scoring efficiency, win%,
     and rest-day features per team using only prior games.
  3. Fit XGBoost regressors for (home_margin, game_total) on those
     features.
  4. Convert the margin prediction to a win probability via a fitted
     Normal distribution of margin residuals (a standard "power rating +
     spread" approach), then blend with the pure-Elo win probability.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.stats import norm
from xgboost import XGBRegressor

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from elo import EloEngine
import config

FEATURE_COLS = [
    "elo_diff", "home_elo", "away_elo",
    "home_roll_margin10", "away_roll_margin10",
    "home_roll_margin20", "away_roll_margin20",
    "home_off10", "home_def10", "away_off10", "away_def10",
    "home_winpct_season", "away_winpct_season",
    "home_rest_days", "away_rest_days",
    "home_b2b", "away_b2b",
    "is_playoffs", "neutral_site",
]


def _rolling_team_feature(df: pd.DataFrame, team_col: str, value_col: str, window: int) -> pd.Series:
    """Rolling mean of value_col for each team, shifted so only PRIOR games
    are included (no leakage of the current game's own result)."""
    return (
        df.groupby(team_col)[value_col]
        .apply(lambda s: s.shift(1).rolling(window, min_periods=1).mean())
        .reset_index(level=0, drop=True)
    )


def build_features(games: pd.DataFrame, elo_cfg=None) -> pd.DataFrame:
    """games: chronologically sorted df with date, home_name, away_name,
    home_score, away_score, margin, total, home_win, season, is_playoffs,
    neutral_site, home_rest_days, away_rest_days.
    Returns games with feature columns appended, and the fitted EloEngine.
    """
    games = games.sort_values("date").reset_index(drop=True)
    elo = EloEngine(
        k=config.NBA_ELO_K, home_adv=config.NBA_ELO_HOME_ADV,
        season_regression=config.SEASON_REGRESSION,
    )

    # long form: one row per (game, team, home/away) to compute team-level
    # rolling scoring stats cleanly, then pivot back.
    home_long = games[["date", "home_name", "home_score", "away_score"]].rename(
        columns={"home_name": "team", "home_score": "pts_for", "away_score": "pts_against"})
    away_long = games[["date", "away_name", "away_score", "home_score"]].rename(
        columns={"away_name": "team", "away_score": "pts_for", "home_score": "pts_against"})
    home_long["is_home"] = 1
    away_long["is_home"] = 0
    long = pd.concat([home_long, away_long]).sort_values("date").reset_index(drop=True)
    long["margin_for"] = long["pts_for"] - long["pts_against"]
    long["win"] = (long["margin_for"] > 0).astype(int)

    long["roll_margin10"] = _rolling_team_feature(long, "team", "margin_for", 10)
    long["roll_margin20"] = _rolling_team_feature(long, "team", "margin_for", 20)
    long["off10"] = _rolling_team_feature(long, "team", "pts_for", 10)
    long["def10"] = _rolling_team_feature(long, "team", "pts_against", 10)

    # season-to-date win% (shifted, prior games only, resets per season via merge key)
    long = long.merge(games[["date", "season"]].drop_duplicates("date"), on="date", how="left")
    long["winpct_season"] = (
        long.groupby(["team", "season"])["win"]
        .apply(lambda s: s.shift(1).expanding().mean())
        .reset_index(level=[0, 1], drop=True)
    )

    feat_cols_map = {
        "roll_margin10": "roll_margin10", "roll_margin20": "roll_margin20",
        "off10": "off10", "def10": "def10", "winpct_season": "winpct_season",
    }
    home_feat_df = long[long["is_home"] == 1][["date", "team"] + list(feat_cols_map)].rename(
        columns={"team": "home_name", **{k: f"home_{k}" for k in feat_cols_map}})
    away_feat_df = long[long["is_home"] == 0][["date", "team"] + list(feat_cols_map)].rename(
        columns={"team": "away_name", **{k: f"away_{k}" for k in feat_cols_map}})

    games = games.merge(home_feat_df, on=["date", "home_name"], how="left")
    games = games.merge(away_feat_df, on=["date", "away_name"], how="left")
    games = games.rename(columns={
        "home_roll_margin10": "home_roll_margin10", "away_roll_margin10": "away_roll_margin10",
        "home_roll_margin20": "home_roll_margin20", "away_roll_margin20": "away_roll_margin20",
        "home_off10": "home_off10", "away_off10": "away_off10",
        "home_def10": "home_def10", "away_def10": "away_def10",
        "home_winpct_season": "home_winpct_season", "away_winpct_season": "away_winpct_season",
    })

    # Elo: walk chronologically (Elo is inherently sequential/leakage-safe)
    home_elo_list, away_elo_list = [], []
    for row in games.itertuples(index=False):
        h, a = row.home_name, row.away_name
        home_elo_list.append(elo.get(h))
        away_elo_list.append(elo.get(a))
        elo.update_no_draw(
            h, a, row.margin,
            mov_mult_a=config.NBA_ELO_MOV_MULT_A, mov_mult_b=config.NBA_ELO_MOV_MULT_B,
            season=row.season,
        )
    games["home_elo"] = home_elo_list
    games["away_elo"] = away_elo_list
    games["elo_diff"] = games["home_elo"] + config.NBA_ELO_HOME_ADV - games["away_elo"]

    games["home_b2b"] = (games["home_rest_days"] <= 1).astype(float)
    games["away_b2b"] = (games["away_rest_days"] <= 1).astype(float)
    games["neutral_site"] = games["neutral_site"].fillna(False).astype(float)
    games["is_playoffs"] = games["is_playoffs"].astype(float)

    for c in FEATURE_COLS:
        if c not in games.columns:
            games[c] = np.nan
    return games, elo


@dataclass
class NBAModel:
    margin_model: XGBRegressor = None
    total_model: XGBRegressor = None
    residual_std_: float = 12.0
    elo: EloEngine = None

    def fit(self, games_with_features: pd.DataFrame):
        df = games_with_features.dropna(subset=FEATURE_COLS + ["margin", "total"]).copy()
        X = df[FEATURE_COLS]
        y_margin = df["margin"]
        y_total = df["total"]

        self.margin_model = XGBRegressor(
            n_estimators=300, max_depth=3, learning_rate=0.03,
            subsample=0.8, colsample_bytree=0.8, reg_lambda=2.0,
            objective="reg:squarederror", n_jobs=4,
        )
        self.margin_model.fit(X, y_margin)

        self.total_model = XGBRegressor(
            n_estimators=300, max_depth=3, learning_rate=0.03,
            subsample=0.8, colsample_bytree=0.8, reg_lambda=2.0,
            objective="reg:squarederror", n_jobs=4,
        )
        self.total_model.fit(X, y_total)

        resid = y_margin - self.margin_model.predict(X)
        self.residual_std_ = float(np.std(resid))
        return self

    def predict(self, feat_row: pd.DataFrame):
        """feat_row: single-row DataFrame with FEATURE_COLS + elo_diff etc."""
        X = feat_row[FEATURE_COLS]
        pred_margin = float(self.margin_model.predict(X)[0])
        pred_total = float(self.total_model.predict(X)[0])
        p_home_margin_model = 1 - norm.cdf(0, loc=pred_margin, scale=self.residual_std_)

        elo_diff = float(feat_row["elo_diff"].iloc[0])
        p_home_elo = 1.0 / (1.0 + 10 ** (-elo_diff / 400.0))

        # blend: margin-model tends to be sharper once warmed up; weight
        # it higher, but keep Elo as a stabilizer for small-sample/early
        # season situations where rolling features are noisy.
        p_home = 0.65 * p_home_margin_model + 0.35 * p_home_elo
        return {
            "pred_margin": pred_margin,
            "pred_total": pred_total,
            "p_home_win": float(p_home),
            "p_home_win_margin_model": float(p_home_margin_model),
            "p_home_win_elo": float(p_home_elo),
        }


def build_live_snapshot(feats: pd.DataFrame) -> dict:
    """Latest known rolling-form state per team, so we can construct a
    feature row for a fixture that hasn't been played yet (i.e. isn't in
    the historical games table at all)."""
    long_cols = ["roll_margin10", "roll_margin20", "off10", "def10", "winpct_season"]
    snap = {}
    for side in ("home", "away"):
        cols = [f"{side}_{c}" for c in long_cols]
        sub = feats[["date", f"{side}_name"] + cols + ["season"]].rename(
            columns={f"{side}_name": "team", **{f"{side}_{c}": c for c in long_cols}})
        for team, g in sub.groupby("team"):
            g = g.sort_values("date")
            last = g.iloc[-1]
            state = snap.setdefault(team, {})
            state.update({c: last[c] for c in long_cols})
            state["last_date"] = last["date"]
            state["season"] = last["season"]
    return snap


def predict_future_game(model: "NBAModel", elo: EloEngine, snapshot: dict,
                         home: str, away: str, game_date, season=None,
                         is_playoffs: float = 0.0, neutral_site: float = 0.0) -> dict:
    """Build a feature row for an unplayed fixture from the current live
    snapshot (Elo ratings + latest rolling form) and run the model."""
    import pandas as pd  # local import to avoid polluting module namespace

    hs = snapshot.get(home, {})
    aws = snapshot.get(away, {})
    game_date = pd.Timestamp(game_date)

    def rest_days(state):
        ld = state.get("last_date")
        if ld is None:
            return 3.0  # neutral default for a team with no history
        return max((game_date - pd.Timestamp(ld)).days, 0)

    row = {
        "elo_diff": elo.get(home) + config.NBA_ELO_HOME_ADV - elo.get(away),
        "home_elo": elo.get(home),
        "away_elo": elo.get(away),
        "home_roll_margin10": hs.get("roll_margin10", 0.0),
        "away_roll_margin10": aws.get("roll_margin10", 0.0),
        "home_roll_margin20": hs.get("roll_margin20", 0.0),
        "away_roll_margin20": aws.get("roll_margin20", 0.0),
        "home_off10": hs.get("off10", 112.0),
        "home_def10": hs.get("def10", 112.0),
        "away_off10": aws.get("off10", 112.0),
        "away_def10": aws.get("def10", 112.0),
        "home_winpct_season": hs.get("winpct_season", 0.5),
        "away_winpct_season": aws.get("winpct_season", 0.5),
        "home_rest_days": rest_days(hs),
        "away_rest_days": rest_days(aws),
        "home_b2b": float(rest_days(hs) <= 1),
        "away_b2b": float(rest_days(aws) <= 1),
        "is_playoffs": is_playoffs,
        "neutral_site": neutral_site,
    }
    feat_row = pd.DataFrame([row])
    return model.predict(feat_row)
