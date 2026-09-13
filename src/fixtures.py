"""
Live fixtures: upcoming matches with a model prediction already attached, for
the dashboard's default view.

Sources
  * Soccer  — football-data.org for the nine divisions its free tier covers,
    TheSportsDB as a best-effort fallback for the rest.
  * AFL     — the Squiggle API, which is keyless and covers the whole fixture.

Coverage is reported honestly. The previous version dumped raw
`HTTPError: HTTP Error 429` strings into the UI for every league it failed to
fetch, which told the user nothing except that something was broken. It also
had no rate limiting at all, so a single refresh fired ~30 requests at an API
that allows ten a minute and most of them 429'd — the errors were largely
self-inflicted. Now every request goes through `http_budget`, each league is
cached independently, and a league that cannot be refreshed keeps serving its
last known fixtures (flagged stale) instead of vanishing from the board.

Each league ends up in exactly one coverage state:
    live        - fetched fresh or from a warm cache, with fixtures
    stale       - served from cache because the refresh could not be made
    no-fixtures - the source answered, but listed nothing in the window
    snapshot    - the live feed refused this host; serving a build-time capture
    unsupported - no free feed carries this competition's fixtures
    unconfigured- a feed covers it, but this server has no API key for it
    error       - the source answered with something unusable
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import afl_data
import config
import football_data as fd
import http_budget
import predictor as pred
import team_match
import tracking

THESPORTSDB_KEY = "3"  # shared free "test" key
BASE = f"https://www.thesportsdb.com/api/v1/json/{THESPORTSDB_KEY}"
LEAGUE_ID_CACHE_PATH = config.DATA_DIR / "league_id_map.json"
FIXTURE_CACHE_TTL_SECONDS = 20 * 60
# How long an INCOMPLETE board is held before it is retried. Long enough not
# to hammer a provider that just refused us, short enough that a cold-start
# race repairs itself in a minute or two rather than persisting a full window.
RETRY_TTL_SECONDS = 90

# TheSportsDB league IDs for divisions football-data.org's free tier misses.
# Verified against its all_leagues list; anything absent here simply has no
# free fixture feed and is reported as unsupported rather than as an error.
THESPORTSDB_LEAGUE_IDS = {
    "SC0": "4330",   # Scotland - Premiership
    "USA": "4346",   # USA - MLS
    "MEX": "4350",   # Mexico - Liga MX
    "T1": "4339",    # Turkey - Super Lig
    "G1": "4336",    # Greece - Super League
    "B1": "4338",    # Belgium - Pro League
    "ARG": "4406",   # Argentina - Primera Division
}

_cache = {"ts": 0.0, "data": None, "ttl": FIXTURE_CACHE_TTL_SECONDS}


def _normalize(name: str) -> str:
    return "".join(c.lower() for c in (name or "") if c.isalnum())


def _thesportsdb_upcoming(league_id: str, max_wait: float = 0.0) -> http_budget.Fetched:
    res = http_budget.get_json(
        f"{BASE}/eventsnextleague.php?id={league_id}",
        budget=http_budget.THESPORTSDB,
        cache_key=f"tsdb:next:{league_id}",
        ttl=FIXTURE_CACHE_TTL_SECONDS,
        max_wait=max_wait,
    )
    if not res.ok:
        return res
    events = res.data.get("events") or []
    for ev in events:
        ev.setdefault("provider", "thesportsdb")
        ev.setdefault("providerId", ev.get("idEvent"))
    return http_budget.Fetched(events, res.status, res.age_seconds, res.error)


def lookup_event(event_id: str, max_wait: float = 0.0) -> http_budget.Fetched:
    """One finished fixture by TheSportsDB event id, as
    {"home_score": int, "away_score": int} — or None data when the match has
    no final score yet.

    Grading these divisions has to be per-fixture. The bulk endpoints are
    unusable on the free key: eventspastleague returns a single event, and
    eventsseason truncates to the first fifteen of the season (verified — a
    2026 MLS season query came back with February only). The volume is small
    because only the seven TheSportsDB-only divisions land here, and the
    caller caps how many it looks up per sweep.
    """
    res = http_budget.get_json(
        f"{BASE}/lookupevent.php?id={event_id}",
        budget=http_budget.THESPORTSDB,
        cache_key=f"tsdb:event:{event_id}",
        # A finished score never changes, and an unfinished one is re-asked
        # on the next sweep anyway, so this only has to avoid re-asking
        # within a single grading pass.
        ttl=30 * 60,
        max_wait=max_wait,
    )
    if not res.ok:
        return res
    events = (res.data or {}).get("events") or []
    if not events:
        return http_budget.Fetched(None, res.status, res.age_seconds, "event not found")
    ev = events[0]
    home, away = ev.get("intHomeScore"), ev.get("intAwayScore")
    if home is None or away is None or str(home) == "" or str(away) == "":
        return http_budget.Fetched(None, res.status, res.age_seconds, None)
    try:
        score = {"home_score": int(home), "away_score": int(away)}
    except (TypeError, ValueError):
        return http_budget.Fetched(None, res.status, res.age_seconds, "unparseable score")
    return http_budget.Fetched(score, res.status, res.age_seconds, None)


def _build_soccer_fixtures(div: str, league_name: str, events: list[dict]) -> list[dict]:
    known = pred.list_soccer_teams(div)
    out = []
    for ev in events:
        home_raw, away_raw = ev.get("strHomeTeam"), ev.get("strAwayTeam")
        if not home_raw or not away_raw:
            continue
        home, home_how = team_match.match_team(home_raw, known)
        away, away_how = team_match.match_team(away_raw, known)
        try:
            prediction = pred.predict_soccer(div, home, away)
        except Exception as e:
            prediction = {"error": f"{type(e).__name__}: {e}"}
        try:
            tracking.record_soccer(div, league_name, ev, home_raw, away_raw, prediction)
        except Exception:
            pass  # tracking is best-effort and must never block the board
        out.append({
            "date": ev.get("dateEvent"),
            "time": ev.get("strTime"),
            "home_team_live_name": home_raw,
            "away_team_live_name": away_raw,
            "home_team": home,
            "away_team": away,
            # An unmatched side is priced at competition-average strength,
            # which is a materially weaker prediction. Say so rather than
            # letting it look like any other row.
            "unmatched": [n for n, how in ((home_raw, home_how), (away_raw, away_how))
                          if how == "unmatched"],
            "prediction": prediction,
        })
    return out


def _build_afl_fixtures(events: list[dict]) -> list[dict]:
    try:
        known = pred.list_afl_teams()
    except FileNotFoundError:
        known = []
    out = []
    for ev in events:
        home_raw, away_raw = ev.get("strHomeTeam"), ev.get("strAwayTeam")
        if not home_raw or not away_raw:
            continue
        home, home_how = team_match.match_team(home_raw, known) if known else (home_raw, "exact")
        away, away_how = team_match.match_team(away_raw, known) if known else (away_raw, "exact")
        try:
            prediction = pred.predict_afl(
                home, away, venue=ev.get("venue"), game_date=ev.get("dateEvent"),
                is_final=bool(ev.get("is_final")),
            )
        except Exception as e:
            prediction = {"error": f"{type(e).__name__}: {e}"}
        try:
            tracking.record_afl(ev, home_raw, away_raw, prediction)
        except Exception:
            pass
        out.append({
            "date": ev.get("dateEvent"),
            "time": ev.get("strTime"),
            "home_team_live_name": home_raw,
            "away_team_live_name": away_raw,
            "home_team": home,
            "away_team": away,
            "venue": ev.get("venue"),
            "round": ev.get("round"),
            "unmatched": [n for n, how in ((home_raw, home_how), (away_raw, away_how))
                          if how == "unmatched"],
            "prediction": prediction,
        })
    return out


# A model is called stale past this many days without a new training match.
# Roughly two months: long enough that a normal mid-season gap or an
# international break doesn't trip it, short enough to catch a competition
# whose data feed has quietly stopped updating.
MODEL_STALE_AFTER_DAYS = 60


def _model_freshness(code: str) -> dict:
    """When the model serving this competition last saw a real match.

    Worth surfacing because a stale model fails silently: it keeps returning
    confident probabilities from team strengths that are a season or two out
    of date, and nothing about the output looks wrong. Four of the trained
    competitions are fed by a source that stopped updating in December 2024,
    so this is a live condition, not a hypothetical.
    """
    try:
        if code == "AFL":
            trained = pred.load_afl_model().trained_as_of
        else:
            trained = pred.load_soccer_artifact(code)["trained_as_of"]
    except Exception:
        return {}
    try:
        age = (date.today() - datetime.strptime(trained, "%Y-%m-%d").date()).days
    except (ValueError, TypeError):
        return {"model_trained_as_of": trained}
    return {"model_trained_as_of": trained, "model_age_days": age,
            "model_stale": age > MODEL_STALE_AFTER_DAYS}


def _coverage(code: str, name: str, state: str, count: int = 0,
              source: str | None = None, detail: str | None = None,
              age_seconds: float = 0.0) -> dict:
    return {"code": code, "league": name, "state": state, "fixtures": count,
            "source": source, "detail": detail,
            "age_minutes": round(age_seconds / 60) if age_seconds else 0,
            **_model_freshness(code)}


def get_live_fixtures(force_refresh: bool = False) -> dict:
    """Returns fixtures grouped by league plus a per-league coverage report.

    Fetches carry a small wait budget rather than none. With nine covered
    divisions and nine slots per window nothing normally queues, so the common
    case is unaffected; the wait only bites when grading has taken a slot, and
    a few seconds there is far better than a division silently dropping off the
    board. Results are cached for twenty minutes, so this cost is paid rarely,
    and a league that still cannot be refreshed serves its cached fixtures and
    is flagged stale.
    """
    now = time.time()
    if not force_refresh and _cache["data"] is not None and \
            (now - _cache["ts"]) < _cache.get("ttl", FIXTURE_CACHE_TTL_SECONDS):
        return _cache["data"]

    result: dict = {"soccer": {}, "afl": {"league": "AFL", "fixtures": []},
                    "coverage": [], "generated_at": now}

    # ------------------------------------------------------------ soccer
    for div in pred.list_soccer_leagues():
        name = config.SOCCER_LEAGUES.get(div, div)
        res, source = None, None

        # A small wait budget: with 9 covered divisions and 9 slots per window
        # nothing normally queues, but if grading has spent a slot this lets a
        # division wait its turn instead of dropping off the board entirely.
        if fd.covers(div):
            res, source = fd.fetch_upcoming(div, max_wait=12.0), "football-data.org"
        elif div in THESPORTSDB_LEAGUE_IDS:
            res, source = _thesportsdb_upcoming(THESPORTSDB_LEAGUE_IDS[div], max_wait=6.0), "thesportsdb"

        if res is None:
            # Distinguish "we have no feed for this competition" from "we have
            # a feed but this deployment has no API key". Reporting the second
            # as unsupported sends the operator hunting for a data source they
            # already have.
            if div in fd.DIV_TO_FD_CODE and not fd.is_configured():
                result["coverage"].append(_coverage(
                    div, name, "unconfigured", source="football-data.org",
                    detail="Covered by football-data.org, but this server has no "
                           "FOOTBALL_DATA_API_KEY set."))
            else:
                result["coverage"].append(_coverage(
                    div, name, "unsupported",
                    detail="No free fixture feed carries this competition. "
                           "It can still be priced by hand on the Matchup tab."))
            continue

        if not res.ok:
            result["coverage"].append(_coverage(
                div, name, "error", source=source,
                detail=res.error or "the source returned nothing usable"))
            continue

        try:
            fixtures = _build_soccer_fixtures(div, name, res.data)
        except Exception as e:
            result["coverage"].append(_coverage(
                div, name, "error", source=source, detail=f"{type(e).__name__}: {e}"))
            continue

        if fixtures:
            result["soccer"][div] = {
                "league": name, "fixtures": fixtures,
                "source": source, "stale": res.is_stale,
                **_model_freshness(div),
            }
        # "no-fixtures" states only what we observed: the feed answered and
        # listed nothing inside the window. Calling that "off-season" would be
        # asserting a reason we have not checked — a mid-season international
        # break looks identical from here.
        state = "no-fixtures" if not fixtures else ("stale" if res.is_stale else "live")
        result["coverage"].append(_coverage(
            div, name, state, len(fixtures), source,
            detail=(res.error if res.is_stale else
                    (f"The feed listed no fixtures in the next "
                     f"{fd.UPCOMING_WINDOW_DAYS} days." if not fixtures else None)),
            age_seconds=res.age_seconds))

    # --------------------------------------------------------------- AFL
    try:
        afl_res = afl_data.fetch_upcoming()
        if not afl_res.ok:
            result["coverage"].append(_coverage(
                "AFL", "AFL", "error", source="squiggle",
                detail=afl_res.error or "no response"))
        else:
            fixtures = _build_afl_fixtures(afl_res.data)
            result["afl"]["fixtures"] = fixtures
            result["afl"]["source"] = "squiggle"
            result["afl"]["stale"] = afl_res.is_stale
            result["afl"].update(_model_freshness("AFL"))
            if not fixtures:
                state = "no-fixtures"
            elif afl_res.status == "snapshot":
                state = "snapshot"
            elif afl_res.is_stale:
                state = "stale"
            else:
                state = "live"
            result["coverage"].append(_coverage(
                "AFL", "AFL", state, len(fixtures), "squiggle",
                detail=("The AFL season runs March to September, so there are "
                        "no fixtures outside it.") if not fixtures else afl_res.error,
                age_seconds=afl_res.age_seconds))
    except Exception as e:
        result["coverage"].append(_coverage(
            "AFL", "AFL", "error", source="squiggle", detail=f"{type(e).__name__}: {e}"))

    # ----------------------------------------------------------- summary
    states: dict[str, int] = {}
    for c in result["coverage"]:
        states[c["state"]] = states.get(c["state"], 0) + 1
    result["summary"] = {
        "total_fixtures": sum(len(g["fixtures"]) for g in result["soccer"].values())
                          + len(result["afl"]["fixtures"]),
        "leagues_live": states.get("live", 0),
        "leagues_total": len(result["coverage"]),
        "states": states,
        "budgets": http_budget.budget_status(),
    }

    # An incomplete board must not be cached for the full window. A cold
    # container has no warm request budget and no disk cache to fall back on,
    # so the leagues that lose the race come back as errors — and pinning that
    # partial result for twenty minutes means every visitor sees a board with
    # competitions missing, when a retry a minute later would succeed. Cache
    # the incomplete answer just long enough to avoid hammering the provider,
    # then let it heal itself.
    incomplete = any(c["state"] == "error" for c in result["coverage"])
    _cache["ts"] = now
    _cache["ttl"] = RETRY_TTL_SECONDS if incomplete else FIXTURE_CACHE_TTL_SECONDS
    _cache["data"] = result
    return result


def resolve_league_ids(force_refresh: bool = False) -> dict:
    """Kept for the CLI's league-id inspection. The live path now uses the
    verified static map above rather than fuzzy-matching league names, which
    used to let one division silently claim another's ID."""
    if LEAGUE_ID_CACHE_PATH.exists() and not force_refresh:
        try:
            return json.loads(LEAGUE_ID_CACHE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    LEAGUE_ID_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEAGUE_ID_CACHE_PATH.write_text(json.dumps(THESPORTSDB_LEAGUE_IDS, indent=2))
    return dict(THESPORTSDB_LEAGUE_IDS)
