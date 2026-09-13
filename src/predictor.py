"""
Shared prediction layer: loads trained artifacts from models/ and exposes
predict_soccer() / predict_afl() for both the CLI and the API/dashboard.
Also has the edge/Kelly-stake utility a sportsbook actually cares about:
given our probability and the market's price, is there value, and how much
should be staked.
"""
from __future__ import annotations

import pickle
import sys
from functools import lru_cache
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import afl_model
import config
from elo import EloEngine
from ensemble import blend_probs
import rationale as rat


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
def load_afl_model():
    if not config.AFL_MODEL_PATH.exists():
        raise FileNotFoundError(
            "No trained AFL model found. Run: python3 src/ingest_afl.py && python3 src/train_afl.py"
        )
    with open(config.AFL_MODEL_PATH, "rb") as f:
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

    # Headline scoreline, constrained to the outcome the calibrated ensemble
    # actually picks. See DixonColes.most_likely_score for why it is not the
    # unconstrained mode and not rounded expected goals.
    pick = "HDA"[int(np.argmax(final))]
    score_h, score_a, score_p = dc.most_likely_score(home, away, outcome=pick)

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
        "predicted_score_home": score_h,
        "predicted_score_away": score_a,
        "prob_predicted_score": score_p,
        "prob_over_2_5": float(over),
        "prob_under_2_5": float(under),
        "prob_btts_yes": float(btts_yes),
        "prob_btts_no": float(btts_no),
        "elo_home": elo.get(home),
        "elo_away": elo.get(away),
    }

    try:
        result["rationale"] = rat.build_soccer_rationale(div, home, away, result)
    except Exception as e:
        result["rationale"] = {"narrative": [], "data_scope": rat.DATA_SCOPE_NOTE,
                                "error": f"{type(e).__name__}: {e}"}

    odds = {"H": odds_home, "D": odds_draw, "A": odds_away}
    probs_map = {"H": final[0], "D": final[1], "A": final[2]}
    edges = {}
    for k, o in odds.items():
        if o:
            edges[k] = edge_and_kelly(probs_map[k], o)
    if edges:
        result["market_analysis"] = edges
    return result


def predict_afl(home: str, away: str, venue: str = None, game_date: str = None,
                 is_final: bool = False, neutral: bool = False,
                 odds_home: float = None, odds_away: float = None) -> dict:
    model = load_afl_model()
    row = afl_model.feature_row_for_fixture(
        model, home, away, venue, game_date, is_final=is_final, neutral=neutral)
    pred = model.predict_row(row)

    result = {
        "sport": "afl",
        "league": "AFL",
        "home_team": home,
        "away_team": away,
        "venue": venue,
        "game_date": game_date,
        "model_trained_as_of": model.trained_as_of,
        "prob_home_win": pred["prob_home_win"],
        "prob_away_win": pred["prob_away_win"],
        "prob_draw": pred["prob_draw"],
        "predicted_margin_home": pred["predicted_margin_home"],
        "predicted_total_points": pred["predicted_total_points"],
        # Projected scoreboard, purely a restatement of the margin and total
        # the model already predicts (h = (total + margin) / 2). It is not an
        # independent estimate and carries no extra information; it exists so
        # the board can show a scoreline for AFL the way it does for soccer.
        "predicted_score_home": int(round(
            (pred["predicted_total_points"] + pred["predicted_margin_home"]) / 2)),
        "predicted_score_away": int(round(
            (pred["predicted_total_points"] - pred["predicted_margin_home"]) / 2)),
        "prob_over_total": pred["prob_over_total"],
        "prob_under_total": pred["prob_under_total"],
        "total_line": pred["total_line"],
        "elo_home": model.elo.get(home),
        "elo_away": model.elo.get(away),
        "home_venue_experience": float(row["home_venue_exp"].iloc[0]),
        "away_venue_experience": float(row["away_venue_exp"].iloc[0]),
    }

    try:
        result["rationale"] = rat.build_afl_rationale(home, away, result)
    except Exception as e:
        result["rationale"] = {"narrative": [], "data_scope": rat.DATA_SCOPE_NOTE_AFL,
                                "error": f"{type(e).__name__}: {e}"}

    edges = {}
    for key, odds in (("H", odds_home), ("A", odds_away)):
        if odds:
            prob = result["prob_home_win"] if key == "H" else result["prob_away_win"]
            edges[key] = edge_and_kelly(prob, odds)
    if edges:
        result["market_analysis"] = edges
    return result


def list_soccer_leagues():
    return sorted(p.stem for p in (config.MODELS_DIR / "soccer").glob("*.pkl"))


def list_soccer_teams(div: str):
    art = load_soccer_artifact(div)
    return sorted(art["dc_model"].teams_)


def list_afl_teams():
    model = load_afl_model()
    return sorted((model.snapshot or {}).get("teams", {}).keys())


def afl_venues():
    """Venues seen in the model's recent history, most-used first — used to
    populate the venue picker, since venue materially changes an AFL
    prediction through the travel/experience features."""
    counts: dict[str, int] = {}
    for key, n in (load_afl_model().snapshot or {}).get("venue_counts", {}).items():
        _, _, venue = key.partition("|")
        if venue and venue != "None":
            counts[venue] = counts.get(venue, 0) + n
    return [v for v, _ in sorted(counts.items(), key=lambda kv: -kv[1])]
