"""
Builds the AFL training frame from the Squiggle API.

Run:
    python3 src/ingest_afl.py            # 2012 to current season
    python3 src/ingest_afl.py --from 2000

Writes data/afl/processed.parquet, one row per completed match, sorted by
date. Venue is kept because AFL home-ground advantage is unusually
venue-specific — interstate travel to Perth or Adelaide is a much bigger
penalty than a cross-town trip in Melbourne, where several clubs share the
same two grounds and a nominal "home" game may carry no real advantage.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import afl_data
import config


def build(start_year: int, end_year: int | None) -> pd.DataFrame:
    # ASCII only in console output: Windows consoles default to cp1252 and
    # raise UnicodeEncodeError on arrows/ellipses.
    print(f"Fetching AFL seasons {start_year}-{end_year or 'current'} from Squiggle...")
    rows = afl_data.fetch_history(start_year, end_year)
    if not rows:
        raise SystemExit("No AFL games returned — check network access to api.squiggle.com.au")

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date", "home_name", "away_name"])
    df = df[(df["home_score"] > 0) | (df["away_score"] > 0)]  # drop abandoned/void rows

    df["margin"] = df["home_score"] - df["away_score"]
    df["total_points"] = df["home_score"] + df["away_score"]
    df["home_win"] = (df["margin"] > 0).astype(int)
    df["draw"] = (df["margin"] == 0).astype(int)

    # A grand final is played at a neutral venue for both sides; other finals
    # are usually hosted by the higher-ranked team, so only the GF is flagged
    # neutral outright.
    df["neutral"] = df["is_grand_final"].astype(int)

    df = df.sort_values("date").reset_index(drop=True)
    return df


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", type=int, default=afl_data.HISTORY_START_YEAR)
    ap.add_argument("--to", dest="end", type=int, default=None)
    # Refreshing the committed snapshots is a routine chore — the fixture one
    # goes stale every round — and does not need the whole training frame
    # rebuilt from a decade of seasons to do it.
    ap.add_argument("--snapshots-only", action="store_true",
                    help="Refresh the committed fixture and results snapshots, "
                         "skipping the training-frame rebuild.")
    args = ap.parse_args()

    df = None
    if not args.snapshots_only:
        df = build(args.start, args.end)
        out = config.AFL_PROCESSED
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(out, index=False)

    # Capture the upcoming fixture too. Squiggle blocks datacenter IPs, so the
    # deployed instance cannot read this itself; committing what we fetch here
    # from a permitted network is what keeps the AFL board populated there.
    upcoming = afl_data.fetch_upcoming(max_wait=30)
    if upcoming.ok and upcoming.data:
        snap = afl_data.write_snapshot(upcoming.data)
        print(f"Captured {len(upcoming.data)} upcoming fixtures to {snap}")
    else:
        print(f"No upcoming fixtures to snapshot ({upcoming.error or 'none scheduled'})")

    # Final scores too, for the same reason. Grading has to reach Squiggle
    # as well, and from a blocked host it never could, so the AFL markets
    # sat pending for ever no matter how long they waited.
    end_year = args.end or datetime.now(timezone.utc).year
    by_year = {}
    for year in range(max(args.start, end_year - 1), end_year + 1):
        res = afl_data.fetch_results(year, max_wait=30)
        if res.ok and res.data:
            by_year[str(year)] = res.data
    if by_year:
        snap = afl_data.write_results_snapshot(by_year)
        total = sum(len(v) for v in by_year.values())
        print(f"Captured {total} final scores across {len(by_year)} season(s) to {snap}")
    else:
        print("No final scores to snapshot")

    if df is None:
        return

    seasons = df["season"].nunique()
    print(f"\nWrote {len(df):,} matches across {seasons} seasons to {out}")
    print(f"  date range   : {df['date'].min().date()} to {df['date'].max().date()}")
    print(f"  teams        : {df['home_name'].nunique()}")
    print(f"  venues       : {df['venue'].nunique()}")
    print(f"  home win rate: {df['home_win'].mean():.3f}  (draws: {df['draw'].sum()})")
    print(f"  mean margin  : {df['margin'].mean():+.2f}   mean total: {df['total_points'].mean():.1f}")


if __name__ == "__main__":
    main()
