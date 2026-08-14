"""Central configuration: paths and league metadata."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SOCCER_RAW = DATA_DIR / "soccer" / "raw" / "matches.csv"
SOCCER_PROCESSED = DATA_DIR / "soccer" / "processed.parquet"
BASKETBALL_RAW = DATA_DIR / "basketball" / "raw" / "nba_schedule_master.parquet"
BASKETBALL_PROCESSED = DATA_DIR / "basketball" / "processed.parquet"
MODELS_DIR = ROOT / "models"
REPORTS_DIR = ROOT / "reports"

for d in [MODELS_DIR, REPORTS_DIR, DATA_DIR / "soccer", DATA_DIR / "basketball"]:
    d.mkdir(parents=True, exist_ok=True)

# football-data.co.uk division codes -> human readable names + tier weight.
# Tier weight is used only for informational display, not model fitting.
SOCCER_LEAGUES = {
    "E0": "England - Premier League",
    "E1": "England - Championship",
    "E2": "England - League One",
    "E3": "England - League Two",
    "EC": "England - Conference",
    "SP1": "Spain - La Liga",
    "SP2": "Spain - Segunda Division",
    "I1": "Italy - Serie A",
    "I2": "Italy - Serie B",
    "D1": "Germany - Bundesliga",
    "D2": "Germany - Bundesliga 2",
    "F1": "France - Ligue 1",
    "F2": "France - Ligue 2",
    "N1": "Netherlands - Eredivisie",
    "P1": "Portugal - Primeira Liga",
    "B1": "Belgium - Pro League",
    "SC0": "Scotland - Premiership",
    "SC1": "Scotland - Championship",
    "SC2": "Scotland - League One",
    "SC3": "Scotland - League Two",
    "T1": "Turkey - Super Lig",
    "G1": "Greece - Super League",
    "ROM": "Romania - Liga I",
    "POL": "Poland - Ekstraklasa",
    "SWE": "Sweden - Allsvenskan",
    "NOR": "Norway - Eliteserien",
    "RUS": "Russia - Premier League",
    "DEN": "Denmark - Superliga",
    "IRL": "Ireland - Premier Division",
    "FIN": "Finland - Veikkausliiga",
    "AUT": "Austria - Bundesliga",
    "SUI": "Switzerland - Super League",
    "CHN": "China - Super League",
    "USA": "USA - MLS",
    "BRA": "Brazil - Serie A",
    "ARG": "Argentina - Primera Division",
    "MEX": "Mexico - Liga MX",
    "JAP": "Japan - J1 League",
}

# Leagues the commercial product actively serves out of the box.
# (Every league in SOCCER_LEAGUES can be modeled; this list just controls
# what gets trained/backtested by default to keep runtime sane.)
DEFAULT_SOCCER_LEAGUES = [
    "E0", "E1", "SP1", "SP2", "I1", "I2", "D1", "D2", "F1", "F2",
    "N1", "P1", "B1", "SC0", "T1", "G1", "USA", "BRA", "ARG", "MEX",
]

# Time-decay half life (days) for weighting historical matches in soccer fits.
SOCCER_HALF_LIFE_DAYS = 400

# Elo constants
SOCCER_ELO_K = 20.0
SOCCER_ELO_HOME_ADV = 60.0
NBA_ELO_K = 20.0
NBA_ELO_HOME_ADV = 100.0
NBA_ELO_MOV_MULT_A = 2.2
NBA_ELO_MOV_MULT_B = 0.001
SEASON_REGRESSION = 0.25  # fraction of regression to mean 1500 at season boundary
