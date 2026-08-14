"""
Prediction tracking log: records every market the model predicted for a
live fixture (1X2/moneyline, BTTS, over/under 2.5), then grades each one
against the real final score once the match has actually been played.
Nothing here is simulated — grading only happens against a real result
fetched from the same provider (football-data.org or TheSportsDB) that
supplied the fixture in the first place.

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

    x12_probs = {"H": prediction["prob_home_win"], "D": prediction["prob_draw"], "A": prediction["prob_away_win"]}
    btts_pick = "YES" if prediction["prob_btts_yes"] >= 0.5 else "NO"
    ou_pick = "OVER" if prediction["prob_over_2_5"] >= 0.5 else "UNDER"

    entries.append({
        "key": key, "sport": "soccer", "div": div, "league": league,
        "date": ev.get("dateEvent"), "home": home_raw, "away": away_raw,
        "provider": ev.get("provider"), "provider_id": ev.get("providerId"),
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "markets": {
            "1x2": {"pick": max(x12_probs, key=x12_probs.get), "probs": x12_probs},
            "btts": {"pick": btts_pick, "probs": {"YES": prediction["prob_btts_yes"], "NO": prediction["prob_btts_no"]}},
            "over_under_2_5": {"pick": ou_pick, "probs": {"OVER": prediction["prob_over_2_5"], "UNDER": prediction["prob_under_2_5"]}},
        },
        "graded": False, "attempts": 0, "graded_at": None,
        "actual_home_score": None, "actual_away_score": None,
    })
    _save(entries)


def record_basketball(ev: dict, home_raw: str, away_raw: str, prediction: dict) -> None:
    if "error" in prediction:
        return
    key = _fixture_key("basketball", None, ev.get("dateEvent"), home_raw, away_raw)
    entries = _load()
    if any(e["key"] == key for e in entries):
        return

    ml_probs = {"HOME": prediction["prob_home_win"], "AWAY": prediction["prob_away_win"]}
    entries.append({
        "key": key, "sport": "basketball", "div": None, "league": "NBA",
        "date": ev.get("dateEvent"), "home": home_raw, "away": away_raw,
        "provider": ev.get("provider"), "provider_id": ev.get("providerId"),
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "markets": {
            "moneyline": {"pick": max(ml_probs, key=ml_probs.get), "probs": ml_probs},
        },
        "predicted_margin_home": prediction.get("predicted_margin_home"),
        "predicted_total_points": prediction.get("predicted_total_points"),
        "graded": False, "attempts": 0, "graded_at": None,
        "actual_home_score": None, "actual_away_score": None,
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


def _grade_soccer_markets(entry: dict, home_score: int, away_score: int) -> None:
    if home_score > away_score:
        actual_1x2 = "H"
    elif away_score > home_score:
        actual_1x2 = "A"
    else:
        actual_1x2 = "D"
    actual_btts = "YES" if (home_score > 0 and away_score > 0) else "NO"
    actual_ou = "OVER" if (home_score + away_score) > 2.5 else "UNDER"

    m = entry["markets"]
    m["1x2"]["actual"] = actual_1x2
    m["1x2"]["correct"] = (actual_1x2 == m["1x2"]["pick"])
    m["btts"]["actual"] = actual_btts
    m["btts"]["correct"] = (actual_btts == m["btts"]["pick"])
    m["over_under_2_5"]["actual"] = actual_ou
    m["over_under_2_5"]["correct"] = (actual_ou == m["over_under_2_5"]["pick"])


def _grade_basketball_markets(entry: dict, home_score: int, away_score: int) -> None:
    actual_ml = "HOME" if home_score > away_score else "AWAY"
    m = entry["markets"]
    m["moneyline"]["actual"] = actual_ml
    m["moneyline"]["correct"] = (actual_ml == m["moneyline"]["pick"])


def grade_pending() -> None:
    """Attempts to fetch a real final score for every ungraded entry whose
    fixture date has passed, and grades each of its markets independently
    against that real score. Entries with no provider id, or that fail
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
            entry["actual_home_score"] = home_score
            entry["actual_away_score"] = away_score
            if entry["sport"] == "soccer":
                _grade_soccer_markets(entry, home_score, away_score)
            else:
                _grade_basketball_markets(entry, home_score, away_score)
            entry["graded"] = True
            entry["graded_at"] = datetime.now(timezone.utc).isoformat()
            changed = True
        elif entry["attempts"] >= MAX_GRADE_ATTEMPTS:
            entry["graded"] = True  # unresolved - no result ever available, markets stay ungraded (no "actual"/"correct")
            entry["graded_at"] = datetime.now(timezone.utc).isoformat()
            changed = True

    if changed:
        _save(entries)


def get_log() -> dict:
    """Grades whatever can be graded right now, then returns the full log
    plus a per-market accuracy summary, most recent fixture first."""
    grade_pending()
    entries = _load()
    entries.sort(key=lambda e: e.get("date") or "", reverse=True)

    def _market_summary(subset, market):
        graded = [e for e in subset if "actual" in e["markets"].get(market, {})]
        wins = sum(1 for e in graded if e["markets"][market]["correct"])
        return {"graded": len(graded), "correct": wins,
                "accuracy": round(wins / len(graded), 4) if graded else None}

    soccer = [e for e in entries if e["sport"] == "soccer"]
    basketball = [e for e in entries if e["sport"] == "basketball"]

    return {
        "entries": entries,
        "summary": {
            "total_logged": len(entries),
            "soccer_1x2": _market_summary(soccer, "1x2"),
            "soccer_btts": _market_summary(soccer, "btts"),
            "soccer_over_under": _market_summary(soccer, "over_under_2_5"),
            "basketball_moneyline": _market_summary(basketball, "moneyline"),
        },
    }
