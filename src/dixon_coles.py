"""
Dixon-Coles bivariate Poisson model (Dixon & Coles, 1997, J. Royal Stat.
Soc.) — the standard academic baseline for soccer scoreline modeling, used
(in various proprietary extensions) by essentially every serious soccer
quant shop. This implementation adds:

  * exponential time-decay weighting (recent form matters more)
  * per-league fitting (goal-scoring environments differ a lot by division)
  * ridge regularization on attack/defense strengths for identifiability
    and to stop small-sample teams (newly promoted, few matches) from
    getting wild estimates
  * the classic low-score tau() correlation correction for 0-0/1-0/0-1/1-1

Each team gets an attack strength and defense strength. Expected goals:
    lambda_home = exp(home_adv + attack_home - defense_away)
    lambda_away = exp(attack_away - defense_home)
Final score probabilities come from independent Poisson(lambda_home),
Poisson(lambda_away), adjusted by tau() for the four low-scoring cells
where soccer scores are empirically slightly correlated.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import minimize
from scipy.stats import poisson


def tau(x: int, y: int, lam: float, mu: float, rho: float) -> float:
    if x == 0 and y == 0:
        return 1 - lam * mu * rho
    if x == 0 and y == 1:
        return 1 + lam * rho
    if x == 1 and y == 0:
        return 1 + mu * rho
    if x == 1 and y == 1:
        return 1 - rho
    return 1.0


@dataclass
class DixonColesModel:
    league: str = ""
    half_life_days: float = 400.0
    l2: float = 0.15
    max_goals: int = 10
    teams_: list = field(default_factory=list)
    team_idx_: dict = field(default_factory=dict)
    attack_: np.ndarray = None
    defense_: np.ndarray = None
    home_adv_: float = 0.25
    rho_: float = -0.05
    league_avg_attack_: float = 0.0
    league_avg_defense_: float = 0.0
    n_matches_: int = 0
    fit_as_of_: object = None

    def _weights(self, dates, as_of):
        days = (as_of - dates).dt.days.clip(lower=0).values.astype(float)
        decay = math.log(2) / self.half_life_days
        return np.exp(-decay * days)

    def fit(self, df, as_of=None):
        """df must have columns: date, HomeTeam, AwayTeam, FTHome, FTAway."""
        df = df.dropna(subset=["HomeTeam", "AwayTeam", "FTHome", "FTAway"]).copy()
        if as_of is None:
            as_of = df["date"].max()
        self.fit_as_of_ = as_of

        teams = sorted(set(df["HomeTeam"]) | set(df["AwayTeam"]))
        self.teams_ = teams
        self.team_idx_ = {t: i for i, t in enumerate(teams)}
        n = len(teams)
        self.n_matches_ = len(df)

        w = self._weights(df["date"], as_of)
        hi = df["HomeTeam"].map(self.team_idx_).values
        ai = df["AwayTeam"].map(self.team_idx_).values
        hg = df["FTHome"].values.astype(int)
        ag = df["FTAway"].values.astype(int)

        def unpack(x):
            attack = x[:n]
            defense = x[n:2 * n]
            home_adv = x[2 * n]
            rho = x[2 * n + 1]
            return attack, defense, home_adv, rho

        def neg_log_lik(x):
            attack, defense, home_adv, rho = unpack(x)
            lam = np.exp(home_adv + attack[hi] - defense[ai])
            mu = np.exp(attack[ai] - defense[hi])
            lam = np.clip(lam, 1e-6, 50)
            mu = np.clip(mu, 1e-6, 50)
            ll = poisson.logpmf(hg, lam) + poisson.logpmf(ag, mu)
            # tau correction, only matters for low-scoring cells
            tau_vals = np.array([
                tau(int(h), int(a), l, m, rho)
                for h, a, l, m in zip(hg, ag, lam, mu)
            ])
            tau_vals = np.clip(tau_vals, 1e-6, None)
            ll = ll + np.log(tau_vals)
            reg = self.l2 * (np.sum(attack ** 2) + np.sum(defense ** 2))
            return -(np.sum(w * ll)) + reg

        x0 = np.zeros(2 * n + 2)
        x0[2 * n] = 0.25   # home_adv init
        x0[2 * n + 1] = -0.05  # rho init
        bounds = [(-3, 3)] * n + [(-3, 3)] * n + [(-1, 1)] + [(-0.3, 0.3)]

        res = minimize(neg_log_lik, x0, method="L-BFGS-B", bounds=bounds,
                        options={"maxiter": 300, "ftol": 1e-9})

        attack, defense, home_adv, rho = unpack(res.x)
        self.attack_ = attack
        self.defense_ = defense
        self.home_adv_ = float(home_adv)
        self.rho_ = float(rho)
        self.league_avg_attack_ = float(np.mean(attack))
        self.league_avg_defense_ = float(np.mean(defense))
        return self

    def _team_strength(self, team):
        if team in self.team_idx_:
            i = self.team_idx_[team]
            return self.attack_[i], self.defense_[i]
        # unseen team (newly promoted from an unmodeled division, etc.)
        # falls back to league-average strength rather than crashing.
        return self.league_avg_attack_, self.league_avg_defense_

    def lambdas(self, home: str, away: str) -> tuple[float, float]:
        a_h, d_h = self._team_strength(home)
        a_a, d_a = self._team_strength(away)
        lam = math.exp(self.home_adv_ + a_h - d_a)
        mu = math.exp(a_a - d_h)
        return lam, mu

    def score_matrix(self, home: str, away: str) -> np.ndarray:
        lam, mu = self.lambdas(home, away)
        mg = self.max_goals
        ph = poisson.pmf(np.arange(mg + 1), lam)
        pa = poisson.pmf(np.arange(mg + 1), mu)
        m = np.outer(ph, pa)
        for x in range(2):
            for y in range(2):
                m[x, y] *= tau(x, y, lam, mu, self.rho_)
        m = m / m.sum()
        return m

    def predict_1x2(self, home: str, away: str) -> tuple[float, float, float]:
        m = self.score_matrix(home, away)
        p_home = np.tril(m, -1).sum()
        p_draw = np.trace(m)
        p_away = np.triu(m, 1).sum()
        return float(p_home), float(p_draw), float(p_away)

    def predict_over_under(self, home: str, away: str, line: float = 2.5):
        m = self.score_matrix(home, away)
        mg = self.max_goals
        totals = np.add.outer(np.arange(mg + 1), np.arange(mg + 1))
        over = m[totals > line].sum()
        return float(over), float(1 - over)

    def predict_btts(self, home: str, away: str):
        m = self.score_matrix(home, away)
        yes = m[1:, 1:].sum()
        return float(yes), float(1 - yes)

    def expected_goals(self, home: str, away: str):
        return self.lambdas(home, away)

    def most_likely_score(self, home: str, away: str,
                          outcome: str | None = None) -> tuple[int, int, float]:
        """The single most probable exact scoreline, as (home, away, prob).

        Rounding expected goals is NOT the same thing and is often wrong: a
        1.6 - 1.2 fixture rounds to 2-1, while the actual modal scoreline is
        1-1, because the Poisson mode sits below the mean. This reads the
        mode off the same score matrix the 1X2 market is integrated from.

        `outcome` ("H" / "D" / "A") restricts the search to scorelines that
        produce that result. The board passes the calibrated pick, because
        the headline tip comes from the blended+calibrated ensemble while the
        matrix is raw Dixon-Coles — left unconstrained the two can disagree
        and the table shows "Away win" next to a 2-1 home scoreline, which
        reads as a bug even when both numbers are individually correct.
        """
        m = self.score_matrix(home, away)
        if outcome:
            mask = np.zeros_like(m, dtype=bool)
            if outcome == "H":
                mask[np.tril_indices_from(mask, -1)] = True
            elif outcome == "A":
                mask[np.triu_indices_from(mask, 1)] = True
            else:
                np.fill_diagonal(mask, True)
            m = np.where(mask, m, 0.0)
        h, a = np.unravel_index(int(np.argmax(m)), m.shape)
        return int(h), int(a), float(m[h, a])
