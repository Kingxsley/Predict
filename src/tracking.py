"""
Prediction tracking log: records every prediction made for a live fixture,
then grades it against the real final score once the match has actually
been played. Nothing here is simulated — grading only happens against a
real result fetched from the same provider (football-data.org or
TheSportsDB) that supplied the fixture in the first place.

Persisted to data/prediction_log.json as a flat JSON list. This is a
single-file, single-process log (no database) - fine for this app's scale,
but note it lives on local disk: a platform deploy without a persistent
volume (e.g. Railway without a mounted volume) will lose it on redeploy.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config
import football_data as fd

LOG_PATH = config.DATA_DIR / "prediction_log.json"
THESPORTSDB_BASE = "https://www.thesportsdb.com/api/v1/json/3"
GRADE_AFTER_DAYS = 1  # only attempt grading once the fixture date is at least this old
MAX_GRADE_ATTEMPTS = 6  # give up (mark "unresolved") after this many failed grading passes
MAX_GRADE_PER_CALL = 25  # cap how many ungraded entries one API request will attempt, to bound latency


def _normalize(name: str) -> str:
    return "".join(c.lower() for c in (name or "") if c.isalnum())


def _load() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    try:
        return json.loads(LOG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save(entries: list[dict]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def _fixture_key(sport: str, div: str | None, ev_date: str | None, home: str, away: str) -> str:
    return f"{sport}:{div or ''}:{ev_date or ''}:{_normalize(home)}:{_normalize(away)}"


def record_soccer(div: str, league: str, ev: dict, home_raw: str, away_raw: str, prediction: dict) -> None:
    if "error" in prediction:
        return
    key = _fixture_key("soccer", div, ev.get("dateEvent"), home_raw, away_raw)
    entries = _load()
    if any(e["key"] == key for e in entries):
        return
    probs = {"H": prediction["prob_home_win"], "D": prediction["prob_draw"], "A": prediction["prob_away_win"]}
    pick = max(probs, key=probs.get)
    entries.append({
        "key": key, "sport": "soccer", "div": div, "league": league,
        "date": ev.get("dateEvent"), "home": home_raw, "away": away_raw,
        "provider": ev.get("provider"), "provider_id": ev.get("providerId"),
        "predicted_pick": pick, "predicted_probs": probs,
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "graded": False, "attempts": 0, "correct": None,
        "actual_home_score": None, "actual_away_score": None, "actual_pick": None, "graded_at": None,
    })
    _save(entries)


def record_basketball(ev: dict, home_raw: str, away_raw: str, prediction: dict) -> None:
    if "error" in prediction:
        return
    key = _fixture_key("basketball", None, ev.get("dateEvent"), home_raw, away_raw)
    entries = _load()
    if any(e["key"] == key for e in entries):
        return
    pick = "HOME" if prediction["prob_home_win"] >= 0.5 else "AWAY"
    entries.append({
        "key": key, "sport": "basketball", "div": None, "league": "NBA",
        "date": ev.get("dateEvent"), "home": home_raw, "away": away_raw,
        "provider": ev.get("provider"), "provider_id": ev.get("providerId"),
        "predicted_pick": pick,
        "predicted_probs": {"HOME": prediction["prob_home_win"], "AWAY": prediction["prob_away_win"]},
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "graded": False, "attempts": 0, "correct": None,
        "actual_home_score": None, "actual_away_score": None, "actual_pick": None, "graded_at": None,
    })
    _save(entries)


def _fetch_thesportsdb_result(event_id: str) -> dict | None:
    req = urllib.request.Request(f"{THESPORTSDB_BASE}/lookupevent.php?id={event_id}",
                                  headers={"User-Agent": "sports-predictor/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    events = data.get("events") or []
    if not events:
        return None
    ev = events[0]
    home, away = ev.get("intHomeScore"), ev.get("intAwayScore")
    if home is None or away is None:
        return None
    return {"home_score": int(home), "away_score": int(away)}


def _actual_pick(sport: str, home_score: int, away_score: int) -> str:
    if sport == "basketball":
        return "HOME" if home_score > away_score else "AWAY"
    if home_score > away_score:
        return "H"
    if away_score > home_score:
        return "A"
    return "D"


def grade_pending() -> None:
    """Attempts to fetch a real final score for every ungraded entry whose
    fixture date has passed, and grades it (correct/incorrect) against the
    model's predicted pick. Entries with no provider id, or that fail
    repeatedly, are marked "unresolved" after MAX_GRADE_ATTEMPTS rather than
    retried forever."""
    entries = _load()
    today = date.today()
    checked = 0
    changed = False

    for entry in entries:
        if entry.get("graded") or checked >= MAX_GRADE_PER_CALL:
            continue
        ev_date_str = entry.get("date")
        if not ev_date_str:
            continue
        try:
            ev_date = datetime.strptime(ev_date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if (today - ev_date).days < GRADE_AFTER_DAYS:
            continue  # too soon - match may not have finished yet

        checked += 1
        provider, provider_id = entry.get("provider"), entry.get("provider_id")
        result = None
        try:
            if provider == "football-data.org" and provider_id:
                result = fd.fetch_result(provider_id)
            elif provider == "thesportsdb" and provider_id:
                result = _fetch_thesportsdb_result(provider_id)
        except Exception:
            result = None

        entry["attempts"] = entry.get("attempts", 0) + 1
        if result is not None:
            home_score, away_score = result["home_score"], result["away_score"]
            actual_pick = _actual_pick(entry["sport"], home_score, away_score)
            entry["actual_home_score"] = home_score
            entry["actual_away_score"] = away_score
            entry["actual_pick"] = actual_pick
            entry["correct"] = (actual_pick == entry["predicted_pick"])
            entry["graded"] = True
            entry["graded_at"] = datetime.now(timezone.utc).isoformat()
            changed = True
        elif entry["attempts"] >= MAX_GRADE_ATTEMPTS:
            entry["graded"] = True
            entry["correct"] = None  # unresolved - no result ever available, not a loss
            entry["graded_at"] = datetime.now(timezone.utc).isoformat()
            changed = True

    if changed:
        _save(entries)


def get_log() -> dict:
    """Grades whatever can be graded right now, then returns the full log
    plus a summary (overall + per-sport accuracy, most recent first)."""
    grade_pending()
    entries = _load()
    entries.sort(key=lambda e: e.get("date") or "", reverse=True)

    def _summary(subset):
        graded = [e for e in subset if e.get("graded") and e.get("correct") is not None]
        wins = sum(1 for e in graded if e["correct"])
        return {"total_logged": len(subset), "graded": len(graded), "correct": wins,
                "accuracy": round(wins / len(graded), 4) if graded else None}

    return {
        "entries": entries,
        "summary": {
            "overall": _summary(entries),
            "soccer": _summary([e for e in entries if e["sport"] == "soccer"]),
            "basketball": _summary([e for e in entries if e["sport"] == "basketball"]),
        },
    }
