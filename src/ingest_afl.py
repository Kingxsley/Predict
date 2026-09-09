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
    args = ap.parse_args()

    df = build(args.start, args.end)
    out = config.AFL_PROCESSED
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)

    seasons = df["season"].nunique()
    print(f"\nWrote {len(df):,} matches across {seasons} seasons to {out}")
    print(f"  date range   : {df['date'].min().date()} to {df['date'].max().date()}")
    print(f"  teams        : {df['home_name'].nunique()}")
    print(f"  venues       : {df['venue'].nunique()}")
    print(f"  home win rate: {df['home_win'].mean():.3f}  (draws: {df['draw'].sum()})")
    print(f"  mean margin  : {df['margin'].mean():+.2f}   mean total: {df['total_points'].mean():.1f}")


if __name__ == "__main__":
    main()
