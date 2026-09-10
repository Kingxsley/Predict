"""
AFL data via the Squiggle API (https://api.squiggle.com.au).

Squiggle is free, needs no API key, and carries the full VFL/AFL match
history along with the current season's fixture and live results. That makes
it a single source for all three things this app needs — training data,
upcoming fixtures, and final scores for grading — which is a better position
than the soccer side, where fixtures and results come from a rate-limited
keyed provider and history comes from a static CSV dump.

Squiggle asks for courteous use and a User-Agent that identifies the caller
rather than publishing a hard rate limit; `http_budget` supplies both and
caches aggressively, since a completed season never changes again.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
import http_budget

BASE = "https://api.squiggle.com.au"

# Squiggle sits behind Cloudflare and blocks datacenter IP ranges: the same
# request that succeeds from a laptop returns a Cloudflare 403 HTML page from
# a cloud host. That is a deliberate anti-abuse control and is not something
# to work around, so instead `ingest_afl.py` captures the upcoming fixture
# list from a permitted network and commits it, and this module falls back to
# that snapshot when the live call is refused. The snapshot is always
# labelled as such in the UI, with its capture date, so nobody mistakes it
# for a live read.
SNAPSHOT_PATH = config.DATA_DIR / "afl" / "upcoming_snapshot.json"

# The competition has been eighteen clubs and a stable finals structure since
# 2012; going back further mixes in eras with different team counts, a
# different finals system and materially different scoring rates, which hurts
# an Elo/margin model more than the extra rows help it.
HISTORY_START_YEAR = 2012

UPCOMING_WINDOW_DAYS = 21


def _get(query: str, cache_key: str, ttl: float, max_wait: float = 0.0) -> http_budget.Fetched:
    return http_budget.get_json(
        f"{BASE}/?q={query}",
        budget=http_budget.SQUIGGLE,
        cache_key=cache_key,
        ttl=ttl,
        max_wait=max_wait,
    )


def fetch_teams(max_wait: float = 5.0) -> http_budget.Fetched:
    res = _get("teams", "afl:teams", ttl=7 * 24 * 3600, max_wait=max_wait)
    if not res.ok:
        return res
    teams = [t for t in (res.data.get("teams") or []) if t.get("name")]
    return http_budget.Fetched(teams, res.status, res.age_seconds, res.error)


def fetch_games(year: int, max_wait: float = 0.0) -> http_budget.Fetched:
    """Every game Squiggle has for one season, finished or not."""
    this_year = datetime.now(timezone.utc).year
    # A finished season is immutable, so cache it effectively forever; the
    # live season needs to come back around often enough to pick up results.
    ttl = 30 * 24 * 3600 if year < this_year else 30 * 60
    res = _get(f"games;year={year}", f"afl:games:{year}", ttl=ttl, max_wait=max_wait)
    if not res.ok:
        return res
    return http_budget.Fetched(res.data.get("games") or [], res.status, res.age_seconds, res.error)


def _is_complete(game: dict) -> bool:
    return game.get("complete") == 100 and game.get("hscore") is not None


def _utc_parts(game: dict) -> tuple[str | None, str | None]:
    """Squiggle gives a venue-local time plus a tz offset and a unix
    timestamp. The timestamp is the only unambiguous field, so derive the
    UTC date and time from it and let the browser localise for display."""
    ts = game.get("unixtime")
    if not ts:
        raw = (game.get("date") or "")
        date_part, _, time_part = raw.partition(" ")
        return (date_part or None), (time_part or None)
    dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    return dt.date().isoformat(), dt.strftime("%H:%M:%S")


def fetch_upcoming(max_wait: float = 0.0) -> http_budget.Fetched:
    """Upcoming AFL fixtures, normalized to the same shape fixtures.py uses
    for soccer so both sports go through one rendering path."""
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=UPCOMING_WINDOW_DAYS)

    # Across the summer break the next fixtures belong to next season, so
    # look at both years rather than reporting an empty board in January.
    events: list[dict] = []
    status, age, error = "missing", 0.0, None
    for year in (now.year, now.year + 1):
        res = fetch_games(year, max_wait=max_wait)
        if not res.ok:
            error = error or res.error
            continue
        status, age = res.status, res.age_seconds
        for g in res.data:
            if _is_complete(g):
                continue
            ts = g.get("unixtime")
            if ts:
                when = datetime.fromtimestamp(int(ts), tz=timezone.utc)
                if when < now - timedelta(hours=6) or when > horizon:
                    continue
            date_part, time_part = _utc_parts(g)
            if not g.get("hteam") or not g.get("ateam"):
                continue
            events.append({
                "dateEvent": date_part,
                "strTime": time_part,
                "strHomeTeam": g.get("hteam"),
                "strAwayTeam": g.get("ateam"),
                "provider": "squiggle",
                "providerId": str(g.get("id")) if g.get("id") is not None else None,
                "venue": g.get("venue"),
                "round": g.get("roundname") or (f"Round {g['round']}" if g.get("round") else None),
                "is_final": bool(g.get("is_final")),
            })
        if events:
            break

    if not events and error:
        snapshot = load_snapshot()
        if snapshot is not None:
            return snapshot
        return http_budget.Fetched(None, "missing", age, error)
    events.sort(key=lambda e: (e["dateEvent"] or "", e["strTime"] or ""))
    return http_budget.Fetched(events, status if events else "fresh", age, error)


def load_snapshot() -> http_budget.Fetched | None:
    """The committed fixture snapshot, filtered to games still ahead of us.
    Returns None when there is no snapshot or nothing in it is still to come."""
    if not SNAPSHOT_PATH.exists():
        return None
    try:
        blob = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    today = datetime.now(timezone.utc).date().isoformat()
    events = [e for e in blob.get("events", []) if (e.get("dateEvent") or "") >= today]
    if not events:
        return None
    captured = blob.get("captured_at", "an earlier build")
    return http_budget.Fetched(
        events, "snapshot", 0.0,
        f"Live feed unreachable from this host; showing the fixture captured at build time ({captured}).")


def write_snapshot(events: list[dict]) -> Path:
    """Called by the ingest script from a network Squiggle permits."""
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(json.dumps({
        "captured_at": datetime.now(timezone.utc).date().isoformat(),
        "events": events,
    }, indent=2), encoding="utf-8")
    return SNAPSHOT_PATH


def fetch_results(year: int, max_wait: float = 0.0) -> http_budget.Fetched:
    """Finished games for one season as {game_id: {home_score, away_score}},
    for grading logged predictions in bulk."""
    res = fetch_games(year, max_wait=max_wait)
    if not res.ok:
        return res
    out = {}
    for g in res.data:
        if not _is_complete(g) or g.get("id") is None:
            continue
        out[str(g["id"])] = {
            "home_score": int(g.get("hscore") or 0),
            "away_score": int(g.get("ascore") or 0),
        }
    return http_budget.Fetched(out, res.status, res.age_seconds, res.error)


def fetch_history(start_year: int = HISTORY_START_YEAR, end_year: int | None = None,
                  max_wait: float = 30.0) -> list[dict]:
    """Flat list of every completed game across a range of seasons, in the
    shape ingest_afl.py turns into the training frame. Used offline by the
    ingest script, so it may block on the rate budget."""
    end_year = end_year or datetime.now(timezone.utc).year
    rows: list[dict] = []
    for year in range(start_year, end_year + 1):
        res = fetch_games(year, max_wait=max_wait)
        if not res.ok:
            print(f"  ! {year}: {res.error}")
            continue
        completed = [g for g in res.data if _is_complete(g)]
        for g in completed:
            date_part, time_part = _utc_parts(g)
            rows.append({
                "game_id": g.get("id"),
                "season": year,
                "date": date_part,
                "time": time_part,
                "round": g.get("round"),
                "round_name": g.get("roundname"),
                "is_final": int(bool(g.get("is_final"))),
                "is_grand_final": int(bool(g.get("is_grand_final"))),
                "venue": g.get("venue"),
                "home_name": g.get("hteam"),
                "away_name": g.get("ateam"),
                "home_score": int(g.get("hscore") or 0),
                "away_score": int(g.get("ascore") or 0),
                "home_goals": g.get("hgoals"),
                "away_goals": g.get("agoals"),
            })
        print(f"  {year}: {len(completed)} completed games ({res.status})")
    return rows
