"""test_predictors.py — unit tests for Prediction Step 1 baseline predictors.

Covers: probability mapping (sums to 1, monotone, flat-at-zero), Brier
correctness on synthetic cases, each predictor's direction logic, and a
walk-forward no-lookahead assertion for the majority predictor.
"""
import numpy as np
import pytest

from predictors import (
    DIRECTIONS,
    brier_score,
    direction_from_gap,
    gap_pct_to_probs,
    llm_probs,
    predict_naive_nightfutures,
    predict_unconditional_majority,
    predict_weighted_rule,
)


# ── gap_pct_to_probs ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("gap", [-3.0, -1.0, -0.3, 0.0, 0.3, 1.0, 3.0])
def test_probs_sum_to_one(gap):
    p = gap_pct_to_probs(gap)
    assert pytest.approx(sum(p), abs=1e-9) == 1.0
    assert all(0.0 <= x <= 1.0 for x in p)


def test_probs_flat_dominates_at_zero():
    p_up, p_flat, p_down = gap_pct_to_probs(0.0)
    assert p_flat > p_up and p_flat > p_down
    assert pytest.approx(p_up, abs=1e-9) == p_down  # symmetric at 0


def test_probs_up_dominates_for_large_positive():
    p_up, p_flat, p_down = gap_pct_to_probs(2.0)
    assert p_up > p_flat > p_down


def test_probs_down_dominates_for_large_negative():
    p_up, p_flat, p_down = gap_pct_to_probs(-2.0)
    assert p_down > p_flat > p_up


def test_probs_monotone_in_gap():
    # P(up) strictly increases as gap increases
    gaps = [-2.0, -1.0, 0.0, 1.0, 2.0]
    p_ups = [gap_pct_to_probs(g)[0] for g in gaps]
    assert all(p_ups[i] < p_ups[i + 1] for i in range(len(p_ups) - 1))


# ── brier_score ────────────────────────────────────────────────────────────────

def test_brier_perfect_confident_prediction():
    # one-hot prob exactly on the actual → Brier 0
    assert brier_score((1.0, 0.0, 0.0), "up") == pytest.approx(0.0)


def test_brier_confidently_wrong():
    # one-hot on up, actual down → (1-0)^2 + 0 + (0-1)^2 = 2
    assert brier_score((1.0, 0.0, 0.0), "down") == pytest.approx(2.0)


def test_brier_uniform_prior():
    # uniform (1/3,1/3,1/3) vs any actual → 2/3
    assert brier_score((1 / 3, 1 / 3, 1 / 3), "flat") == pytest.approx(2 / 3)


# ── naive_nightfutures ─────────────────────────────────────────────────────────

def test_naive_follows_night_futures_sign():
    d, gap, _ = predict_naive_nightfutures({"night_futures_chg_pct": 1.2})
    assert d == "up" and gap == pytest.approx(1.2)
    d, gap, _ = predict_naive_nightfutures({"night_futures_chg_pct": -1.2})
    assert d == "down"
    d, _, _ = predict_naive_nightfutures({"night_futures_chg_pct": 0.1})
    assert d == "flat"  # within ±0.3 dead band


def test_naive_falls_back_to_overnight_when_night_missing():
    # no night_futures → weighted overnight (SOX .30 etc). All strong up.
    feat = {"sox_chg_pct": 2.0, "ndx_chg_pct": 2.0,
            "tsm_adr_chg_pct": 2.0, "djia_chg_pct": 2.0}
    d, gap, _ = predict_naive_nightfutures(feat)
    assert d == "up" and gap == pytest.approx(2.0)


# ── weighted_rule ──────────────────────────────────────────────────────────────

def test_weighted_rule_uses_night_weighting_when_present():
    # night_futures dominates at 0.40 weight
    feat = {"night_futures_chg_pct": 3.0, "sox_chg_pct": -0.5,
            "ndx_chg_pct": -0.5, "tsm_adr_chg_pct": -0.5, "djia_chg_pct": -0.5}
    d, avg, _ = predict_weighted_rule(feat)
    # 0.40*3 + (0.20+0.18+0.14+0.08)*(-0.5) = 1.2 - 0.30 = 0.90 → up
    assert avg == pytest.approx(0.90)
    assert d == "up"


def test_weighted_rule_day_weighting_when_night_missing():
    feat = {"sox_chg_pct": 1.0, "ndx_chg_pct": 1.0,
            "tsm_adr_chg_pct": 1.0, "djia_chg_pct": 1.0}
    d, avg, _ = predict_weighted_rule(feat)
    assert avg == pytest.approx(1.0)  # weights sum to 1.0
    assert d == "up"


def test_weighted_rule_flat_near_zero():
    feat = {"night_futures_chg_pct": 0.0, "sox_chg_pct": 0.0,
            "ndx_chg_pct": 0.0, "tsm_adr_chg_pct": 0.0, "djia_chg_pct": 0.0}
    d, avg, _ = predict_weighted_rule(feat)
    assert d == "flat" and avg == pytest.approx(0.0)


# ── unconditional_majority (walk-forward) ──────────────────────────────────────

def test_majority_picks_most_common():
    trailing = ["up", "up", "up", "down", "flat"]
    d, gap, probs = predict_unconditional_majority(trailing)
    assert d == "up"
    assert gap == 0.0
    assert probs[0] == pytest.approx(3 / 5)  # P(up) = empirical freq


def test_majority_cold_start_uniform():
    d, gap, probs = predict_unconditional_majority([])
    assert d == "flat"
    assert probs == pytest.approx((1 / 3, 1 / 3, 1 / 3))


def test_majority_probs_are_calibrated_frequencies():
    trailing = ["up"] * 6 + ["down"] * 3 + ["flat"] * 1
    _, _, probs = predict_unconditional_majority(trailing)
    assert probs[0] == pytest.approx(0.6)   # up
    assert probs[1] == pytest.approx(0.1)   # flat
    assert probs[2] == pytest.approx(0.3)   # down


def test_majority_no_lookahead():
    """The majority predictor must depend ONLY on the trailing actuals passed.
    Appending a FUTURE actual must not change a prediction made from the past
    window — this guards the walk-forward contract at the call boundary."""
    past = ["up", "up", "down"]
    pred_past = predict_unconditional_majority(past)
    # caller appends tomorrow's actual; the *past* prediction is unchanged
    future = past + ["down", "down", "down"]
    pred_future = predict_unconditional_majority(future)
    assert pred_past[0] == "up"     # past window majority
    assert pred_future[0] == "down"  # different window → different call
    # i.e. the function is a pure function of its window; no hidden state
    assert predict_unconditional_majority(past)[0] == "up"


# ── llm_probs ──────────────────────────────────────────────────────────────────

def test_llm_probs_none_is_uniform():
    assert llm_probs(None) == pytest.approx((1 / 3, 1 / 3, 1 / 3))


def test_llm_probs_tracks_gap():
    assert llm_probs(2.0) == pytest.approx(gap_pct_to_probs(2.0))


# ── direction_from_gap edges ───────────────────────────────────────────────────

def test_direction_band_edges():
    assert direction_from_gap(0.31) == "up"
    assert direction_from_gap(0.30) == "flat"     # boundary inclusive to flat
    assert direction_from_gap(-0.30) == "flat"
    assert direction_from_gap(-0.31) == "down"
