"""
Matching live-feed team names onto the names the models were trained with.

This is not cosmetic plumbing — it decides whether a prediction means
anything. The training data (football-data.co.uk) uses terse names like
"Man City", "Ath Madrid", "Inter", "AZ Alkmaar". The live feeds return formal
registered names: "Manchester City FC", "Club Atlético de Madrid", "FC
Internazionale Milano", "AZ". A plain character-similarity match fails on
most of those, and an unmatched team silently falls back to
competition-average strength, so the board ends up pricing Manchester City as
a mid-table side while looking perfectly normal.

Strategy, cheapest and safest first:

  1. Exact match on a normalised form (accents folded, punctuation dropped).
  2. An explicit alias table for names no rule can bridge (PSG, Spurs).
  3. Token matching after stripping club-type words and founding years, where
     every token of the shorter name must prefix a distinct token of the
     longer one. This is what bridges "Man City" to "Manchester City FC" and
     "Inter" to "FC Internazionale Milano".
  4. Fuzzy similarity, last and strictest.

A wrong-but-confident match is worse than no match: it would run the
prediction, head-to-head and form narrative for the wrong club under the
right club's name. So each rule is required to be unambiguous — if a step
produces more than one candidate, it declines rather than guesses, and the
caller keeps the raw name and falls back to average strength.
"""
from __future__ import annotations

import difflib
import re
import unicodedata

# Club-type words and legal suffixes that carry no identifying information.
# Only ever stripped when a token is one of these outright.
# NOTE: "city", "united", "town", "rovers" and friends are deliberately NOT
# here. They look like decoration but they are the identifying half of most
# English club names — stripping them collapses Manchester City and
# Manchester United onto the same token and makes the match ambiguous, which
# is exactly the bug this module exists to fix.
_NOISE = {
    "fc", "afc", "cf", "sc", "ac", "as", "ss", "ssd", "us", "sv", "sk",
    "rc", "cd", "ca", "cr", "ec", "sd", "ud", "vfl", "vfb", "tsg", "fsv", "bsc",
    "cfc", "afcs", "club", "clube", "calcio", "futbol", "football", "fussball",
    "spor", "kulubu", "sportclub", "sportiva", "societa",
    "de", "do", "da", "of", "the", "and", "el", "la", "le", "les", "los", "e",
}

# Names no rule can bridge. Keyed by normalised live name -> trained name.
# Deliberately small: every entry here is a rule that failed, so the list
# doubles as a record of what the heuristics do not cover.
ALIASES = {
    "parissaintgermain": "Paris SG",
    "tottenhamhotspur": "Tottenham",
    "wolverhamptonwanderers": "Wolves",
    "brightonhovealbion": "Brighton",
    "westhamunited": "West Ham",
    "newcastleunited": "Newcastle",
    "sheffieldunited": "Sheffield United",
    "nottinghamforest": "Nott'm Forest",
    "internazionale": "Inter",
    "internazionalemilano": "Inter",
    "queensparkrangers": "QPR",
    "staderennais": "Rennes",
    "nec": "Nijmegen",
    "vitoria": "Guimaraes",
    "vitoriasc": "Guimaraes",
    "acmilan": "Milan",
    "asroma": "Roma",
    "sscnapoli": "Napoli",
    "juventus": "Juventus",
    "clubatleticodemadrid": "Ath Madrid",
    "athleticclub": "Ath Bilbao",
    "rcceltadevigo": "Celta",
    "rcdeportivolacoruna": "La Coruna",
    "realsociedaddefutbol": "Sociedad",
    "realbetisbalompie": "Betis",
    "rcdespanyoldebarcelona": "Espanol",
    "psveindhoven": "PSV",
    "feyenoordrotterdam": "Feyenoord",
    "azalkmaar": "AZ Alkmaar",
    "az": "AZ Alkmaar",
    "olympiquedemarseille": "Marseille",
    "olympiquelyonnais": "Lyon",
    "assaintetienne": "St Etienne",
    "borussiadortmund": "Dortmund",
    "borussiamonchengladbach": "M'gladbach",
    "bayernmunchen": "Bayern Munich",
    "bayer04leverkusen": "Leverkusen",
    "eintrachtfrankfurt": "Ein Frankfurt",
    "hamburgersv": "Hamburg",
    "sportlisboaebenfica": "Benfica",
    "sportingclubedeportugal": "Sp Lisbon",
    "sportingclubedebraga": "Sp Braga",
    "fcporto": "Porto",
    "gremiofbpa": "Gremio",
    "crvascodagama": "Vasco",
    "mineiro": "Atletico-MG",
    "caparanaense": "Athletico-PR",
    "scinternacional": "Internacional",
    "sepalmeiras": "Palmeiras",
    "crflamengo": "Flamengo",
    "sccorinthianspaulista": "Corinthians",
    "saopaulo": "Sao Paulo",
    "clubedoremo": "Remo",
    "ecvitoria": "Vitoria",
    "cruzeiroec": "Cruzeiro",
    "fluminense": "Fluminense",
    "botafogofr": "Botafogo RJ",
    "rbbragantino": "Bragantino",
    "santos": "Santos",
    "mirassol": "Mirassol",
    "ceara": "Ceara",
    "bahia": "Bahia",
    "juventude": "Juventude",
    "fortaleza": "Fortaleza",
    "chapecoenseaf": "Chapecoense-SC",
    "pumasunam": "U.N.A.M.- Pumas",
    "leon": "Leon",
}


def normalise(name: str) -> str:
    """Lowercase, fold accents, drop everything that isn't a letter or digit."""
    folded = unicodedata.normalize("NFKD", name or "")
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return "".join(c.lower() for c in folded if c.isalnum())


def _tokens(name: str) -> list[str]:
    """Identifying tokens: accents folded, punctuation split on, club-type
    words and founding years removed — but never reduced to nothing."""
    folded = unicodedata.normalize("NFKD", name or "")
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    raw = [t for t in re.split(r"[^A-Za-z0-9]+", folded.lower()) if t]
    kept = [t for t in raw
            if t not in _NOISE
            and not re.fullmatch(r"\d{2,4}", t)          # 1907, 05, 1899
            and not re.fullmatch(r"\d+", t)]
    if kept:
        return kept
    # Everything was noise ("AC Milan" is only noise + a name we stripped):
    # fall back to the non-year tokens so the club is still identifiable.
    return [t for t in raw if not re.fullmatch(r"\d{2,4}", t)] or raw


def _tokens_prefix_match(short: list[str], long: list[str]) -> bool:
    """True when every token of `short` prefixes a distinct token of `long`.

    Requires 3+ characters before a prefix counts, so "s" cannot claim
    "sassuolo". This is what connects "Man City" to "Manchester City" and
    "Inter" to "Internazionale".
    """
    if not short or len(short) > len(long):
        return False
    remaining = list(long)
    for tok in short:
        hit = None
        for cand in remaining:
            if cand == tok or (len(tok) >= 3 and cand.startswith(tok)) \
                           or (len(cand) >= 3 and tok.startswith(cand)):
                hit = cand
                break
        if hit is None:
            return False
        remaining.remove(hit)
    return True


def match_team(name: str, known: list[str]) -> tuple[str, str]:
    """Returns (matched_name, how). `how` is one of exact / alias / token /
    fuzzy / unmatched, so callers can report match quality honestly."""
    if not name or not known:
        return name, "unmatched"

    by_norm = {}
    for team in known:
        by_norm.setdefault(normalise(team), team)

    key = normalise(name)
    if key in by_norm:
        return by_norm[key], "exact"

    # Aliases are looked up on both the raw normalised form and the
    # noise-stripped token form, so one entry covers "Paris Saint-Germain",
    # "Paris Saint-Germain FC" and "Paris Saint Germain".
    token_key = "".join(_tokens(name))
    for candidate_key in (key, token_key):
        target = ALIASES.get(candidate_key)
        if target and target in known:
            return target, "alias"

    # Token matching, in both directions (the live name is usually the longer
    # one, but not always — "AZ" against "AZ Alkmaar" runs the other way).
    live_tokens = _tokens(name)
    candidates = []
    for team in known:
        team_tokens = _tokens(team)
        if _tokens_prefix_match(team_tokens, live_tokens) or \
           _tokens_prefix_match(live_tokens, team_tokens):
            candidates.append(team)
    if len(candidates) == 1:
        return candidates[0], "token"
    if len(candidates) > 1:
        # Ambiguous: prefer an exact token-set equality if exactly one has it.
        exact = [t for t in candidates if _tokens(t) == live_tokens]
        if len(exact) == 1:
            return exact[0], "token"
        return name, "unmatched"     # decline rather than guess

    close = difflib.get_close_matches(key, list(by_norm.keys()), n=2, cutoff=0.78)
    if len(close) == 1 or (len(close) == 2 and
                           difflib.SequenceMatcher(None, key, close[0]).ratio() -
                           difflib.SequenceMatcher(None, key, close[1]).ratio() > 0.08):
        return by_norm[close[0]], "fuzzy"

    return name, "unmatched"
