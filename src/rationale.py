"""
"Why this prediction" narratives, built strictly from data this system
actually has: Elo ratings, the Dixon-Coles goal model, and real historical
match results (head-to-head + recent form) from data/*/processed.parquet.

Deliberately does NOT include injuries, suspensions, transfers/signings, or
any other news-derived factor — this build has no injury/transfer news feed
wired in (no such API is integrated, and the sandbox this was built in had
no network access to one either), so inventing that kind of commentary
would mean presenting fabricated information as fact in a tool whose whole
purpose is decision support. Every sentence this module produces traces
back to a real row in the historical dataset or a real model output.
"""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config

_SCOPE_TAIL = (
    " and actual historical results/head-to-head in our dataset. Does not "
    "include injuries, suspensions, or roster/transfer news: no such feed is "
    "integrated in this build, so none of that is guessed at here."
)
# Kept per-sport because the models genuinely differ — quoting the goal model
# on an AFL card would be describing something that never ran.
DATA_SCOPE_NOTE = "Built from Elo ratings, the Dixon-Coles goal model," + _SCOPE_TAIL
DATA_SCOPE_NOTE_AFL = (
    "Built from Elo ratings, a gradient-boosted margin/total model, and each "
    "side's recent record at this venue," + _SCOPE_TAIL
)


@lru_cache(maxsize=1)
def _soccer_by_div() -> dict:
    df = pd.read_parquet(config.SOCCER_PROCESSED)
    return {div: sub.sort_values("date", ascending=False).reset_index(drop=True)
            for div, sub in df.groupby("div")}


@lru_cache(maxsize=1)
def _afl_sorted() -> pd.DataFrame:
    df = pd.read_parquet(config.AFL_PROCESSED)
    return df.sort_values("date", ascending=False).reset_index(drop=True)


def _fmt_date(d) -> str:
    try:
        return pd.Timestamp(d).strftime("%Y-%m-%d")
    except Exception:
        return str(d)


# ---------------- Soccer ----------------

def soccer_h2h(div: str, home: str, away: str, n: int = 5) -> dict:
    df = _soccer_by_div().get(div)
    if df is None or df.empty:
        return {"meetings": [], "home_wins": 0, "draws": 0, "away_wins": 0}
    mask = ((df["HomeTeam"] == home) & (df["AwayTeam"] == away)) | \
           ((df["HomeTeam"] == away) & (df["AwayTeam"] == home))
    subset = df[mask].head(n)
    meetings, home_wins, draws, away_wins = [], 0, 0, 0
    for _, row in subset.iterrows():
        winner = None
        if pd.notna(row["FTHome"]) and pd.notna(row["FTAway"]):
            if row["FTHome"] > row["FTAway"]:
                winner = row["HomeTeam"]
            elif row["FTAway"] > row["FTHome"]:
                winner = row["AwayTeam"]
        if winner == home:
            home_wins += 1
        elif winner == away:
            away_wins += 1
        elif winner is None:
            pass
        else:
            draws += 1
        meetings.append({
            "date": _fmt_date(row["date"]),
            "home_team": row["HomeTeam"],
            "away_team": row["AwayTeam"],
            "score": f"{int(row['FTHome'])}-{int(row['FTAway'])}" if pd.notna(row["FTHome"]) else None,
        })
    return {"meetings": meetings, "home_wins": home_wins, "draws": draws, "away_wins": away_wins}


def soccer_form(div: str, team: str, n: int = 5) -> dict:
    df = _soccer_by_div().get(div)
    if df is None or df.empty:
        return {"results": "", "wins": 0, "draws": 0, "losses": 0, "goals_for": 0, "goals_against": 0}
    mask = (df["HomeTeam"] == team) | (df["AwayTeam"] == team)
    subset = df[mask].head(n)
    letters, wins, draws, losses, gf, ga = [], 0, 0, 0, 0, 0
    for _, row in subset.iterrows():
        if pd.isna(row["FTHome"]) or pd.isna(row["FTAway"]):
            continue
        is_home = row["HomeTeam"] == team
        for_goals = row["FTHome"] if is_home else row["FTAway"]
        against_goals = row["FTAway"] if is_home else row["FTHome"]
        gf += for_goals
        ga += against_goals
        if for_goals > against_goals:
            letters.append("W"); wins += 1
        elif for_goals < against_goals:
            letters.append("L"); losses += 1
        else:
            letters.append("D"); draws += 1
    return {"results": "-".join(letters), "wins": wins, "draws": draws, "losses": losses,
            "goals_for": int(gf), "goals_against": int(ga)}


def build_soccer_rationale(div: str, home: str, away: str, prediction: dict) -> dict:
    elo_home, elo_away = prediction["elo_home"], prediction["elo_away"]
    gap = elo_home - elo_away
    home_form = soccer_form(div, home)
    away_form = soccer_form(div, away)
    h2h = soccer_h2h(div, home, away)

    bullets = []

    if abs(gap) >= 50:
        stronger, weaker, g = (home, away, gap) if gap > 0 else (away, home, -gap)
        bullets.append(f"{stronger} rated {g:.0f} Elo points higher than {weaker} ({round(elo_home)} vs {round(elo_away)}) — a clear strength edge by rating.")
    else:
        bullets.append(f"Elo ratings are close ({round(elo_home)} vs {round(elo_away)}) — a fairly even matchup by team strength.")

    if home_form["results"]:
        bullets.append(f"{home} — last {len(home_form['results'].split('-'))} results: {home_form['results']} "
                        f"({home_form['wins']}W-{home_form['draws']}D-{home_form['losses']}L, "
                        f"{home_form['goals_for']} scored / {home_form['goals_against']} conceded).")
    if away_form["results"]:
        bullets.append(f"{away} — last {len(away_form['results'].split('-'))} results: {away_form['results']} "
                        f"({away_form['wins']}W-{away_form['draws']}D-{away_form['losses']}L, "
                        f"{away_form['goals_for']} scored / {away_form['goals_against']} conceded).")

    if h2h["meetings"]:
        total = h2h["home_wins"] + h2h["draws"] + h2h["away_wins"]
        last = h2h["meetings"][0]
        bullets.append(f"Head-to-head, last {total} meeting{'s' if total != 1 else ''}: "
                        f"{home} won {h2h['home_wins']}, drawn {h2h['draws']}, {away} won {h2h['away_wins']} "
                        f"— most recent was {last['home_team']} {last['score']} {last['away_team']} on {last['date']}.")
    else:
        bullets.append(f"No previous {home} vs {away} meetings found in our historical data — "
                        f"likely a first meeting in this dataset, a newly promoted side, or a team-name mismatch.")

    lam, mu = prediction["expected_goals_home"], prediction["expected_goals_away"]
    ou_lean = "over" if prediction["prob_over_2_5"] > 0.5 else "under"
    ou_pct = max(prediction["prob_over_2_5"], prediction["prob_under_2_5"])
    bullets.append(f"Model projects an average scoreline near {lam:.1f}-{mu:.1f}, leaning {ou_lean} 2.5 goals ({ou_pct*100:.0f}%).")

    return {
        "elo_gap": round(gap, 1),
        "home_form": home_form,
        "away_form": away_form,
        "h2h": h2h,
        "narrative": bullets,
        "data_scope": DATA_SCOPE_NOTE,
    }



# ---------------- AFL ----------------

def afl_h2h(home: str, away: str, n: int = 5) -> dict:
    df = _afl_sorted()
    mask = ((df["home_name"] == home) & (df["away_name"] == away)) | \
           ((df["home_name"] == away) & (df["away_name"] == home))
    subset = df[mask].head(n)
    meetings, home_wins, away_wins, draws = [], 0, 0, 0
    for _, row in subset.iterrows():
        margin = row["home_score"] - row["away_score"]
        if margin == 0:
            draws += 1
            winner = None
        else:
            winner = row["home_name"] if margin > 0 else row["away_name"]
        if winner == home:
            home_wins += 1
        elif winner == away:
            away_wins += 1
        meetings.append({
            "date": _fmt_date(row["date"]),
            "home_team": row["home_name"],
            "away_team": row["away_name"],
            "score": f"{int(row['home_score'])}-{int(row['away_score'])}",
            "venue": row["venue"],
        })
    return {"meetings": meetings, "home_wins": home_wins,
            "away_wins": away_wins, "draws": draws}


def afl_form(team: str, n: int = 5) -> dict:
    df = _afl_sorted()
    mask = (df["home_name"] == team) | (df["away_name"] == team)
    subset = df[mask].head(n)
    letters, wins, losses, draws, point_diff = [], 0, 0, 0, 0
    for _, row in subset.iterrows():
        is_home = row["home_name"] == team
        diff = (row["home_score"] - row["away_score"]) * (1 if is_home else -1)
        point_diff += diff
        if diff > 0:
            letters.append("W"); wins += 1
        elif diff < 0:
            letters.append("L"); losses += 1
        else:
            letters.append("D"); draws += 1
    return {"results": "-".join(letters), "wins": wins, "losses": losses, "draws": draws,
            "avg_point_diff": round(point_diff / len(letters), 1) if letters else 0.0}


def afl_venue_record(team: str, venue: str, n: int = 8) -> dict:
    """How the side has actually gone at this specific ground recently. In
    the AFL this carries real signal beyond generic home advantage, because
    interstate visitors and clubs relocated to a 'home' ground they rarely
    use are materially worse off than the fixture label suggests."""
    if not venue:
        return {"played": 0}
    df = _afl_sorted()
    mask = ((df["home_name"] == team) | (df["away_name"] == team)) & (df["venue"] == venue)
    subset = df[mask].head(n)
    if subset.empty:
        return {"played": 0}
    wins, diff = 0, 0
    for _, row in subset.iterrows():
        is_home = row["home_name"] == team
        margin = (row["home_score"] - row["away_score"]) * (1 if is_home else -1)
        diff += margin
        if margin > 0:
            wins += 1
    return {"played": len(subset), "wins": wins,
            "avg_margin": round(diff / len(subset), 1)}


def build_afl_rationale(home: str, away: str, prediction: dict) -> dict:
    elo_home, elo_away = prediction["elo_home"], prediction["elo_away"]
    gap = elo_home - elo_away
    venue = prediction.get("venue")
    home_form, away_form = afl_form(home), afl_form(away)
    h2h = afl_h2h(home, away)
    home_venue = afl_venue_record(home, venue)
    away_venue = afl_venue_record(away, venue)

    bullets = []

    if abs(gap) >= 40:
        stronger, weaker, g = (home, away, gap) if gap > 0 else (away, home, -gap)
        bullets.append(f"{stronger} rated {g:.0f} Elo points above {weaker} "
                       f"({round(elo_home)} vs {round(elo_away)}), a clear edge on rating.")
    else:
        bullets.append(f"Elo ratings are close ({round(elo_home)} vs {round(elo_away)}), "
                       f"an evenly matched game on team strength.")

    for team, form in ((home, home_form), (away, away_form)):
        if form["results"]:
            record = f"{form['wins']}W-{form['losses']}L"
            if form["draws"]:
                record += f"-{form['draws']}D"
            bullets.append(f"{team}: last {len(form['results'].split('-'))} results "
                           f"{form['results']} ({record}, average margin "
                           f"{form['avg_point_diff']:+.1f}).")

    # The venue line is the AFL-specific one, and only worth printing when
    # there is a real disparity in how familiar the ground is.
    if venue:
        h_exp = prediction.get("home_venue_experience", 0)
        a_exp = prediction.get("away_venue_experience", 0)
        if home_venue["played"] and away_venue["played"]:
            bullets.append(
                f"At {venue}: {home} {home_venue['wins']}/{home_venue['played']} recent wins "
                f"(avg {home_venue['avg_margin']:+.1f}), {away} {away_venue['wins']}/"
                f"{away_venue['played']} (avg {away_venue['avg_margin']:+.1f}).")
        elif h_exp or a_exp:
            bullets.append(
                f"{home} has played {h_exp:.0f} game(s) at {venue} in the last three years "
                f"against {away}'s {a_exp:.0f}, so this is "
                f"{'familiar ground for the home side' if h_exp > a_exp + 2 else 'not a strong venue edge either way'}.")

    if h2h["meetings"]:
        total = h2h["home_wins"] + h2h["away_wins"] + h2h["draws"]
        last = h2h["meetings"][0]
        drawn = f", {h2h['draws']} drawn" if h2h["draws"] else ""
        bullets.append(f"Head-to-head, last {total} meeting{'s' if total != 1 else ''}: "
                       f"{home} {h2h['home_wins']}, {away} {h2h['away_wins']}{drawn}. "
                       f"Most recent: {last['home_team']} {last['score']} {last['away_team']} "
                       f"on {last['date']}.")
    else:
        bullets.append(f"No previous {home} v {away} meetings in the dataset.")

    margin = prediction["predicted_margin_home"]
    total_pts = prediction["predicted_total_points"]
    favored = home if margin > 0 else away
    bullets.append(f"Model projects {favored} by {abs(margin):.0f} points, "
                   f"with a combined total near {total_pts:.0f} "
                   f"(line {prediction.get('total_line', 169.5)}).")

    return {
        "elo_gap": round(gap, 1),
        "home_form": home_form,
        "away_form": away_form,
        "h2h": h2h,
        "home_venue_record": home_venue,
        "away_venue_record": away_venue,
        "narrative": bullets,
        "data_scope": DATA_SCOPE_NOTE_AFL,
    }
