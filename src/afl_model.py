"""
AFL match model.

Structure mirrors the soccer model so the inference layer
stays uniform:

  1. An Elo rating walked chronologically, with margin-of-victory scaling and
     season-boundary regression.
  2. Rolling form features (scoring for/against, margin, season win rate).
  3. Gradient-boosted regressors for home margin and game total.
  4. Win probabilities derived from the margin prediction and its residual
     spread, blended with the Elo probability as a stabiliser.

Two things are specific to Australian rules and worth stating:

**Venue experience instead of hardcoded geography.** The dominant
non-strength effect in the AFL is interstate travel: a Perth side visiting
the MCG is at a much larger disadvantage than a cross-town Melbourne trip,
and several Melbourne clubs share grounds so their nominal "home" game
confers little. Rather than hardcode a team-to-state and venue-to-state map
(brittle, and wrong for the relocated and Tasmanian home games that dot the
fixture), the model counts how many of its previous three years of games each
side has played at *this* venue. A true home ground scores high, a genuine
interstate trip scores near zero, and a shared or relocated ground lands in
between — learned from the fixture rather than asserted.

**Draws are modelled explicitly.** AFL draws are rare but real (24 in the
3,092 matches here, 0.78%). Because margins are integers, the probability of
a draw is the mass the margin distribution puts on zero, so it falls out of
the same normal the win probability comes from rather than being ignored or
bolted on.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm
from xgboost import XGBRegressor

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config
from elo import EloEngine, expected_score

FEATURE_COLS = [
    "elo_diff",
    "home_roll_margin5", "away_roll_margin5",
    "home_roll_margin10", "away_roll_margin10",
    "home_off5", "home_def5", "away_off5", "away_def5",
    "home_winpct_season", "away_winpct_season",
    "home_rest_days", "away_rest_days",
    "home_venue_exp", "away_venue_exp", "venue_exp_diff",
    "is_final", "neutral",
]

VENUE_EXP_WINDOW_DAYS = 365 * 3


def _rolling(df: pd.DataFrame, key: str, col: str, window: int) -> pd.Series:
    """Rolling mean over a team's PRIOR games only (shifted), so the current
    match's own result never leaks into its own features."""
    return (
        df.groupby(key)[col]
        .apply(lambda s: s.shift(1).rolling(window, min_periods=1).mean())
        .reset_index(level=0, drop=True)
    )


def _venue_experience(games: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """For each match, how many games each side played at this venue in the
    preceding VENUE_EXP_WINDOW_DAYS. Counts prior games only."""
    # (team, venue) -> sorted list of past appearance timestamps
    history: dict[tuple[str, str], list[pd.Timestamp]] = {}
    home_exp, away_exp = [], []

    for row in games.itertuples(index=False):
        cutoff = row.date - pd.Timedelta(days=VENUE_EXP_WINDOW_DAYS)
        for team, bucket in ((row.home_name, home_exp), (row.away_name, away_exp)):
            past = history.get((team, row.venue), [])
            bucket.append(sum(1 for t in past if t >= cutoff))
        for team in (row.home_name, row.away_name):
            history.setdefault((team, row.venue), []).append(row.date)

    return pd.Series(home_exp, index=games.index), pd.Series(away_exp, index=games.index)


def build_features(games: pd.DataFrame) -> tuple[pd.DataFrame, EloEngine]:
    games = games.sort_values("date").reset_index(drop=True)
    elo = EloEngine(
        k=config.AFL_ELO_K,
        home_adv=config.AFL_ELO_HOME_ADV,
        season_regression=config.SEASON_REGRESSION,
    )

    # Long form (one row per team per match) for team-level rolling stats.
    home_long = games[["date", "season", "home_name", "home_score", "away_score"]].rename(
        columns={"home_name": "team", "home_score": "pts_for", "away_score": "pts_against"})
    away_long = games[["date", "season", "away_name", "away_score", "home_score"]].rename(
        columns={"away_name": "team", "away_score": "pts_for", "home_score": "pts_against"})
    home_long["is_home"], away_long["is_home"] = 1, 0
    long = pd.concat([home_long, away_long]).sort_values("date").reset_index(drop=True)
    long["margin_for"] = long["pts_for"] - long["pts_against"]
    long["win"] = (long["margin_for"] > 0).astype(int)

    long["roll_margin5"] = _rolling(long, "team", "margin_for", 5)
    long["roll_margin10"] = _rolling(long, "team", "margin_for", 10)
    long["off5"] = _rolling(long, "team", "pts_for", 5)
    long["def5"] = _rolling(long, "team", "pts_against", 5)
    long["winpct_season"] = (
        long.groupby(["team", "season"])["win"]
        .apply(lambda s: s.shift(1).expanding().mean())
        .reset_index(level=[0, 1], drop=True)
    )
    # Days since that team's previous match, regardless of venue.
    long["rest_days"] = (
        long.groupby("team")["date"].diff().dt.days.fillna(7).clip(0, 30)
    )

    stat_cols = ["roll_margin5", "roll_margin10", "off5", "def5", "winpct_season", "rest_days"]
    home_side = long[long["is_home"] == 1][["date", "team"] + stat_cols].rename(
        columns={"team": "home_name", **{c: f"home_{c}" for c in stat_cols}})
    away_side = long[long["is_home"] == 0][["date", "team"] + stat_cols].rename(
        columns={"team": "away_name", **{c: f"away_{c}" for c in stat_cols}})

    games = games.merge(home_side, on=["date", "home_name"], how="left")
    games = games.merge(away_side, on=["date", "away_name"], how="left")
    games = games.drop_duplicates(subset=["game_id"]).reset_index(drop=True)

    games["home_venue_exp"], games["away_venue_exp"] = _venue_experience(games)
    games["venue_exp_diff"] = games["home_venue_exp"] - games["away_venue_exp"]

    # Elo walks the fixture in order; it is sequential by construction and so
    # cannot leak a result into its own match.
    home_elos, away_elos = [], []
    for row in games.itertuples(index=False):
        home_elos.append(elo.get(row.home_name))
        away_elos.append(elo.get(row.away_name))
        elo.update_no_draw(
            row.home_name, row.away_name, row.margin,
            mov_mult_a=config.AFL_ELO_MOV_MULT_A,
            mov_mult_b=config.AFL_ELO_MOV_MULT_B,
            season=row.season,
        )
    games["home_elo"] = home_elos
    games["away_elo"] = away_elos
    # A grand final is neutral, so home advantage is withheld there.
    games["neutral"] = games["neutral"].astype(float)
    games["is_final"] = games["is_final"].astype(float)
    games["elo_diff"] = (
        games["home_elo"] + config.AFL_ELO_HOME_ADV * (1 - games["neutral"]) - games["away_elo"]
    )

    for c in FEATURE_COLS:
        if c not in games.columns:
            games[c] = np.nan
    return games, elo


@dataclass
class AFLModel:
    margin_model: XGBRegressor = None
    total_model: XGBRegressor = None
    margin_residual_std_: float = 40.0
    total_residual_std_: float = 30.0
    elo: EloEngine = None
    trained_as_of: str = ""
    total_line: float = config.AFL_TOTAL_LINE
    snapshot: dict = field(default_factory=dict)

    def fit(self, feats: pd.DataFrame) -> "AFLModel":
        df = feats.dropna(subset=["margin", "total_points"]).copy()
        X = df[FEATURE_COLS].astype(float)

        common = dict(n_estimators=400, max_depth=3, learning_rate=0.03,
                      subsample=0.8, colsample_bytree=0.8, reg_lambda=2.0,
                      objective="reg:squarederror", n_jobs=4, random_state=7)
        self.margin_model = XGBRegressor(**common).fit(X, df["margin"])
        self.total_model = XGBRegressor(**common).fit(X, df["total_points"])

        self.margin_residual_std_ = float(np.std(df["margin"] - self.margin_model.predict(X)))
        self.total_residual_std_ = float(np.std(df["total_points"] - self.total_model.predict(X)))
        return self

    def predict_row(self, row: pd.DataFrame) -> dict:
        X = row[FEATURE_COLS].astype(float)
        margin = float(self.margin_model.predict(X)[0])
        total = float(self.total_model.predict(X)[0])
        s = self.margin_residual_std_

        # Margins are integers, so a draw is the mass on exactly zero: the
        # interval (-0.5, 0.5). Modelling it this way reproduces the observed
        # ~0.8% draw rate without any separate calibration.
        p_home_margin = float(1 - norm.cdf(0.5, loc=margin, scale=s))
        p_away_margin = float(norm.cdf(-0.5, loc=margin, scale=s))

        elo_diff = float(row["elo_diff"].iloc[0])
        p_home_elo = float(expected_score(elo_diff, 0.0))

        # The margin model is sharper once form features have warmed up; Elo
        # is the stabiliser for early-season and small-sample situations.
        blend = 0.65
        p_home = blend * p_home_margin + (1 - blend) * p_home_elo
        p_away = blend * p_away_margin + (1 - blend) * (1 - p_home_elo)
        total_mass = p_home + p_away
        p_draw = max(0.0, 1.0 - total_mass)
        if total_mass > 1.0:  # blending can overshoot; renormalise
            p_home, p_away, p_draw = p_home / total_mass, p_away / total_mass, 0.0

        line = self.total_line
        p_over = float(1 - norm.cdf(line, loc=total, scale=self.total_residual_std_))

        return {
            "predicted_margin_home": margin,
            "predicted_total_points": total,
            "prob_home_win": p_home,
            "prob_away_win": p_away,
            "prob_draw": p_draw,
            "prob_over_total": p_over,
            "prob_under_total": 1.0 - p_over,
            "total_line": line,
            "prob_home_win_margin_model": p_home_margin,
            "prob_home_win_elo": p_home_elo,
        }


def build_snapshot(feats: pd.DataFrame) -> dict:
    """Latest known form state per team plus venue history, so a fixture that
    has not been played (and so is not in the historical frame at all) can be
    turned into a feature row."""
    latest: dict[str, dict] = {}
    long_home = feats[["date", "home_name", "home_roll_margin5", "home_roll_margin10",
                       "home_off5", "home_def5", "home_winpct_season"]].rename(
        columns=lambda c: c.replace("home_", "").replace("name", "team"))
    long_away = feats[["date", "away_name", "away_roll_margin5", "away_roll_margin10",
                       "away_off5", "away_def5", "away_winpct_season"]].rename(
        columns=lambda c: c.replace("away_", "").replace("name", "team"))
    allrows = pd.concat([long_home, long_away]).sort_values("date")
    for team, grp in allrows.groupby("team"):
        row = grp.iloc[-1]
        latest[team] = {
            "roll_margin5": float(row["roll_margin5"]) if pd.notna(row["roll_margin5"]) else 0.0,
            "roll_margin10": float(row["roll_margin10"]) if pd.notna(row["roll_margin10"]) else 0.0,
            "off5": float(row["off5"]) if pd.notna(row["off5"]) else 85.0,
            "def5": float(row["def5"]) if pd.notna(row["def5"]) else 85.0,
            "winpct_season": float(row["winpct_season"]) if pd.notna(row["winpct_season"]) else 0.5,
            "last_played": row["date"].isoformat(),
        }

    # Venue appearances within the experience window, for the same feature at
    # inference time.
    cutoff = feats["date"].max() - pd.Timedelta(days=VENUE_EXP_WINDOW_DAYS)
    recent = feats[feats["date"] >= cutoff]
    venue_counts: dict[str, int] = {}
    for row in recent.itertuples(index=False):
        for team in (row.home_name, row.away_name):
            venue_counts[f"{team}|{row.venue}"] = venue_counts.get(f"{team}|{row.venue}", 0) + 1

    return {
        "teams": latest,
        "venue_counts": venue_counts,
        "last_date": feats["date"].max().isoformat(),
    }


def feature_row_for_fixture(model: "AFLModel", home: str, away: str, venue: str | None,
                            game_date: str | None = None, is_final: bool = False,
                            neutral: bool = False) -> pd.DataFrame:
    """Assembles a single feature row for an unplayed fixture from the
    model's stored snapshot. Unknown teams fall back to league-average form
    rather than erroring, matching how the soccer side handles them."""
    snap = model.snapshot or {}
    teams = snap.get("teams", {})
    venue_counts = snap.get("venue_counts", {})
    default = {"roll_margin5": 0.0, "roll_margin10": 0.0, "off5": 85.0,
               "def5": 85.0, "winpct_season": 0.5, "last_played": None}
    h = teams.get(home, default)
    a = teams.get(away, default)

    def rest(state) -> float:
        if not game_date or not state.get("last_played"):
            return 7.0
        try:
            delta = (pd.Timestamp(game_date) - pd.Timestamp(state["last_played"])).days
        except (ValueError, TypeError):
            return 7.0
        return float(min(max(delta, 0), 30))

    home_exp = float(venue_counts.get(f"{home}|{venue}", 0))
    away_exp = float(venue_counts.get(f"{away}|{venue}", 0))
    neutral_f = 1.0 if neutral else 0.0
    elo_diff = (model.elo.get(home)
                + config.AFL_ELO_HOME_ADV * (1 - neutral_f)
                - model.elo.get(away))

    return pd.DataFrame([{
        "elo_diff": elo_diff,
        "home_roll_margin5": h["roll_margin5"], "away_roll_margin5": a["roll_margin5"],
        "home_roll_margin10": h["roll_margin10"], "away_roll_margin10": a["roll_margin10"],
        "home_off5": h["off5"], "home_def5": h["def5"],
        "away_off5": a["off5"], "away_def5": a["def5"],
        "home_winpct_season": h["winpct_season"], "away_winpct_season": a["winpct_season"],
        "home_rest_days": rest(h), "away_rest_days": rest(a),
        "home_venue_exp": home_exp, "away_venue_exp": away_exp,
        "venue_exp_diff": home_exp - away_exp,
        "is_final": 1.0 if is_final else 0.0,
        "neutral": neutral_f,
    }])
