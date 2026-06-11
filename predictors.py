"""predictors.py — Prediction Step 1 baseline predictors + scoring primitives.

Pure functions, no IO, no LLM. Each predictor maps a day's pre-open features
to (direction, gap_pct, (p_up, p_flat, p_down)). Scored uniformly against the
actual gap direction with the same ±0.3% band the rest of the system uses
(evaluation_runner._calc_actual_direction / get_recent_accuracy_context).

The four predictors on the scoreboard (scripts/predictor_scoreboard.py):
  - unconditional_majority : trailing-window majority class (the null hypothesis)
  - naive_nightfutures     : follow the night-futures sign (dumb baseline)
  - weighted_rule          : _TECH_SYSTEM's weighted formula in code (the control
                             that answers "does the LLM beat its own arithmetic?")
  - llm_tech_analyst       : the production LLM output (scored, not computed here)

Design choices (kept uniform so the comparison is fair):
  * ONE band (±0.3%, = the actuals definition) drives gap_pct_to_probs; a
    gap-based predictor's hard call is argmax of its probability vector. This
    keeps direction and confidence coherent and judges every predictor by the
    same rule the actuals are defined with.
  * weighted_rule carries _TECH_SYSTEM's WEIGHTS faithfully (the substance);
    only the final ±0.3 cut is unified for fairness.
  * gap_pct_to_probs uses a fixed shared temperature so "predicts +1.8%" is
    more confident than "+0.4%" identically across predictors — Brier then
    measures calibration, not an unfair per-predictor knob.
"""
from __future__ import annotations

from typing import Optional

import numpy as np

DIRECTIONS = ("up", "flat", "down")

_FLAT_BAND = 0.3    # ±0.3% — matches actual_direction threshold system-wide
_PROB_TEMP = 0.5    # fixed softmax temperature, shared by all predictors

# _TECH_SYSTEM weights (market_analyst_agents.py:82-106), reproduced faithfully.
_WEIGHTS_NIGHT = {
    "night_futures_chg_pct": 0.40,
    "sox_chg_pct":           0.20,
    "ndx_chg_pct":           0.18,
    "tsm_adr_chg_pct":       0.14,
    "djia_chg_pct":          0.08,
}
_WEIGHTS_DAY = {
    "sox_chg_pct":     0.30,
    "ndx_chg_pct":     0.25,
    "tsm_adr_chg_pct": 0.25,
    "djia_chg_pct":    0.20,
}


# ── Scoring primitives ─────────────────────────────────────────────────────────

def direction_from_gap(gap_pct: float, band: float = _FLAT_BAND) -> str:
    if gap_pct > band:
        return "up"
    if gap_pct < -band:
        return "down"
    return "flat"


def gap_pct_to_probs(gap_pct: float, temp: float = _PROB_TEMP,
                     band: float = _FLAT_BAND) -> tuple[float, float, float]:
    """Monotone map from a point gap estimate to P(up, flat, down).

    Class scores are distances past the ±band boundary; a fixed-temperature
    softmax turns them into a distribution. At gap=0 → flat dominates; at a
    large |gap| → the matching tail dominates; at gap=±band → up/flat (or
    down/flat) are near-tied. Returns probabilities in DIRECTIONS order."""
    g = max(-5.0, min(5.0, float(gap_pct)))
    scores = np.array([
        g - band,         # up
        band - abs(g),    # flat
        -g - band,        # down
    ]) / temp
    scores -= scores.max()
    e = np.exp(scores)
    p = e / e.sum()
    return float(p[0]), float(p[1]), float(p[2])


def brier_score(probs: tuple[float, float, float], actual_direction: str) -> float:
    """Multiclass Brier: Σ_k (p_k − I[actual==k])². Range [0, 2]."""
    onehot = [1.0 if actual_direction == d else 0.0 for d in DIRECTIONS]
    return float(sum((probs[i] - onehot[i]) ** 2 for i in range(3)))


def _argmax_direction(probs: tuple[float, float, float]) -> str:
    return DIRECTIONS[int(np.argmax(probs))]


# ── Feature helpers ────────────────────────────────────────────────────────────

def _weighted_overnight(feat: dict) -> float:
    """No-night-futures weighting over the 4 overnight US features."""
    return sum(w * float(feat.get(k) or 0.0) for k, w in _WEIGHTS_DAY.items())


def _weighted_full(feat: dict) -> float:
    """Night-futures weighting (5 features) when night_futures present."""
    return sum(w * float(feat.get(k) or 0.0) for k, w in _WEIGHTS_NIGHT.items())


# ── Predictors ─────────────────────────────────────────────────────────────────
# Each returns (pred_direction, pred_gap_pct, (p_up, p_flat, p_down)).

def predict_naive_nightfutures(feat: dict) -> tuple[str, float, tuple[float, float, float]]:
    """Follow the night-futures sign; fall back to weighted overnight when the
    night-futures field is missing (historical rows have no night futures)."""
    nf = feat.get("night_futures_chg_pct")
    gap = float(nf) if nf is not None else _weighted_overnight(feat)
    probs = gap_pct_to_probs(gap)
    return direction_from_gap(gap), gap, probs


def predict_weighted_rule(feat: dict) -> tuple[str, float, tuple[float, float, float]]:
    """_TECH_SYSTEM's weighted average in code. Uses the 5-feature night
    weighting when night_futures is present, else the 4-feature day weighting.
    Direction = argmax of the calibrated probs (uniform ±0.3 cut)."""
    nf = feat.get("night_futures_chg_pct")
    avg = _weighted_full(feat) if nf is not None else _weighted_overnight(feat)
    probs = gap_pct_to_probs(avg)
    return _argmax_direction(probs), avg, probs


def predict_unconditional_majority(
    trailing_actuals: list[str],
) -> tuple[str, float, tuple[float, float, float]]:
    """The null hypothesis: predict the most common actual direction over the
    trailing window. Probabilities are the empirical class frequencies (a
    well-calibrated prior — its Brier is the bar everyone must beat). Caller
    must pass ONLY past actuals (walk-forward, no lookahead). Cold start
    (empty window) → flat with a uniform prior."""
    if not trailing_actuals:
        return "flat", 0.0, (1 / 3, 1 / 3, 1 / 3)
    n = len(trailing_actuals)
    freqs = tuple(
        trailing_actuals.count(d) / n for d in DIRECTIONS
    )  # (up, flat, down)
    return _argmax_direction(freqs), 0.0, freqs  # type: ignore[arg-type]


def llm_probs(predicted_gap_pct: Optional[float]) -> tuple[float, float, float]:
    """Confidence vector for the production LLM, derived from its stored
    estimated_gap_pct. The LLM's hard direction is its own stored output (read
    by the scoreboard), not recomputed here — this only supplies the prob
    vector for Brier/calibration."""
    if predicted_gap_pct is None:
        return (1 / 3, 1 / 3, 1 / 3)
    return gap_pct_to_probs(float(predicted_gap_pct))
