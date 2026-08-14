"""
football-data.org client: a second live-fixtures source, used ahead of
TheSportsDB for the divisions it covers. Its free tier has narrower
competition coverage (9 of our 20 trained divisions) but real current-season
data with no ID-guessing needed — competition codes are a small, stable,
documented set, unlike TheSportsDB's free "test" key which only exposes a
different 10 leagues and needs fuzzy name-matching to find them.

Requires a free registered API key (no payment) from
https://www.football-data.org/client/register, read from the
FOOTBALL_DATA_API_KEY env var. Never hardcode a real key here — this file
is committed to git. Everything degrades gracefully when the env var is
unset: callers should fall back to TheSportsDB for any division this module
doesn't cover.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

FOOTBALL_DATA_KEY = os.environ.get("FOOTBALL_DATA_API_KEY", "")
BASE = "https://api.football-data.org/v4"
# How far ahead "upcoming" reaches. football-data.org's SCHEDULED filter
# returns the entire rest of the season (380-550+ matches per league) with
# no cap — a bounded date window keeps this to genuinely upcoming fixtures
# instead of a full season dump, and keeps per-refresh latency (each
# fixture gets a full prediction + rationale computed) reasonable.
UPCOMING_WINDOW_DAYS = 21

# Our division code -> football-data.org competition code. Free-tier
# ("TIER_ONE") competitions only; verified against the live /v4/competitions
# list. Two of these (PPL, BSA) aren't available at all via TheSportsDB's
# free key, so this genuinely extends coverage rather than just duplicating it.
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


def is_configured() -> bool:
    return bool(FOOTBALL_DATA_KEY)


def _get_json(url: str, timeout: int = 10) -> dict:
    req = urllib.request.Request(url, headers={
        "X-Auth-Token": FOOTBALL_DATA_KEY,
        "User-Agent": "sports-predictor/1.0",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_upcoming_events(div: str) -> list[dict]:
    """Returns upcoming events normalized to the same shape fixtures.py
    already expects from TheSportsDB (dateEvent/strTime/strHomeTeam/
    strAwayTeam), so both providers can feed the same downstream code."""
    fd_code = DIV_TO_FD_CODE.get(div)
    if not fd_code or not FOOTBALL_DATA_KEY:
        return []
    today = datetime.now(timezone.utc).date()
    date_from = today.isoformat()
    date_to = (today + timedelta(days=UPCOMING_WINDOW_DAYS)).isoformat()
    data = _get_json(f"{BASE}/competitions/{fd_code}/matches?dateFrom={date_from}&dateTo={date_to}")
    events = []
    for m in data.get("matches") or []:
        if m.get("status") not in ("SCHEDULED", "TIMED"):
            continue  # skip already-finished/postponed/in-play matches in the window
        utc_date = m.get("utcDate") or ""
        date_part, _, time_part = utc_date.partition("T")
        events.append({
            "dateEvent": date_part,
            "strTime": time_part.rstrip("Z") if time_part else None,
            "strHomeTeam": (m.get("homeTeam") or {}).get("name"),
            "strAwayTeam": (m.get("awayTeam") or {}).get("name"),
            "provider": "football-data.org",
            "providerId": str(m.get("id")) if m.get("id") is not None else None,
        })
    return events


def fetch_result(match_id: str) -> dict | None:
    """Looks up one match by football-data.org's own id and returns the
    final score if it's finished, else None. Used to grade a previously
    logged prediction once its match has actually been played."""
    if not FOOTBALL_DATA_KEY:
        return None
    data = _get_json(f"{BASE}/matches/{match_id}")
    if data.get("status") != "FINISHED":
        return None
    score = (data.get("score") or {}).get("fullTime") or {}
    home, away = score.get("home"), score.get("away")
    if home is None or away is None:
        return None
    return {"home_score": home, "away_score": away}
