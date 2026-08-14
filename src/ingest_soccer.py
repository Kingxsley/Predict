"""
Ingest soccer match data (results, stats, closing odds, ClubElo snapshots).

Source: football-data.co.uk historical results/odds, mirrored + merged with
ClubElo pre-match ratings by the open "Club-Football-Match-Data-2000-2025"
dataset on GitHub. Free, no API key, ~230k matches across 38 divisions,
2000-2025.

Usage:
    python3 src/ingest_soccer.py            # use cached raw CSV
    python3 src/ingest_soccer.py --refresh   # re-download raw CSV first
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
    "https://raw.githubusercontent.com/xgabora/"
    "Club-Football-Match-Data-2000-2025/main/data/Matches.csv"
)


def download_raw(force: bool = False) -> Path:
    if config.SOCCER_RAW.exists() and not force:
        return config.SOCCER_RAW
    print(f"Downloading soccer match data from {SOURCE_URL} ...")
    config.SOCCER_RAW.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(SOURCE_URL, config.SOCCER_RAW)
    print(f"Saved to {config.SOCCER_RAW}")
    return config.SOCCER_RAW


def implied_prob_1x2(odd_h, odd_d, odd_a):
    """De-vig (normalize) 1X2 decimal odds into fair probabilities."""
    with np.errstate(divide="ignore", invalid="ignore"):
        ih, idd, ia = 1.0 / odd_h, 1.0 / odd_d, 1.0 / odd_a
    overround = ih + idd + ia
    return ih / overround, idd / overround, ia / overround, overround


def build_processed(raw_path: Path) -> pd.DataFrame:
    df = pd.read_csv(raw_path, low_memory=False)
    df["MatchDate"] = pd.to_datetime(df["MatchDate"], errors="coerce")
    df = df.dropna(subset=["MatchDate", "HomeTeam", "AwayTeam", "FTHome", "FTAway"])
    df = df.sort_values("MatchDate").reset_index(drop=True)

    df["FTHome"] = df["FTHome"].astype(float)
    df["FTAway"] = df["FTAway"].astype(float)
    df["result"] = np.select(
        [df["FTHome"] > df["FTAway"], df["FTHome"] == df["FTAway"]],
        ["H", "D"],
        default="A",
    )
    df["league"] = df["Division"].map(config.SOCCER_LEAGUES).fillna(df["Division"])

    # De-vig market odds -> fair probabilities, used for calibration &
    # closing-line-value backtesting. Prefer average close (Odd*) then fall
    # back to Max* if the average is missing.
    oh = df["OddHome"].where(df["OddHome"].notna(), df["MaxHome"])
    od = df["OddDraw"].where(df["OddDraw"].notna(), df["MaxDraw"])
    oa = df["OddAway"].where(df["OddAway"].notna(), df["MaxAway"])
    ph, pd_, pa, overround = implied_prob_1x2(oh, od, oa)
    df["mkt_prob_home"] = ph
    df["mkt_prob_draw"] = pd_
    df["mkt_prob_away"] = pa
    df["mkt_overround"] = overround
    df["odd_home"], df["odd_draw"], df["odd_away"] = oh, od, oa

    keep = [
        "MatchDate", "Division", "league", "HomeTeam", "AwayTeam",
        "FTHome", "FTAway", "result", "HomeElo", "AwayElo",
        "Form3Home", "Form5Home", "Form3Away", "Form5Away",
        "odd_home", "odd_draw", "odd_away",
        "mkt_prob_home", "mkt_prob_draw", "mkt_prob_away", "mkt_overround",
        "Over25", "Under25", "HandiSize", "HandiHome", "HandiAway",
    ]
    df = df[keep].rename(columns={"MatchDate": "date", "Division": "div"})
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="re-download raw CSV")
    args = ap.parse_args()

    raw_path = download_raw(force=args.refresh)
    df = build_processed(raw_path)
    df.to_parquet(config.SOCCER_PROCESSED, index=False)
    print(f"Processed {len(df):,} matches -> {config.SOCCER_PROCESSED}")
    print(df["league"].value_counts().head(10))
    print(f"Date range: {df['date'].min().date()} .. {df['date'].max().date()}")
    print(f"Matches with usable odds: {df['odd_home'].notna().sum():,}")


if __name__ == "__main__":
    main()
