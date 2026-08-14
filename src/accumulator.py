"""
Daily accumulator (parlay) builder: picks one leg from each of several
different upcoming fixtures - each leg being that fixture's single most
confident market call (1X2, BTTS, or over/under 2.5 for soccer; moneyline
for NBA) - and searches for the combination whose combined odds clears a
target minimum while keeping combined win probability as high as possible.

IMPORTANT — these are MODEL-IMPLIED odds (1 / predicted probability), not
real bookmaker prices. No live odds feed is integrated anywhere in this
app (see fixtures.py's module docstring) - a real sportsbook's actual
price for any of these markets will differ, usually favoring the book via
their overround/vig. Treat the "combined odds" number here as a read on
how confident the model's combined view is, not a quote you could actually
get paid out at. Never present this as a real betting line.
"""
from __future__ import annotations

import sys
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures as live_fixtures
import odds_provider as odds

MAX_CANDIDATE_LEGS = 120  # bounds the combination search (C(120,3) ~ 280k, fast)


def _soccer_leg(fixture: dict) -> dict | None:
    p = fixture.get("prediction") or {}
    if "error" in p:
        return None
    candidates = [
        ("1x2", "H", p["prob_home_win"], fixture["home_team_live_name"]),
        ("1x2", "D", p["prob_draw"], "Draw"),
        ("1x2", "A", p["prob_away_win"], fixture["away_team_live_name"]),
        ("btts", "YES", p["prob_btts_yes"], "BTTS: Yes"),
        ("btts", "NO", p["prob_btts_no"], "BTTS: No"),
        ("over_under_2_5", "OVER", p["prob_over_2_5"], "Over 2.5 Goals"),
        ("over_under_2_5", "UNDER", p["prob_under_2_5"], "Under 2.5 Goals"),
    ]
    market, pick, prob, label = max(candidates, key=lambda c: c[2])
    return {
        "sport": "soccer", "div": fixture.get("div"), "league": fixture.get("league_name"),
        "date": fixture["date"], "time": fixture.get("time"),
        "home": fixture["home_team_live_name"], "away": fixture["away_team_live_name"],
        "market": market, "pick": pick, "label": label, "probability": prob,
        "rationale": (p.get("rationale") or {}).get("narrative", []),
    }


def _basketball_leg(fixture: dict) -> dict | None:
    p = fixture.get("prediction") or {}
    if "error" in p:
        return None
    home, away = fixture["home_team_live_name"], fixture["away_team_live_name"]
    if p["prob_home_win"] >= p["prob_away_win"]:
        pick, prob, label = "HOME", p["prob_home_win"], f"{home} to win"
    else:
        pick, prob, label = "AWAY", p["prob_away_win"], f"{away} to win"
    return {
        "sport": "basketball", "league": "NBA", "date": fixture["date"], "time": fixture.get("time"),
        "home": home, "away": away,
        "market": "moneyline", "pick": pick, "label": label, "probability": prob,
        "rationale": (p.get("rationale") or {}).get("narrative", []),
    }


def _candidate_legs(date_from: str | None, date_to: str | None) -> list[dict]:
    data = live_fixtures.get_live_fixtures()
    legs = []
    for div, group in (data.get("soccer") or {}).items():
        for f in group.get("fixtures", []):
            if date_from and (f.get("date") or "") < date_from:
                continue
            if date_to and (f.get("date") or "") > date_to:
                continue
            f = {**f, "div": div, "league_name": group.get("league")}
            leg = _soccer_leg(f)
            if leg:
                legs.append(leg)
    for f in (data.get("basketball") or {}).get("fixtures", []):
        if date_from and (f.get("date") or "") < date_from:
            continue
        if date_to and (f.get("date") or "") > date_to:
            continue
        leg = _basketball_leg(f)
        if leg:
            legs.append(leg)
    return legs


def build_accumulator(num_legs: int = 3, min_combined_odds: float = 5.0,
                       date_from: str | None = None, date_to: str | None = None) -> dict:
    legs = _candidate_legs(date_from, date_to)

    if len(legs) < num_legs:
        return {
            "ok": False,
            "reason": f"Only {len(legs)} fixture(s) with a usable prediction in this date range - "
                      f"need at least {num_legs} to build a {num_legs}-leg accumulator. Try widening the date range.",
            "candidates_considered": len(legs),
        }

    # Keep the search bounded: the most useful candidates are the model's
    # most confident picks (safest legs) and its least confident picks
    # (highest individual odds, needed to actually clear a high combined
    # target) - so if there are more candidates than the cap, keep both
    # tails rather than an arbitrary/truncated middle slice.
    if len(legs) > MAX_CANDIDATE_LEGS:
        legs_sorted = sorted(legs, key=lambda l: l["probability"], reverse=True)
        half = MAX_CANDIDATE_LEGS // 2
        legs = legs_sorted[:half] + legs_sorted[-half:]

    target_prob = 1.0 / min_combined_odds
    best_combo, best_prob = None, -1.0

    for combo in combinations(legs, num_legs):
        combined_prob = 1.0
        for leg in combo:
            combined_prob *= leg["probability"]
        if combined_prob <= target_prob and combined_prob > best_prob:
            best_combo, best_prob = combo, combined_prob

    if best_combo is None:
        return {
            "ok": False,
            "reason": f"No combination of {num_legs} fixtures in this date range reaches combined odds of "
                      f"{min_combined_odds:.2f}x - the model's picks here are too confident individually. "
                      f"Try a lower target odds, a wider date range, or more legs.",
            "candidates_considered": len(legs),
        }

    final_legs = [dict(leg) for leg in best_combo]
    _attach_real_odds(final_legs)
    real_prices = [leg["real_odds"] for leg in final_legs if leg.get("real_odds") is not None]
    combined_real_odds = None
    if len(real_prices) == len(final_legs):
        combined_real_odds = 1.0
        for p in real_prices:
            combined_real_odds *= p

    return {
        "ok": True,
        "legs": final_legs,
        "combined_probability": best_prob,
        "combined_odds": 1.0 / best_prob,
        "combined_real_odds": combined_real_odds,
        "candidates_considered": len(legs),
    }


_MARKET_ODDS_FIELD = {
    ("1x2", "H"): "odds_home", ("1x2", "D"): "odds_draw", ("1x2", "A"): "odds_away",
    ("over_under_2_5", "OVER"): "odds_over_2_5", ("over_under_2_5", "UNDER"): "odds_under_2_5",
}


def _attach_real_odds(legs: list[dict]) -> None:
    """Best-effort: looks up real bookmaker odds for each leg's specific
    market/pick (only for the legs actually chosen, not every candidate -
    keeps this to at most 3 odds-provider calls per accumulator build).
    BTTS has no real-odds source at all (see odds_provider.py), so those
    legs simply stay without a real price. Never raises - a lookup failure
    just leaves that leg's real_odds as None."""
    for leg in legs:
        leg["real_odds"] = None
        try:
            if leg["sport"] == "soccer":
                field = _MARKET_ODDS_FIELD.get((leg["market"], leg["pick"]))
                if not field:
                    continue  # btts - no real-odds market available
                result = odds.get_soccer_odds(leg["div"], leg["home"], leg["away"])
                if result:
                    leg["real_odds"] = result.get(field)
            elif leg["sport"] == "basketball":
                result = odds.get_basketball_odds(leg["home"], leg["away"])
                if result:
                    field = "odds_home" if leg["pick"] == "HOME" else "odds_away"
                    leg["real_odds"] = result.get(field)
        except Exception:
            leg["real_odds"] = None
