"""
Local FastAPI server exposing the prediction engine + a small dashboard.

Run:
    python3 src/api.py
    # or: uvicorn api:app --app-dir src --host 0.0.0.0 --port 8000

Then open http://localhost:8000 in a browser, or hit the JSON endpoints
directly (e.g. for integrating into a sportsbook's own trading tools):

    GET /api/soccer/leagues
    GET /api/soccer/teams?div=E0
    GET /api/soccer/predict?div=E0&home=Arsenal&away=Chelsea&odds_home=1.9&odds_draw=3.6&odds_away=4.2
    GET /api/afl/teams
    GET /api/afl/predict?home=Geelong&away=Carlton&venue=M.C.G.
"""
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from typing import Optional

import config
import predictor as pred
import fixtures as live_fixtures
import tracking
import accumulator as acca
import odds_provider as odds
import store

app = FastAPI(title="Local Sports Prediction Engine", version="1.0")

# The dashboard is a plain static bundle (no build step): index.html plus one
# stylesheet and one ES module, served from web/ and mounted at /assets.
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")


def _asset_fingerprints() -> dict[str, str]:
    """Content hash per asset, computed once at import.

    Without this the stylesheet and script are served from fixed URLs with an
    ETag and no Cache-Control, which lets a browser apply heuristic freshness
    and hold an old copy for days without ever revalidating. That is not
    theoretical: a phone held a stylesheet from two deploys earlier while
    getting fresh HTML, so the wordmark rendered as an unstyled link and
    icons fell back to their intrinsic size — the site looked broken and
    every design change appeared not to have shipped at all.
    """
    out = {}
    for name in ("app.css", "app.js"):
        path = WEB_DIR / name
        try:
            out[name] = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        except OSError:
            out[name] = "dev"
    return out


ASSET_HASHES = _asset_fingerprints()


@app.middleware("http")
async def _asset_cache_headers(request, call_next):
    """Fingerprinted assets are immutable; bare ones must revalidate.

    The bare-URL branch is what repairs a browser that is already holding a
    stale copy. It matters less than it looks, because the fingerprinted URL
    is a different URL and therefore misses the stale entry outright — a
    stuck client is fixed by the next load of index.html, with no hard
    refresh asked of anyone.
    """
    response = await call_next(request)
    if request.url.path.startswith("/assets/"):
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if request.query_params.get("v")
            else "no-cache, must-revalidate")
    return response


@app.on_event("startup")
def _startup() -> None:
    """Pick the log backend once, and carry over anything an earlier
    file-backed deploy had already written."""
    backend = store.init()
    if backend == "postgres":
        moved = store.migrate_json_into_db()
        if moved:
            print(f"[store] imported {moved} entries from the on-disk log")
    print(f"[store] prediction log backend: {backend}")
    # Build the board behind the startup so the first visitor after a deploy
    # is not the one who pays for a cold cache.
    live_fixtures.warm_cache()


@app.get("/api/soccer/leagues")
def soccer_leagues():
    divs = pred.list_soccer_leagues()
    return [{"code": d, "name": config.SOCCER_LEAGUES.get(d, d)} for d in divs]


@app.get("/api/soccer/teams")
def soccer_teams(div: str = Query(...)):
    try:
        return pred.list_soccer_teams(div)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.get("/api/soccer/predict")
def soccer_predict(
    div: str, home: str, away: str,
    odds_home: Optional[float] = None, odds_draw: Optional[float] = None,
    odds_away: Optional[float] = None,
):
    try:
        return pred.predict_soccer(div, home, away, odds_home, odds_draw, odds_away)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(400, f"{type(e).__name__}: {e}")


@app.get("/api/afl/teams")
def afl_teams():
    try:
        return pred.list_afl_teams()
    except FileNotFoundError as e:
        raise HTTPException(503, str(e))


@app.get("/api/afl/venues")
def afl_venues():
    """Venues the model has recent history for. Venue is not cosmetic in the
    AFL: interstate travel and ground familiarity are real features, so the
    same fixture at a different ground genuinely prices differently."""
    try:
        return pred.afl_venues()
    except FileNotFoundError as e:
        raise HTTPException(503, str(e))


@app.get("/api/afl/predict")
def afl_predict(
    home: str, away: str, venue: Optional[str] = None,
    game_date: Optional[str] = None, is_final: bool = False, neutral: bool = False,
    odds_home: Optional[float] = None, odds_away: Optional[float] = None,
):
    try:
        return pred.predict_afl(home, away, venue, game_date, is_final, neutral,
                                 odds_home, odds_away)
    except FileNotFoundError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(400, f"{type(e).__name__}: {e}")


@app.get("/")
def dashboard():
    """The shell, with its asset URLs fingerprinted.

    index.html itself must never be cached — it is the only thing that knows
    which asset versions are current. The assets it points at are immutable
    by construction, because a change to either produces a different URL, so
    they can be cached for a year and a deploy is picked up on the next page
    load rather than whenever a browser happens to revalidate.
    """
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    for name, digest in ASSET_HASHES.items():
        html = html.replace(f"/assets/{name}", f"/assets/{name}?v={digest}")
    return Response(
        html, media_type="text/html",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/api/fixtures/live")
def fixtures_live(refresh: bool = False):
    """Upcoming fixtures grouped by league, each with a prediction already
    attached — this is what powers the dashboard's default view so nobody
    has to type team names in by hand. Cached ~30 min server-side; pass
    ?refresh=true to force a re-fetch (use sparingly, the upstream free
    tier is rate-limited)."""
    try:
        return live_fixtures.get_live_fixtures(force_refresh=refresh)
    except Exception as e:
        raise HTTPException(502, f"Live fixtures fetch failed: {type(e).__name__}: {e}")


@app.get("/api/analytics")
def analytics():
    """Real backtest results — unedited output of src/backtest.py via
    src/train.py, not live-computed. Per-league Brier score/log-loss
    (model vs. real historical closing-odds market) and a value-betting
    ROI simulation for soccer; win-probability Brier/log-loss plus margin and
    total MAE against held-out seasons for the AFL, each against a stated
    baseline. See README "Backtest results" for methodology."""
    try:
        soccer = json.loads(config.REPORTS_DIR.joinpath("soccer_backtest.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        soccer = []
    try:
        afl = json.loads(config.REPORTS_DIR.joinpath("afl_backtest.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        afl = None
    return {"soccer": soccer, "afl": afl}


@app.get("/api/tracking/log")
def tracking_log():
    """Every prediction logged from a live fixture, graded against the
    real final score (fetched from the same provider that supplied the
    fixture) once available. Nothing here is simulated - a fixture stays
    "pending" until its actual result can be fetched."""
    try:
        return tracking.get_log()
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}")


@app.get("/api/odds/soccer")
def odds_soccer(div: str, home: str, away: str):
    """Real best-available bookmaker odds (the-odds-api.com) for one
    fixture, if configured (THE_ODDS_API_KEY) and the fixture is on the
    current board. Returns null fields rather than an error when odds
    simply aren't available - this is a best-effort lookup, not required
    for a prediction to work."""
    if not odds.is_configured():
        return {"available": False, "reason": "No THE_ODDS_API_KEY configured on this server."}
    result = odds.get_soccer_odds(div, home, away)
    if result is None:
        return {"available": False, "reason": "Fixture not found on the current odds board for this league."}
    return {"available": True, **result}


@app.get("/api/odds/afl")
def odds_afl(home: str, away: str):
    if not odds.is_configured():
        return {"available": False, "reason": "No THE_ODDS_API_KEY configured on this server."}
    result = odds.get_afl_odds(home, away)
    if result is None:
        return {"available": False, "reason": "Fixture not found on the current odds board."}
    return {"available": True, **result}


@app.get("/api/accumulator")
def accumulator(
    legs: int = 3, min_odds: float = 5.0,
    date_from: Optional[str] = None, date_to: Optional[str] = None,
):
    """Picks `legs` different fixtures' most-confident market calls and
    returns the combination with the highest combined win probability that
    still clears `min_odds` combined. IMPORTANT: "odds" here are
    model-implied (1 / predicted probability), not a real bookmaker price -
    see src/accumulator.py's module docstring."""
    try:
        return acca.build_accumulator(num_legs=legs, min_combined_odds=min_odds,
                                       date_from=date_from, date_to=date_to)
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}")


@app.get("/api/status")
def status():
    """What the app can actually serve right now: which leagues have a live
    fixture feed, which are off-season, which have no free source at all, and
    how much of each provider's rate budget is left. Surfaced in the UI so a
    thin board is explained rather than just looking broken."""
    try:
        data = live_fixtures.get_live_fixtures()
        return {
            "coverage": data.get("coverage", []),
            "summary": data.get("summary", {}),
            "storage": store.backend(),
            "generated_at": data.get("generated_at"),
        }
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}")


@app.get("/healthz")
def healthz():
    """Health check for platform deploys (Railway etc.)."""
    return {"status": "ok"}


if __name__ == "__main__":
    import os
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("api:app", host="0.0.0.0", port=port, reload=False)
