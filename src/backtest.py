"""
Backtesting harness. The whole point of a commercial-grade system is that
its edge is measured against the market, not against "did we guess the
winner" — a book's closing line already prices in almost everything public,
so the real benchmarks are:

  1. Brier score / log-loss of our probabilities vs. actual outcomes
     (lower is better; compared against the market's de-vigged
     probabilities as the toughest realistic baseline).
  2. Calibration: when we say 70%, does it happen ~70% of the time?
  3. Simulated closing-line value (CLV): if we only bet where our
     probability exceeds the market's by some edge threshold, is the
     resulting flat-stake ROI positive over a large, out-of-sample
     sample? This is the metric that actually matters for a sportsbook /
     bettor, far more than raw accuracy.

Soccer backtest evaluates against the real historical closing odds in the
dataset. The AFL backtest evaluates against actual results only (no static
public odds archive was available to pull in this offline build — see
README "Plugging in live odds" for how a book's own feed slots in here).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def brier_multiclass(probs: np.ndarray, outcomes: np.ndarray) -> float:
    """probs: (N,3) array of [p_home,p_draw,p_away]; outcomes: (N,) in {0,1,2}."""
    onehot = np.eye(3)[outcomes]
    return float(np.mean(np.sum((probs - onehot) ** 2, axis=1)))


def log_loss_multiclass(probs: np.ndarray, outcomes: np.ndarray, eps=1e-12) -> float:
    onehot = np.eye(3)[outcomes]
    p = np.clip(probs, eps, 1 - eps)
    return float(-np.mean(np.sum(onehot * np.log(p), axis=1)))


def brier_binary(probs: np.ndarray, outcomes: np.ndarray) -> float:
    return float(np.mean((probs - outcomes) ** 2))


def log_loss_binary(probs: np.ndarray, outcomes: np.ndarray, eps=1e-12) -> float:
    p = np.clip(probs, eps, 1 - eps)
    return float(-np.mean(outcomes * np.log(p) + (1 - outcomes) * np.log(1 - p)))


def calibration_table(probs: np.ndarray, outcomes: np.ndarray, n_bins: int = 10) -> pd.DataFrame:
    bins = np.linspace(0, 1, n_bins + 1)
    idx = np.digitize(probs, bins) - 1
    idx = np.clip(idx, 0, n_bins - 1)
    rows = []
    for b in range(n_bins):
        mask = idx == b
        if mask.sum() == 0:
            continue
        rows.append({
            "bin_lo": bins[b], "bin_hi": bins[b + 1], "n": int(mask.sum()),
            "avg_predicted": float(probs[mask].mean()),
            "actual_freq": float(outcomes[mask].mean()),
        })
    return pd.DataFrame(rows)


def simulate_value_betting(model_probs: np.ndarray, market_probs: np.ndarray,
                            odds: np.ndarray, outcomes: np.ndarray,
                            edge_threshold: float = 0.04, stake: float = 1.0) -> dict:
    """Flat-stake simulation: bet `stake` on any outcome where
    model_probs - market_probs (de-vigged fair probs) exceeds
    edge_threshold. odds are the DECIMAL odds actually available (i.e.
    with the book's vig baked in, since that's what you'd actually get
    paid). Returns bets placed, staked, returned, ROI, and win rate.
    """
    edge = model_probs - market_probs
    bet_mask = edge > edge_threshold
    n_bets = int(bet_mask.sum())
    if n_bets == 0:
        return {"n_bets": 0, "staked": 0.0, "returned": 0.0, "roi": None, "win_rate": None}
    staked = n_bets * stake
    returned = np.where(bet_mask & (outcomes == 1), stake * odds, 0.0).sum()
    returned += 0.0  # losses return 0
    roi = (returned - staked) / staked
    win_rate = float((outcomes[bet_mask] == 1).mean())
    return {
        "n_bets": n_bets, "staked": float(staked), "returned": float(returned),
        "roi": float(roi), "win_rate": win_rate,
    }
