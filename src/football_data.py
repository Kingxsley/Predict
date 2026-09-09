"""
football-data.org client: the primary live-fixtures and results source for
the divisions its free tier covers.

Requires a free registered API key (no payment) from
https://www.football-data.org/client/register, read from the
FOOTBALL_DATA_API_KEY env var. Never hardcode a real key here — this file is
committed to git. Everything degrades gracefully when the env var is unset.

Rate limiting: the free tier allows 10 requests/minute, shared across
*everything* this app does. All calls go through `http_budget`, which
serialises them against that quota and serves cached responses (stale if
necessary) rather than failing. Before that existed, a board refresh and a
grading pass would together fire 30+ requests and both get 429'd.

Results are fetched in bulk — one query per competition returns every
finished match in a date window — rather than one lookup per match. Grading
250 logged predictions used to cost 250 requests against a 10/minute quota,
which is why the prediction log never graded anything.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import http_budget

FOOTBALL_DATA_KEY = os.environ.get("FOOTBALL_DATA_API_KEY", "")
BASE = "https://api.football-data.org/v4"

# How far ahead "upcoming" reaches. football-data.org's SCHEDULED filter
# returns the entire rest of the season with no cap; a bounded window keeps
# this to genuinely upcoming fixtures and keeps per-refresh latency
# reasonable, since every fixture gets a full prediction + rationale.
UPCOMING_WINDOW_DAYS = 14

# Our division code -> football-data.org competition code. Free-tier
# ("TIER_ONE") competitions only.
DIV_TO_FD_CODE = {
    "E0": "PL",    # England - Premier League
    "E1": "ELC",   # England - Championship
    "D1": "BL1",   # Germany - Bundesliga
    "F1": "FL1",   # France - Ligue 1
    "I1": "SA",    # Italy - Serie A
    "N1": "DED",   # Netherlands - Eredivisie
    "SP1": "PD",   # Spain - La Liga
    "P1": "PPL",   # Portugal - Primeira Liga
    "BRA": "BSA",  # Brazil - Serie A
}


# Statuses that mean "not an upcoming fixture". Anything else, including a
# value this API decides to put in the status field that isn't a status at
# all, is kept and judged on its kick-off time instead.
_NOT_UPCOMING = {"FINISHED", "AWARDED", "CANCELLED", "CANCELED", "POSTPONED", "SUSPENDED"}


def is_configured() -> bool:
    return bool(FOOTBALL_DATA_KEY)


def covers(div: str) -> bool:
    return is_configured() and div in DIV_TO_FD_CODE


def _headers() -> dict:
    return {"X-Auth-Token": FOOTBALL_DATA_KEY}


def fetch_upcoming(div: str, max_wait: float = 0.0) -> http_budget.Fetched:
    """Upcoming events for one division, normalized to the shape fixtures.py
    expects. Cached 30 minutes; a rate-limited refresh serves the previous
    response rather than emptying the league off the board."""
    code = DIV_TO_FD_CODE.get(div)
    if not code or not FOOTBALL_DATA_KEY:
        return http_budget.Fetched(None, "missing", error="not covered by football-data.org")

    today = datetime.now(timezone.utc).date()
    date_to = (today + timedelta(days=UPCOMING_WINDOW_DAYS)).isoformat()
    url = f"{BASE}/competitions/{code}/matches?dateFrom={today.isoformat()}&dateTo={date_to}"

    res = http_budget.get_json(
        url, budget=http_budget.FOOTBALL_DATA, headers=_headers(),
        # Keyed on the competition and day, not the full URL, so the rolling
        # date window doesn't invalidate the cache on every single request.
        cache_key=f"fd:upcoming:{code}:{today.isoformat()}",
        ttl=30 * 60, max_wait=max_wait,
    )
    if not res.ok:
        return res

    events = []
    now = datetime.now(timezone.utc)
    for m in res.data.get("matches") or []:
        # Deny-list, not allow-list. football-data.org is not consistent about
        # this field: some competitions report status as "TIMED"/"SCHEDULED",
        # others put a timestamp there instead. An allow-list silently dropped
        # every match from the inconsistent competitions — Brazil and the
        # Bundesliga vanished from the board entirely while reporting no error.
        # Treating an unrecognised status as "still to be played" and relying
        # on the kick-off time is the safe direction to be wrong in.
        if str(m.get("status") or "").upper() in _NOT_UPCOMING:
            continue
        utc_date = m.get("utcDate") or ""
        try:
            if utc_date and datetime.fromisoformat(utc_date.replace("Z", "+00:00")) < now - timedelta(hours=3):
                continue  # already kicked off well before now
        except ValueError:
            pass
        date_part, _, time_part = utc_date.partition("T")
        events.append({
            "dateEvent": date_part,
            "strTime": time_part.rstrip("Z") if time_part else None,
            "strHomeTeam": (m.get("homeTeam") or {}).get("name"),
            "strAwayTeam": (m.get("awayTeam") or {}).get("name"),
            "provider": "football-data.org",
            "providerId": str(m.get("id")) if m.get("id") is not None else None,
        })
    return http_budget.Fetched(events, res.status, res.age_seconds, res.error)


def fetch_finished_results(div: str, date_from: str, date_to: str,
                            max_wait: float = 0.0) -> http_budget.Fetched:
    """Every finished match for one division in a date window, as
    {provider_id: {"home_score": int, "away_score": int}}.

    This is the bulk replacement for the old per-match lookup. One request
    grades an entire league's backlog instead of one prediction.
    """
    code = DIV_TO_FD_CODE.get(div)
    if not code or not FOOTBALL_DATA_KEY:
        return http_budget.Fetched(None, "missing", error="not covered by football-data.org")

    url = (f"{BASE}/competitions/{code}/matches"
           f"?status=FINISHED&dateFrom={date_from}&dateTo={date_to}")
    res = http_budget.get_json(
        url, budget=http_budget.FOOTBALL_DATA, headers=_headers(),
        cache_key=f"fd:finished:{code}:{date_from}:{date_to}",
        # Short TTL: a match that finished five minutes ago should be
        # gradeable now, but re-asking on every page load is what caused
        # the 429 storm in the first place.
        ttl=60 * 60, max_wait=max_wait,
    )
    if not res.ok:
        return res

    results = {}
    for m in res.data.get("matches") or []:
        if m.get("status") != "FINISHED":
            continue
        score = (m.get("score") or {}).get("fullTime") or {}
        home, away = score.get("home"), score.get("away")
        if home is None or away is None:
            continue
        mid = m.get("id")
        if mid is not None:
            results[str(mid)] = {"home_score": int(home), "away_score": int(away)}
    return http_budget.Fetched(results, res.status, res.age_seconds, res.error)
