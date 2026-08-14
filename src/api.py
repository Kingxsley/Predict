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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from typing import Optional

import config
import predictor as pred
import fixtures as live_fixtures

app = FastAPI(title="Local Sports Prediction Engine", version="1.0")


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


@app.get("/", response_class=HTMLResponse)
def dashboard():
    html_path = Path(__file__).resolve().parent.parent / "dashboard.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


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


@app.get("/healthz")
def healthz():
    """Health check for platform deploys (Railway etc.)."""
    return {"status": "ok"}


if __name__ == "__main__":
    import os
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("api:app", host="0.0.0.0", port=port, reload=False)
