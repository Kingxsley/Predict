"""
Live fixtures integration: pulls upcoming matches from TheSportsDB (free
tier, no signup required for the shared "test" key) so the dashboard/API
can show real upcoming fixtures grouped by league, with model predictions
attached automatically — no manual team entry needed.

IMPORTANT — network note: the sandbox this was *built* in has an
allowlisted network that blocks essentially every external sports API
(ESPN, TheSportsDB, football-data.org, Odds API, etc. were all
unreachable during development — see README "Live fixtures" section).
That means the HTTP calls in this file are schema-correct against
TheSportsDB's documented, stable API but were verified locally with
mocked responses, NOT a live round trip. The very first thing to check
after deploying (Railway has normal internet access) is `/api/fixtures/live`.

Design:
  - `_get_json()` is the one place that makes an HTTP call, so it's easy
    to monkeypatch/mock in tests.
  - League IDs are resolved by name-matching against TheSportsDB's
    `all_leagues.php` list rather than fully hardcoded, so a wrong
    guessed ID self-corrects instead of silently fetching the wrong
    league. A small seed map biases/speeds up matching for the leagues
    we're fairly confident about; every resolved ID is still verified
    against the fetched league name before being trusted.
  - Results are cached in-process with a TTL, since the free tier is
    rate-limited (~30 req/min) and a live dashboard shouldn't hammer it
    on every page load.
  - Team names from the live feed are fuzzy-matched against the team
    names our trained models actually know (difflib), since a live feed
    and a historical CSV rarely spell every club identically
    ("Man United" vs "Manchester United"). Unmatched teams still work —
    predict_soccer/predict_basketball fall back to league-average
    strength for unseen teams rather than erroring.
"""
from __future__ import annotations

import difflib
import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config
import predictor as pred
import football_data as fd
import tracking

THESPORTSDB_KEY = "3"  # shared free "test" key; swap for a paid key via env var for higher limits
BASE = f"https://www.thesportsdb.com/api/v1/json/{THESPORTSDB_KEY}"
LEAGUE_ID_CACHE_PATH = config.DATA_DIR / "league_id_map.json"
FIXTURE_CACHE_TTL_SECONDS = 30 * 60  # 30 min, respects free-tier rate limits

# Best-effort seed hints for well-known league IDs (still verified against
# the live league-name list before use, never trusted blindly).
SEED_LEAGUE_ID_HINTS = {
    "E0": "4328",   # English Premier League
    "E1": "4329",   # English Championship
    "SP1": "4335",  # Spanish La Liga
    "I1": "4332",   # Italian Serie A
    "D1": "4331",   # German Bundesliga
    "F1": "4334",   # French Ligue 1
    "N1": "4337",   # Dutch Eredivisie
    "P1": "4344",   # Portuguese Primeira Liga
    "SC0": "4330",  # Scottish Premiership
    "USA": "4346",  # MLS
    "BRA": "4351",  # Brazilian Serie A
    "MEX": "4350",  # Liga MX
}
NBA_LEAGUE_ID_HINT = "4387"

# Alternate search terms tried when the primary name doesn't fuzzy-match
# well (mostly abbreviations/short forms that don't compare well against
# a full league name character-by-character).
LEAGUE_NAME_ALIASES = {
    "USA": ["Major League Soccer", "American Major League Soccer", "MLS"],
    "MEX": ["Mexican Primera Division", "Liga MX", "Mexican Liga MX"],
    "ARG": ["Argentinian Primera Division", "Argentine Primera Division"],
    "BRA": ["Brazilian Serie A", "Campeonato Brasileiro"],
    "T1": ["Turkish Super Lig", "Turkish Super League"],
    "G1": ["Greek Super League"],
}

_fixture_cache = {"ts": 0.0, "data": None}


def _get_json(url: str, timeout: int = 10) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "sports-predictor/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _normalize(name: str) -> str:
    return "".join(c.lower() for c in name if c.isalnum())


def resolve_league_ids(force_refresh: bool = False) -> dict:
    """Returns {div_code: thesportsdb_league_id} for every trained soccer
    league plus a special 'NBA' key. Caches to disk since this rarely
    changes; call with force_refresh=True to re-resolve."""
    if LEAGUE_ID_CACHE_PATH.exists() and not force_refresh:
        try:
            return json.loads(LEAGUE_ID_CACHE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass  # fall through and re-resolve

    all_leagues = _get_json(f"{BASE}/all_leagues.php").get("leagues") or []
    soccer_leagues = [l for l in all_leagues if l.get("strSport") == "Soccer"]
    basketball_leagues = [l for l in all_leagues if l.get("strSport") == "Basketball"]

    by_norm_name = {_normalize(l["strLeague"]): l["idLeague"] for l in soccer_leagues}

    resolved = {}
    claimed_ids = set()  # hard guard: never let two divisions resolve to the same league ID
    divs = pred.list_soccer_leagues()

    # Pass 1: claim every valid seed hint FIRST, across all divisions,
    # before any fuzzy fallback runs. Doing hints and fuzzy fallback in a
    # single per-division pass let a division with no real match on this
    # provider's list (which only carries ~10 leagues) fuzzy-steal an ID
    # that rightfully belonged to a division processed later — e.g. "BRA"
    # grabbing Italian Serie A's ID before "I1" got a chance to claim its
    # own correct hint. Resolving hints first removes that race.
    for div in divs:
        hint_id = SEED_LEAGUE_ID_HINTS.get(div)
        hint_valid = hint_id and any(l["idLeague"] == hint_id for l in soccer_leagues) and hint_id not in claimed_ids
        if hint_valid:
            resolved[div] = hint_id
            claimed_ids.add(hint_id)

    # Pass 2: fuzzy-match whatever's left against whatever IDs are still
    # unclaimed. Any division with no real counterpart on this provider
    # correctly ends up unresolved here instead of stealing someone else's ID.
    for div in divs:
        if div in resolved:
            continue
        our_name = config.SOCCER_LEAGUES.get(div, div)
        # Try the FULL name first (e.g. "Italy - Serie A") — this is what
        # disambiguates "Serie A" (Italy) from "Serie A" (Brazil), since a
        # bare competition suffix alone is often shared across countries.
        # Only fall back to the bare suffix, and only at a stricter cutoff,
        # since it's the most collision-prone term.
        search_terms = [our_name] + LEAGUE_NAME_ALIASES.get(div, []) + [our_name.split(" - ")[-1]]
        candidates = list(by_norm_name.keys())
        found = None
        for cutoff in (0.6, 0.45):
            for term in search_terms:
                # ask for a few candidates so an already-claimed top match
                # doesn't block a perfectly good runner-up
                matches = difflib.get_close_matches(_normalize(term), candidates, n=5, cutoff=cutoff)
                unclaimed = [m for m in matches if by_norm_name[m] not in claimed_ids]
                if unclaimed:
                    found = unclaimed[0]
                    break
            if found:
                break
        if found:
            resolved[div] = by_norm_name[found]
            claimed_ids.add(by_norm_name[found])

    for l in basketball_leagues:
        if l["idLeague"] == NBA_LEAGUE_ID_HINT or _normalize(l["strLeague"]) == "nba":
            resolved["NBA"] = l["idLeague"]
            break
    else:
        if basketball_leagues:
            resolved["NBA"] = NBA_LEAGUE_ID_HINT  # last-resort fallback

    LEAGUE_ID_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEAGUE_ID_CACHE_PATH.write_text(json.dumps(resolved, indent=2))
    return resolved


def fetch_upcoming_events(league_id: str) -> list[dict]:
    """Raw upcoming events for one TheSportsDB league ID."""
    data = _get_json(f"{BASE}/eventsnextleague.php?id={league_id}")
    return data.get("events") or []


def _fuzzy_team_match(name: str, known_teams: list[str]) -> str:
    norm_map = {_normalize(t): t for t in known_teams}
    if _normalize(name) in norm_map:
        return norm_map[_normalize(name)]
    # 0.7 cutoff: loose enough to catch real spelling/suffix variants
    # ("Fluminense FC" -> "Fluminense", ratio .91) but tight enough to
    # reject two different real clubs with superficially similar names
    # ("CA Paranaense" -> "Parana", ratio .67 — a different club entirely).
    # A wrong-but-confident match is worse than no match: it would silently
    # run the prediction, H2H, and form rationale for the wrong team under
    # the right team's name. Falling back to the raw (unmatched) name is
    # the honest outcome here — predictor.py handles unseen teams already.
    match = difflib.get_close_matches(_normalize(name), list(norm_map.keys()), n=1, cutoff=0.7)
    return norm_map[match[0]] if match else name


def get_live_fixtures(force_refresh: bool = False) -> dict:
    """Returns {"soccer": {div: {"league": name, "fixtures": [...]}}, "basketball": {"fixtures": [...]}}
    with model predictions merged into each fixture. Cached in-process for
    FIXTURE_CACHE_TTL_SECONDS."""
    now = time.time()
    if not force_refresh and _fixture_cache["data"] is not None and \
            (now - _fixture_cache["ts"]) < FIXTURE_CACHE_TTL_SECONDS:
        return _fixture_cache["data"]

    league_ids = resolve_league_ids()
    result = {"soccer": {}, "basketball": {"league": "NBA", "fixtures": []}, "errors": []}

    for div in pred.list_soccer_leagues():
        league_name = config.SOCCER_LEAGUES.get(div, div)

        # Prefer football-data.org where it covers this division — real
        # current-season data with no ID-guessing needed. Falls back to
        # TheSportsDB (a different, mostly non-overlapping set of leagues)
        # if football-data.org isn't configured, doesn't cover this
        # division, or the request fails.
        events, provider_error = None, None
        if fd.is_configured() and div in fd.DIV_TO_FD_CODE:
            try:
                events = fd.fetch_upcoming_events(div)
            except Exception as e:
                provider_error = f"football-data.org fetch failed ({type(e).__name__}: {e})"
                events = None

        if events is None:
            league_id = league_ids.get(div)
            if not league_id:
                result["errors"].append(f"{div}: no live-fixtures source available"
                                         + (f" (football-data.org: {provider_error})" if provider_error else ""))
                continue
            try:
                events = fetch_upcoming_events(league_id)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                result["errors"].append(f"{div}: fetch failed ({type(e).__name__}: {e})")
                continue
            except Exception as e:
                result["errors"].append(f"{div}: unexpected error ({type(e).__name__}: {e})")
                continue

        for ev in events:
            ev.setdefault("provider", "thesportsdb")
            ev.setdefault("providerId", ev.get("idEvent"))

        try:
            known_teams = pred.list_soccer_teams(div)
            fixtures = []
            for ev in events:
                home_raw, away_raw = ev.get("strHomeTeam"), ev.get("strAwayTeam")
                if not home_raw or not away_raw:
                    continue
                home = _fuzzy_team_match(home_raw, known_teams)
                away = _fuzzy_team_match(away_raw, known_teams)
                try:
                    prediction = pred.predict_soccer(div, home, away)
                except Exception as e:
                    prediction = {"error": f"{type(e).__name__}: {e}"}
                try:
                    tracking.record_soccer(div, league_name, ev, home_raw, away_raw, prediction)
                except Exception:
                    pass  # tracking is best-effort, never block the live-fixtures response
                fixtures.append({
                    "date": ev.get("dateEvent"),
                    "time": ev.get("strTime"),
                    "home_team_live_name": home_raw,
                    "away_team_live_name": away_raw,
                    "home_team": home,
                    "away_team": away,
                    "prediction": prediction,
                })
            result["soccer"][div] = {"league": league_name, "fixtures": fixtures}
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            result["errors"].append(f"{div}: fetch failed ({type(e).__name__}: {e})")
        except Exception as e:
            result["errors"].append(f"{div}: unexpected error ({type(e).__name__}: {e})")

    nba_id = league_ids.get("NBA")
    if nba_id:
        try:
            events = fetch_upcoming_events(nba_id)
            for ev in events:
                ev.setdefault("provider", "thesportsdb")
                ev.setdefault("providerId", ev.get("idEvent"))
            known_teams = pred.list_basketball_teams()
            fixtures = []
            for ev in events:
                home_raw, away_raw = ev.get("strHomeTeam"), ev.get("strAwayTeam")
                if not home_raw or not away_raw:
                    continue
                home = _fuzzy_team_match(home_raw, known_teams)
                away = _fuzzy_team_match(away_raw, known_teams)
                try:
                    prediction = pred.predict_basketball(home, away, ev.get("dateEvent"))
                except Exception as e:
                    prediction = {"error": f"{type(e).__name__}: {e}"}
                try:
                    tracking.record_basketball(ev, home_raw, away_raw, prediction)
                except Exception:
                    pass
                fixtures.append({
                    "date": ev.get("dateEvent"),
                    "time": ev.get("strTime"),
                    "home_team_live_name": home_raw,
                    "away_team_live_name": away_raw,
                    "home_team": home,
                    "away_team": away,
                    "prediction": prediction,
                })
            result["basketball"]["fixtures"] = fixtures
        except Exception as e:
            result["errors"].append(f"NBA: fetch failed ({type(e).__name__}: {e})")

    _fixture_cache["ts"] = now
    _fixture_cache["data"] = result
    return result
