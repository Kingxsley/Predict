#!/usr/bin/env python3
"""
Command-line predictor — no server needed, good for scripting/cron jobs
that feed a trading desk or a nightly report.

Examples:
    python3 src/predict_cli.py soccer --div E0 --home Arsenal --away Chelsea \
        --odds-home 1.9 --odds-draw 3.6 --odds-away 4.2

    python3 src/predict_cli.py basketball --home Celtics --away Lakers \
        --odds-home 1.55 --odds-away 2.5

    python3 src/predict_cli.py leagues
    python3 src/predict_cli.py teams --div E0
    python3 src/predict_cli.py nba-teams
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import predictor as pred


def main():
    ap = argparse.ArgumentParser(description="Local sports prediction engine CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_soccer = sub.add_parser("soccer", help="Predict a soccer match")
    p_soccer.add_argument("--div", required=True, help="League code, e.g. E0 (see `leagues` command)")
    p_soccer.add_argument("--home", required=True)
    p_soccer.add_argument("--away", required=True)
    p_soccer.add_argument("--odds-home", type=float, default=None)
    p_soccer.add_argument("--odds-draw", type=float, default=None)
    p_soccer.add_argument("--odds-away", type=float, default=None)

    p_nba = sub.add_parser("basketball", help="Predict an NBA game")
    p_nba.add_argument("--home", required=True)
    p_nba.add_argument("--away", required=True)
    p_nba.add_argument("--date", default=None, help="YYYY-MM-DD, defaults to today")
    p_nba.add_argument("--playoffs", action="store_true")
    p_nba.add_argument("--neutral", action="store_true")
    p_nba.add_argument("--odds-home", type=float, default=None)
    p_nba.add_argument("--odds-away", type=float, default=None)

    sub.add_parser("leagues", help="List trained soccer leagues")
    p_teams = sub.add_parser("teams", help="List teams for a soccer league")
    p_teams.add_argument("--div", required=True)
    sub.add_parser("nba-teams", help="List NBA teams")

    args = ap.parse_args()

    if args.cmd == "leagues":
        for div in pred.list_soccer_leagues():
            print(f"{div}\t{__import__('config').SOCCER_LEAGUES.get(div, div)}")
        return
    if args.cmd == "teams":
        print("\n".join(pred.list_soccer_teams(args.div)))
        return
    if args.cmd == "nba-teams":
        print("\n".join(pred.list_basketball_teams()))
        return
    if args.cmd == "soccer":
        out = pred.predict_soccer(args.div, args.home, args.away,
                                   args.odds_home, args.odds_draw, args.odds_away)
    elif args.cmd == "basketball":
        out = pred.predict_basketball(args.home, args.away, args.date,
                                       args.playoffs, args.neutral,
                                       args.odds_home, args.odds_away)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
