"""
Prediction tracking log: records every market the model predicted for a live
fixture, then grades each one against the real final score once the match has
actually been played. Nothing here is simulated — grading only ever happens
against a real result fetched from the provider that supplied the fixture.

Persisted to data/prediction_log.json. Single-file, single-process; fine at
this app's scale, but it lives on local disk, so a platform deploy without a
persistent volume loses it on redeploy.

Grading design (rewritten — the previous version never graded anything):

  * Results are fetched **in bulk**, one request per competition per date
    window, and matched back to logged entries by provider id. The old code
    issued one HTTP request per logged prediction against an API that allows
    ten requests a minute, so it was permanently rate-limited.

  * An entry's `attempts` counter is only incremented when a provider
    actually answered and the match was not in the finished set. Failing to
    *ask* — because the rate budget was spent — is not the fixture's fault
    and must not push it toward being written off as unresolved.

  * The log is saved whenever anything changed, including a bare attempt
    increment. The old code only saved when a grade succeeded, so failed
    attempts were discarded and every entry sat at `attempts: 0` forever,
    never grading and never ageing out.

  * Grading is throttled to `GRADE_INTERVAL_SECONDS` and never blocks on the
    rate budget, so `/api/tracking/log` returns promptly whether or not the
    quota happens to be free.
"""
from __future__ import annotations

import json
import sys
import threading
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config
import football_data as fd
import http_budget

LOG_PATH = config.DATA_DIR / "prediction_log.json"

GRADE_AFTER_DAYS = 1        # don't look for a result until the day after kick-off
MAX_GRADE_ATTEMPTS = 8      # write off as "unresolved" after this many answered-but-absent passes
GRADE_INTERVAL_SECONDS = 300  # at most one grading sweep every 5 minutes
AFL_TOTAL_LINE = 165.5      # benchmark total for the AFL over/under market

_write_lock = threading.Lock()
_last_grade_at = 0.0


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
    tmp = LOG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    tmp.replace(LOG_PATH)


def _fixture_key(sport: str, div: str | None, ev_date: str | None, home: str, away: str) -> str:
    return f"{sport}:{div or ''}:{ev_date or ''}:{_normalize(home)}:{_normalize(away)}"


# ---------------------------------------------------------------- recording

def record_soccer(div: str, league: str, ev: dict, home_raw: str, away_raw: str,
                  prediction: dict) -> None:
    if "error" in prediction:
        return
    key = _fixture_key("soccer", div, ev.get("dateEvent"), home_raw, away_raw)
    with _write_lock:
        entries = _load()
        if any(e["key"] == key for e in entries):
            return
        probs_1x2 = {
            "H": prediction["prob_home_win"],
            "D": prediction["prob_draw"],
            "A": prediction["prob_away_win"],
        }
        entries.append({
            "key": key, "sport": "soccer", "div": div, "league": league,
            "date": ev.get("dateEvent"), "home": home_raw, "away": away_raw,
            "provider": ev.get("provider"), "provider_id": ev.get("providerId"),
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "markets": {
                "1x2": {"pick": max(probs_1x2, key=probs_1x2.get), "probs": probs_1x2},
                "btts": {
                    "pick": "YES" if prediction["prob_btts_yes"] >= 0.5 else "NO",
                    "probs": {"YES": prediction["prob_btts_yes"], "NO": prediction["prob_btts_no"]},
                },
                "over_under_2_5": {
                    "pick": "OVER" if prediction["prob_over_2_5"] >= 0.5 else "UNDER",
                    "probs": {"OVER": prediction["prob_over_2_5"], "UNDER": prediction["prob_under_2_5"]},
                },
            },
            "graded": False, "attempts": 0, "graded_at": None,
            "actual_home_score": None, "actual_away_score": None,
        })
        _save(entries)


def record_afl(ev: dict, home_raw: str, away_raw: str, prediction: dict) -> None:
    if "error" in prediction:
        return
    key = _fixture_key("afl", None, ev.get("dateEvent"), home_raw, away_raw)
    with _write_lock:
        entries = _load()
        if any(e["key"] == key for e in entries):
            return
        h2h = {"HOME": prediction["prob_home_win"], "AWAY": prediction["prob_away_win"]}
        entries.append({
            "key": key, "sport": "afl", "div": None, "league": "AFL",
            "date": ev.get("dateEvent"), "home": home_raw, "away": away_raw,
            "provider": ev.get("provider"), "provider_id": ev.get("providerId"),
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "markets": {
                "h2h": {"pick": max(h2h, key=h2h.get), "probs": h2h},
                "total_points": {
                    "pick": "OVER" if prediction["prob_over_total"] >= 0.5 else "UNDER",
                    "line": AFL_TOTAL_LINE,
                    "probs": {"OVER": prediction["prob_over_total"],
                              "UNDER": prediction["prob_under_total"]},
                },
            },
            "predicted_margin_home": prediction.get("predicted_margin_home"),
            "predicted_total_points": prediction.get("predicted_total_points"),
            "graded": False, "attempts": 0, "graded_at": None,
            "actual_home_score": None, "actual_away_score": None,
        })
        _save(entries)


# ----------------------------------------------------------------- grading

def _grade_soccer(entry: dict, hs: int, as_: int) -> None:
    actual_1x2 = "H" if hs > as_ else ("A" if as_ > hs else "D")
    m = entry["markets"]
    m["1x2"]["actual"] = actual_1x2
    m["1x2"]["correct"] = actual_1x2 == m["1x2"]["pick"]
    actual_btts = "YES" if (hs > 0 and as_ > 0) else "NO"
    m["btts"]["actual"] = actual_btts
    m["btts"]["correct"] = actual_btts == m["btts"]["pick"]
    actual_ou = "OVER" if (hs + as_) > 2.5 else "UNDER"
    m["over_under_2_5"]["actual"] = actual_ou
    m["over_under_2_5"]["correct"] = actual_ou == m["over_under_2_5"]["pick"]


def _grade_afl(entry: dict, hs: int, as_: int) -> None:
    m = entry["markets"]
    # A drawn AFL game settles head-to-head as neither side winning; recording
    # it as a loss for the pick would overstate the model's error, and as a win
    # would flatter it, so it is graded DRAW and counted as incorrect only
    # against an explicit HOME/AWAY call.
    actual_h2h = "HOME" if hs > as_ else ("AWAY" if as_ > hs else "DRAW")
    m["h2h"]["actual"] = actual_h2h
    m["h2h"]["correct"] = actual_h2h == m["h2h"]["pick"]
    line = m["total_points"].get("line", AFL_TOTAL_LINE)
    actual_total = "OVER" if (hs + as_) > line else "UNDER"
    m["total_points"]["actual"] = actual_total
    m["total_points"]["correct"] = actual_total == m["total_points"]["pick"]
    entry["actual_margin_home"] = hs - as_
    entry["actual_total_points"] = hs + as_


def _apply_result(entry: dict, hs: int, as_: int) -> None:
    entry["actual_home_score"] = hs
    entry["actual_away_score"] = as_
    if entry["sport"] == "soccer":
        _grade_soccer(entry, hs, as_)
    elif entry["sport"] == "afl":
        _grade_afl(entry, hs, as_)
    entry["graded"] = True
    entry["graded_at"] = datetime.now(timezone.utc).isoformat()


def _pending(entries: list[dict]) -> list[dict]:
    today = date.today()
    out = []
    for e in entries:
        if e.get("graded"):
            continue
        raw = e.get("date")
        if not raw:
            continue
        try:
            when = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            continue
        if (today - when).days < GRADE_AFTER_DAYS:
            continue
        out.append(e)
    return out


def grade_pending(force: bool = False) -> dict:
    """Runs one grading sweep. Returns a small report so the API can surface
    what actually happened instead of silently doing nothing."""
    global _last_grade_at
    import time as _time

    now = _time.monotonic()
    if not force and (now - _last_grade_at) < GRADE_INTERVAL_SECONDS:
        return {"ran": False, "reason": "throttled"}
    _last_grade_at = now

    with _write_lock:
        entries = _load()
        pending = _pending(entries)
        if not pending:
            return {"ran": True, "pending": 0, "graded": 0, "queried": []}

        graded = 0
        queried: list[str] = []
        changed = False

        # --- soccer, one bulk query per division ---------------------------
        by_div: dict[str, list[dict]] = {}
        for e in pending:
            if e["sport"] == "soccer" and fd.covers(e.get("div") or ""):
                by_div.setdefault(e["div"], []).append(e)

        for div, group in by_div.items():
            dates = sorted(d for d in (g.get("date") for g in group) if d)
            if not dates:
                continue
            res = fd.fetch_finished_results(div, dates[0], dates[-1], max_wait=0.0)
            if not res.ok:
                # Budget spent or provider down: we never asked about these
                # fixtures, so their attempt counters stay untouched.
                queried.append(f"{div}: {res.error or 'unavailable'}")
                continue
            queried.append(f"{div}: {len(res.data)} finished ({res.status})")
            for e in group:
                hit = res.data.get(str(e.get("provider_id")))
                if hit:
                    _apply_result(e, hit["home_score"], hit["away_score"])
                    graded += 1
                else:
                    e["attempts"] = e.get("attempts", 0) + 1
                changed = True

        # --- AFL, one bulk query per season --------------------------------
        afl_pending = [e for e in pending if e["sport"] == "afl"]
        if afl_pending:
            import afl_data
            years = {e["date"][:4] for e in afl_pending if e.get("date")}
            for year in sorted(years):
                res = afl_data.fetch_results(int(year), max_wait=0.0)
                if not res.ok:
                    queried.append(f"AFL {year}: {res.error or 'unavailable'}")
                    continue
                queried.append(f"AFL {year}: {len(res.data)} finished ({res.status})")
                for e in afl_pending:
                    if not (e.get("date") or "").startswith(year):
                        continue
                    hit = res.data.get(str(e.get("provider_id")))
                    if hit:
                        _apply_result(e, hit["home_score"], hit["away_score"])
                        graded += 1
                    else:
                        e["attempts"] = e.get("attempts", 0) + 1
                    changed = True

        # --- write off anything that has been asked about too many times ---
        for e in pending:
            if not e.get("graded") and e.get("attempts", 0) >= MAX_GRADE_ATTEMPTS:
                e["graded"] = True          # resolved-as-unresolvable
                e["graded_at"] = datetime.now(timezone.utc).isoformat()
                changed = True

        # Save on ANY change, including a bare attempt increment. Not doing
        # this is what froze the entire log at attempts=0.
        if changed:
            _save(entries)

        return {"ran": True, "pending": len(pending), "graded": graded, "queried": queried}


# -------------------------------------------------------------- reporting

def _market_summary(subset: list[dict], market: str) -> dict:
    graded = [e for e in subset if "actual" in (e.get("markets") or {}).get(market, {})]
    wins = sum(1 for e in graded if e["markets"][market].get("correct"))
    return {"graded": len(graded), "correct": wins,
            "accuracy": round(wins / len(graded), 4) if graded else None}


def _mae(subset: list[dict], pred_key: str, actual_key: str) -> float | None:
    pairs = [(e[pred_key], e[actual_key]) for e in subset
             if e.get(pred_key) is not None and e.get(actual_key) is not None]
    if not pairs:
        return None
    return round(sum(abs(p - a) for p, a in pairs) / len(pairs), 2)


def get_log(grade: bool = True) -> dict:
    """Returns the full log plus per-market accuracy. Runs a (throttled,
    non-blocking) grading sweep first unless told not to."""
    report = grade_pending() if grade else {"ran": False, "reason": "skipped"}
    entries = _load()
    entries.sort(key=lambda e: e.get("date") or "", reverse=True)

    soccer = [e for e in entries if e["sport"] == "soccer"]
    afl = [e for e in entries if e["sport"] == "afl"]
    pending = _pending(entries)

    return {
        "entries": entries,
        "summary": {
            "total_logged": len(entries),
            "awaiting_result": len(pending),
            "soccer_1x2": _market_summary(soccer, "1x2"),
            "soccer_btts": _market_summary(soccer, "btts"),
            "soccer_over_under": _market_summary(soccer, "over_under_2_5"),
            "afl_h2h": _market_summary(afl, "h2h"),
            "afl_total": _market_summary(afl, "total_points"),
            "afl_margin_mae": _mae(afl, "predicted_margin_home", "actual_margin_home"),
        },
        "grading": report,
    }
