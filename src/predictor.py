"""
Shared prediction layer: loads trained artifacts from models/ and exposes
simple predict_soccer() / predict_basketball() functions used by both the
CLI and the local API/dashboard. Also has the edge/Kelly-stake utility a
sportsbook actually cares about: given our probability and the market's
price, is there value, and how much should be staked.
"""
from __future__ import annotations

import pickle
import sys
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config
from elo import EloEngine
from ensemble import blend_probs, _logit
from basketball_model import predict_future_game, FEATURE_COLS


@lru_cache(maxsize=64)
def load_soccer_artifact(div: str):
    path = config.MODELS_DIR / "soccer" / f"{div}.pkl"
    if not path.exists():
        raise FileNotFoundError(
            f"No trained model for division '{div}'. Available: "
            f"{[p.stem for p in (config.MODELS_DIR / 'soccer').glob('*.pkl')]}"
        )
    with open(path, "rb") as f:
        return pickle.load(f)


@lru_cache(maxsize=1)
def load_basketball_artifact():
    path = config.MODELS_DIR / "basketball" / "nba_model.pkl"
    if not path.exists():
        raise FileNotFoundError("No trained NBA model found. Run src/train.py first.")
    with open(path, "rb") as f:
        return pickle.load(f)


def decimal_odds_to_prob(odds: float) -> float:
    return 1.0 / odds


def edge_and_kelly(model_prob: float, decimal_odds: float, kelly_fraction: float = 0.25) -> dict:
    """Standard sportsbook value metrics: edge = our prob - market implied
    prob; Kelly stake fraction of bankroll (fractional Kelly, default 1/4
    Kelly — full Kelly is far too aggressive for model uncertainty this
    size and is not something any competent shop actually runs at 1.0x)."""
    implied = decimal_odds_to_prob(decimal_odds)
    edge = model_prob - implied
    b = decimal_odds - 1.0  # net odds
    kelly_full = (model_prob * (b + 1) - 1) / b if b > 0 else 0.0
    kelly_full = max(kelly_full, 0.0)
    return {
        "market_implied_prob": implied,
        "edge": edge,
        "edge_pct": edge * 100,
        "kelly_stake_fraction": kelly_full * kelly_fraction,
    }


def predict_soccer(div: str, home: str, away: str,
                    odds_home: float = None, odds_draw: float = None, odds_away: float = None) -> dict:
    art = load_soccer_artifact(div)
    dc = art["dc_model"]
    elo = EloEngine(**art["elo_config"])
    elo.ratings = dict(art["elo_ratings"])

    dc_probs = np.array([dc.predict_1x2(home, away)])
    elo_probs = np.array([elo.win_draw_probs(home, away)])
    blended = blend_probs(dc_probs, elo_probs, art["blend_alpha"])[0]

    calibrator = art["calibrator"]
    if calibrator.fitted:
        final = calibrator.transform(blended.reshape(1, -1))[0]
    else:
        final = blended

    lam, mu = dc.expected_goals(home, away)
    over, under = dc.predict_over_under(home, away, 2.5)
    btts_yes, btts_no = dc.predict_btts(home, away)

    result = {
        "division": div,
        "league": art["league"],
        "home_team": home,
        "away_team": away,
        "model_trained_as_of": art["trained_as_of"],
        "prob_home_win": float(final[0]),
        "prob_draw": float(final[1]),
        "prob_away_win": float(final[2]),
        "expected_goals_home": float(lam),
        "expected_goals_away": float(mu),
        "prob_over_2_5": float(over),
        "prob_under_2_5": float(under),
        "prob_btts_yes": float(btts_yes),
        "prob_btts_no": float(btts_no),
        "elo_home": elo.get(home),
        "elo_away": elo.get(away),
    }

    odds = {"H": odds_home, "D": odds_draw, "A": odds_away}
    probs_map = {"H": final[0], "D": final[1], "A": final[2]}
    edges = {}
    for k, o in odds.items():
        if o:
            edges[k] = edge_and_kelly(probs_map[k], o)
    if edges:
        result["market_analysis"] = edges
    return result


def predict_basketball(home: str, away: str, game_date: str = None,
                        is_playoffs: bool = False, neutral_site: bool = False,
                        odds_home: float = None, odds_away: float = None) -> dict:
    art = load_basketball_artifact()
    model = art["model"]
    elo = EloEngine(k=config.NBA_ELO_K, home_adv=config.NBA_ELO_HOME_ADV,
                     season_regression=config.SEASON_REGRESSION)
    elo.ratings = dict(art["elo_ratings"])
    snapshot = art["snapshot"]

    if game_date is None:
        game_date = pd.Timestamp.now().normalize()
    else:
        game_date = pd.Timestamp(game_date)

    pred = predict_future_game(
        model, elo, snapshot, home, away, game_date,
        is_playoffs=float(is_playoffs), neutral_site=float(neutral_site),
    )

    p_home = pred["p_home_win"]
    win_cal = art.get("win_calibrator")
    if win_cal is not None:
        p_home = float(win_cal.predict_proba(_logit(np.array([p_home])).reshape(-1, 1))[0, 1])

    result = {
        "home_team": home,
        "away_team": away,
        "game_date": str(game_date.date()),
        "model_trained_as_of": art["trained_as_of"],
        "prob_home_win": p_home,
        "prob_away_win": 1 - p_home,
        "predicted_margin_home": pred["pred_margin"],
        "predicted_total_points": pred["pred_total"],
        "elo_home": elo.get(home),
        "elo_away": elo.get(away),
    }

    odds = {"H": odds_home, "A": odds_away}
    probs_map = {"H": p_home, "A": 1 - p_home}
    edges = {}
    for k, o in odds.items():
        if o:
            edges[k] = edge_and_kelly(probs_map[k], o)
    if edges:
        result["market_analysis"] = edges
    return result


def list_soccer_leagues():
    return sorted(p.stem for p in (config.MODELS_DIR / "soccer").glob("*.pkl"))


def list_soccer_teams(div: str):
    art = load_soccer_artifact(div)
    return sorted(art["dc_model"].teams_)


def list_basketball_teams():
    art = load_basketball_artifact()
    return sorted(art["snapshot"].keys())
