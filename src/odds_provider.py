"""
Real bookmaker odds via the-odds-api.com. Requires a free registered key
(no payment) from https://the-odds-api.com, read from the THE_ODDS_API_KEY
env var - never hardcode a real key here, this file is committed to git.

Quota discipline: the free tier is ~500 requests/month, and each call
here costs 2 credits (h2h + totals markets, 1 region). One call returns
EVERY upcoming fixture for that league with odds already attached, so the
right usage pattern is "fetch a whole league once, cache it for hours,
reuse for every fixture in it" - never fetch per-fixture, and never fetch
every league on every page load. Cached in-process per league for
ODDS_CACHE_TTL_SECONDS; callers should only touch the specific league(s)
they actually need (e.g. the ones in a Custom Matchup query or an
accumulator's chosen legs), not all 20 up front.

BTTS odds are not available via this API's markets at all (confirmed by a
live 400 response) - only 1X2/moneyline (h2h) and over/under 2.5 (totals,
filtered to the 2.5 goal line specifically) have real prices here.
"""
from __future__ import annotations

import difflib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config

ODDS_API_KEY = os.environ.get("THE_ODDS_API_KEY", "")
BASE = "https://api.the-odds-api.com/v4"
REGION = "uk"
ODDS_CACHE_TTL_SECONDS = 6 * 60 * 60  # 6h - real prices move, but the free quota can't support fast refresh

# Our division code -> the-odds-api.com sport key. Verified live against
# the real /v4/sports list - covers all 20 trained soccer divisions plus
# the AFL, which is far broader fixture coverage than either TheSportsDB or
# football-data.org's free tiers. Note this is odds coverage only: a league
# can have a priced market here while having no free *fixture* feed, which
# is why the Matchup tab can fetch odds for divisions the board can't list.
DIV_TO_ODDS_SPORT_KEY = {
    "E0": "soccer_epl", "E1": "soccer_efl_champ", "SP1": "soccer_spain_la_liga",
    "SP2": "soccer_spain_segunda_division", "I1": "soccer_italy_serie_a", "I2": "soccer_italy_serie_b",
    "D1": "soccer_germany_bundesliga", "D2": "soccer_germany_bundesliga2", "F1": "soccer_france_ligue_one",
    "F2": "soccer_france_ligue_two", "N1": "soccer_netherlands_eredivisie", "P1": "soccer_portugal_primeira_liga",
    "B1": "soccer_belgium_first_div", "SC0": "soccer_spl", "T1": "soccer_turkey_super_league",
    "G1": "soccer_greece_super_league", "USA": "soccer_usa_mls", "BRA": "soccer_brazil_campeonato",
    "ARG": "soccer_argentina_primera_division", "MEX": "soccer_mexico_ligamx",
}
AFL_SPORT_KEY = "aussierules_afl"

_league_cache: dict[str, dict] = {}  # sport_key -> {"ts": float, "events": [...]}


def is_configured() -> bool:
    return bool(ODDS_API_KEY)


def _normalize(name: str) -> str:
    return "".join(c.lower() for c in (name or "") if c.isalnum())


def _get_json(url: str, timeout: int = 10):
    req = urllib.request.Request(url, headers={"User-Agent": "sports-predictor/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_league_odds(sport_key: str) -> list[dict]:
    now = time.time()
    cached = _league_cache.get(sport_key)
    if cached and (now - cached["ts"]) < ODDS_CACHE_TTL_SECONDS:
        return cached["events"]

    url = f"{BASE}/sports/{sport_key}/odds/?apiKey={ODDS_API_KEY}&regions={REGION}&markets=h2h,totals&oddsFormat=decimal"
    events = _get_json(url)
    _league_cache[sport_key] = {"ts": now, "events": events}
    return events


def _best_prices(event: dict, market_key: str, point: float | None = None) -> dict[str, float]:
    """Best (highest) available decimal price per outcome name, across
    every tracked bookmaker for this event/market. `point` filters totals
    to one specific goal/point line (bookmakers post several)."""
    best: dict[str, float] = {}
    for bm in event.get("bookmakers", []):
        for m in bm.get("markets", []):
            if m.get("key") != market_key:
                continue
            for outcome in m.get("outcomes", []):
                if point is not None and outcome.get("point") != point:
                    continue
                name, price = outcome.get("name"), outcome.get("price")
                if name is None or price is None:
                    continue
                if name not in best or price > best[name]:
                    best[name] = price
    return best


def get_soccer_odds(div: str, home: str, away: str) -> dict | None:
    """Real best-available 1X2 and over/under 2.5 prices for one fixture,
    matched by fuzzy team name within the division's cached odds board.
    Returns None if odds aren't configured, the league isn't covered, or
    the fixture isn't found on the current board (e.g. too far out)."""
    sport_key = DIV_TO_ODDS_SPORT_KEY.get(div)
    if not ODDS_API_KEY or not sport_key:
        return None
    try:
        events = _fetch_league_odds(sport_key)
    except (urllib.error.URLError, TimeoutError, OSError):
        return None

    target = f"{_normalize(home)}|{_normalize(away)}"
    candidates = {f"{_normalize(e.get('home_team'))}|{_normalize(e.get('away_team'))}": e for e in events}
    match = difflib.get_close_matches(target, list(candidates.keys()), n=1, cutoff=0.7)
    if not match:
        return None
    event = candidates[match[0]]

    h2h = _best_prices(event, "h2h")
    totals = _best_prices(event, "totals", point=2.5)
    if not h2h and not totals:
        return None

    result = {"event_home": event.get("home_team"), "event_away": event.get("away_team"), "bookmaker_count": len(event.get("bookmakers", []))}
    if h2h:
        result["odds_home"] = h2h.get(event.get("home_team"))
        result["odds_away"] = h2h.get(event.get("away_team"))
        result["odds_draw"] = h2h.get("Draw")
    if totals:
        result["odds_over_2_5"] = totals.get("Over")
        result["odds_under_2_5"] = totals.get("Under")
    return result


def get_afl_odds(home: str, away: str) -> dict | None:
    if not ODDS_API_KEY:
        return None
    try:
        events = _fetch_league_odds(AFL_SPORT_KEY)
    except (urllib.error.URLError, TimeoutError, OSError):
        return None

    target = f"{_normalize(home)}|{_normalize(away)}"
    candidates = {f"{_normalize(e.get('home_team'))}|{_normalize(e.get('away_team'))}": e for e in events}
    match = difflib.get_close_matches(target, list(candidates.keys()), n=1, cutoff=0.7)
    if not match:
        return None
    event = candidates[match[0]]

    h2h = _best_prices(event, "h2h")
    if not h2h:
        return None
    return {
        "event_home": event.get("home_team"), "event_away": event.get("away_team"),
        "bookmaker_count": len(event.get("bookmakers", [])),
        "odds_home": h2h.get(event.get("home_team")), "odds_away": h2h.get(event.get("away_team")),
    }
