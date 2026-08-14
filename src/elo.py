"""
Generic Elo rating engine with margin-of-victory scaling, home advantage,
and season-boundary regression to the mean. Used standalone for a fast
sanity-check model, and as an input feature to the sharper sport-specific
models (Dixon-Coles for soccer, gradient-boosted margin model for NBA).

Soccer uses a 3-outcome (draw-aware) variant; basketball has no draws so
uses the classic win/margin formulation (in the style of FiveThirtyEight's
NBA Elo, an approach that has held up well in the public sports-analytics
literature for producing well-calibrated win probabilities cheaply).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


def expected_score(rating_a: float, rating_b: float) -> float:
    """Probability A beats B (no draws), standard logistic Elo curve."""
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400.0))


@dataclass
class EloEngine:
    k: float = 20.0
    home_adv: float = 60.0
    start_rating: float = 1500.0
    season_regression: float = 0.25
    ratings: dict = field(default_factory=dict)
    _last_season: dict = field(default_factory=dict)

    def get(self, team: str) -> float:
        return self.ratings.get(team, self.start_rating)

    def maybe_regress_season(self, team: str, season) -> None:
        if season is None:
            return
        prev = self._last_season.get(team)
        if prev is not None and prev != season:
            r = self.get(team)
            self.ratings[team] = r + self.season_regression * (self.start_rating - r)
        self._last_season[team] = season

    def win_probs_no_draw(self, home: str, away: str) -> float:
        """P(home win), basketball-style (no draw outcome)."""
        rh = self.get(home) + self.home_adv
        ra = self.get(away)
        return expected_score(rh, ra)

    def update_no_draw(self, home: str, away: str, home_margin: float,
                        mov_mult_a: float = 2.2, mov_mult_b: float = 0.001,
                        season=None) -> tuple[float, float]:
        """Update ratings after a game with no draw possible (basketball).
        Uses a margin-of-victory multiplier so a 30pt blowout moves ratings
        more than a 1pt nail-biter, damped by rating gap so upsets by a
        big margin aren't over-rewarded/punished.
        """
        self.maybe_regress_season(home, season)
        self.maybe_regress_season(away, season)
        rh, ra = self.get(home), self.get(away)
        p_home = expected_score(rh + self.home_adv, ra)
        actual_home = 1.0 if home_margin > 0 else 0.0
        elo_diff = (rh + self.home_adv) - ra
        if home_margin < 0:
            elo_diff = -elo_diff
        # FiveThirtyEight NBA formula: ln(|margin|+1) * (2.2 / (0.001*elo_diff + 2.2))
        mult = math.log(abs(home_margin) + 1) * (mov_mult_a / (mov_mult_b * elo_diff + mov_mult_a))
        mult = max(mult, 0.1)
        delta = self.k * mult * (actual_home - p_home)
        self.ratings[home] = rh + delta
        self.ratings[away] = ra - delta
        return rh, ra

    def win_draw_probs(self, home: str, away: str, draw_c: float = 0.42) -> tuple[float, float, float]:
        """Approximate 1X2 probabilities from Elo for soccer.
        Uses the classic 'Elo + draw quota' heuristic: start from a
        no-draw win probability, then carve out a draw probability that
        shrinks as the rating gap grows (draws are more likely between
        evenly matched sides). draw_c controls the max draw probability
        for a dead-even matchup; tuned against historical draw rates.
        """
        rh = self.get(home) + self.home_adv
        ra = self.get(away)
        diff = rh - ra
        p_home_nodraw = expected_score(rh, ra)
        draw_p = draw_c * math.exp(-((diff / 400.0) ** 2) * 1.4)
        p_home = p_home_nodraw * (1 - draw_p)
        p_away = (1 - p_home_nodraw) * (1 - draw_p)
        p_draw = draw_p
        s = p_home + p_draw + p_away
        return p_home / s, p_draw / s, p_away / s

    def update_soccer(self, home: str, away: str, result: str, season=None) -> None:
        """result in {'H','D','A'}. Uses 3-way expected score (1/0.5/0)."""
        self.maybe_regress_season(home, season)
        self.maybe_regress_season(away, season)
        rh, ra = self.get(home), self.get(away)
        ph, pdraw, pa = self.win_draw_probs(home, away)
        actual_home = {"H": 1.0, "D": 0.5, "A": 0.0}[result]
        # expected value on the same 1/0.5/0 scale
        exp_home = ph * 1.0 + pdraw * 0.5 + pa * 0.0
        delta = self.k * (actual_home - exp_home)
        self.ratings[home] = rh + delta
        self.ratings[away] = ra - delta
