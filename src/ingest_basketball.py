"""
Ingest NBA game data (schedule + final scores), source ESPN via the
sportsdataverse "hoopR-nba-raw" open data mirror on GitHub. Free, no API
key, ~32k games 2002-present, updated regularly by the sportsdataverse
project.

Usage:
    python3 src/ingest_basketball.py            # use cached raw parquet
    python3 src/ingest_basketball.py --refresh   # re-download first
"""
import argparse
import sys
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config

SOURCE_URL = (
    "https://raw.githubusercontent.com/sportsdataverse/hoopR-nba-raw/"
    "main/nba/nba_schedule_master.parquet"
)


def download_raw(force: bool = False) -> Path:
    if config.BASKETBALL_RAW.exists() and not force:
        return config.BASKETBALL_RAW
    print(f"Downloading NBA schedule data from {SOURCE_URL} ...")
    config.BASKETBALL_RAW.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(SOURCE_URL, config.BASKETBALL_RAW)
    print(f"Saved to {config.BASKETBALL_RAW}")
    return config.BASKETBALL_RAW


def build_processed(raw_path: Path) -> pd.DataFrame:
    df = pd.read_parquet(raw_path)
    # season_type: 1=preseason 2=regular 3=playoffs (5 = play-in, rare code)
    df = df[df["season_type"].isin([2, 3, 5])].copy()
    df = df[df["status_type_completed"] == True]  # noqa: E712 - only finished games
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    df = df.dropna(subset=["home_score", "away_score", "home_name", "away_name"])
    df["home_score"] = df["home_score"].astype(float)
    df["away_score"] = df["away_score"].astype(float)
    df["margin"] = df["home_score"] - df["away_score"]
    df["total"] = df["home_score"] + df["away_score"]
    df["home_win"] = (df["margin"] > 0).astype(int)
    df["is_playoffs"] = (df["season_type"] == 3).astype(int)
    df = df.sort_values("date").reset_index(drop=True)

    # rest days per team (proxy for fatigue / back-to-backs, a real edge
    # factor that simple public power rankings under-weight)
    df["home_rest_days"] = np.nan
    df["away_rest_days"] = np.nan
    last_played = {}
    home_rest, away_rest = [], []
    for row in df.itertuples(index=False):
        h, a, d = row.home_name, row.away_name, row.date
        home_rest.append((d - last_played[h]).days if h in last_played else np.nan)
        away_rest.append((d - last_played[a]).days if a in last_played else np.nan)
        last_played[h] = d
        last_played[a] = d
    df["home_rest_days"] = home_rest
    df["away_rest_days"] = away_rest

    keep = [
        "date", "season", "season_type", "is_playoffs", "game_id",
        "home_name", "away_name", "home_score", "away_score",
        "margin", "total", "home_win", "neutral_site",
        "home_rest_days", "away_rest_days",
    ]
    return df[keep]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="re-download raw parquet")
    args = ap.parse_args()

    raw_path = download_raw(force=args.refresh)
    df = build_processed(raw_path)
    df.to_parquet(config.BASKETBALL_PROCESSED, index=False)
    print(f"Processed {len(df):,} games -> {config.BASKETBALL_PROCESSED}")
    print(f"Date range: {df['date'].min().date()} .. {df['date'].max().date()}")
    print(df["season"].value_counts().sort_index().tail(5))


if __name__ == "__main__":
    main()
