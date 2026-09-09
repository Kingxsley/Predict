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
    GET /api/basketball/teams
    GET /api/basketball/predict?home=Celtics&away=Lakers&odds_home=1.55&odds_away=2.5
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from typing import Optional

import config
import predictor as pred
import fixtures as live_fixtures
import tracking
import accumulator as acca
import odds_provider as odds

app = FastAPI(title="Local Sports Prediction Engine", version="1.0")

# The dashboard is a plain static bundle (no build step): index.html plus one
# stylesheet and one ES module, served from web/ and mounted at /assets.
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")


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


@app.get("/api/basketball/teams")
def basketball_teams():
    return pred.list_basketball_teams()


@app.get("/api/basketball/predict")
def basketball_predict(
    home: str, away: str, game_date: Optional[str] = None,
    is_playoffs: bool = False, neutral_site: bool = False,
    odds_home: Optional[float] = None, odds_away: Optional[float] = None,
):
    try:
        return pred.predict_basketball(
            home, away, game_date, is_playoffs, neutral_site, odds_home, odds_away
        )
    except Exception as e:
        raise HTTPException(400, f"{type(e).__name__}: {e}")


@app.get("/")
def dashboard():
    return FileResponse(WEB_DIR / "index.html", media_type="text/html")


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
    ROI simulation for soccer; win-probability Brier/log-loss and margin/
    total MAE for NBA. See README "Backtest results" for methodology."""
    try:
        soccer = json.loads(config.REPORTS_DIR.joinpath("soccer_backtest.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        soccer = []
    try:
        basketball = json.loads(config.REPORTS_DIR.joinpath("basketball_backtest.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        basketball = None
    return {"soccer": soccer, "basketball": basketball}


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


@app.get("/api/odds/basketball")
def odds_basketball(home: str, away: str):
    if not odds.is_configured():
        return {"available": False, "reason": "No THE_ODDS_API_KEY configured on this server."}
    result = odds.get_basketball_odds(home, away)
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


@app.get("/healthz")
def healthz():
    """Health check for platform deploys (Railway etc.)."""
    return {"status": "ok"}


if __name__ == "__main__":
    import os
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("api:app", host="0.0.0.0", port=port, reload=False)
