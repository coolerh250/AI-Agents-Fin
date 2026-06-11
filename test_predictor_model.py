"""test_predictor_model.py — Prediction Step 2 unit tests.

Focus: the no-lookahead point-in-time alignment (the critical correctness
property), direction thresholds, and the LogisticPredictor wrapper interface.
yfinance fetching is integration-tested on host; these tests are pure logic.
"""
import numpy as np
import pandas as pd
import pytest

from market_features import (
    DIRECTIONS,
    FEATURE_COLS,
    _align_feature,
    _direction,
)


# ── No-lookahead alignment (the property that matters most) ─────────────────────

def _feat_df(dates, vals):
    return pd.DataFrame({"val": vals}, index=pd.DatetimeIndex(dates))


def test_align_uses_strictly_prior_value():
    # feature observed on D1..D5; aligning onto D2..D5 must pull the value from
    # the date STRICTLY BEFORE each target — never the same-day value.
    fdf = _feat_df(
        ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"],
        [10.0, 20.0, 30.0, 40.0, 50.0],
    )
    twse = pd.DatetimeIndex(["2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"])
    out = _align_feature(twse, fdf, "val")
    # D2 → D1's 10 (NOT 20), D3 → D2's 20, ...
    assert out.loc["2026-01-02"] == 10.0
    assert out.loc["2026-01-03"] == 20.0
    assert out.loc["2026-01-04"] == 30.0
    assert out.loc["2026-01-05"] == 40.0


def test_align_no_prior_is_nan():
    fdf = _feat_df(["2026-01-03", "2026-01-04"], [30.0, 40.0])
    twse = pd.DatetimeIndex(["2026-01-01", "2026-01-03", "2026-01-05"])
    out = _align_feature(twse, fdf, "val")
    assert pd.isna(out.loc["2026-01-01"])   # nothing before 01
    assert pd.isna(out.loc["2026-01-03"])   # 03 itself excluded, nothing before
    assert out.loc["2026-01-05"] == 40.0    # 04 is the last strictly before 05


def test_align_gap_weekend():
    # Friday feature, predicting Monday: Monday uses Friday (strictly before).
    fdf = _feat_df(["2026-01-02", "2026-01-05"], [11.0, 22.0])  # Fri, Mon
    twse = pd.DatetimeIndex(["2026-01-05"])                      # Mon TWSE
    out = _align_feature(twse, fdf, "val")
    assert out.loc["2026-01-05"] == 11.0   # Friday's value, not Monday's


def test_align_same_day_excluded():
    # The exact-match guard: a feature dated == target must be excluded.
    fdf = _feat_df(["2026-01-01", "2026-01-02"], [1.0, 2.0])
    twse = pd.DatetimeIndex(["2026-01-02"])
    out = _align_feature(twse, fdf, "val")
    assert out.loc["2026-01-02"] == 1.0    # D1, not D2's own 2.0


# ── direction thresholds ───────────────────────────────────────────────────────

def test_direction_band():
    assert _direction(0.31) == "up"
    assert _direction(0.30) == "flat"
    assert _direction(-0.30) == "flat"
    assert _direction(-0.31) == "down"


# ── LogisticPredictor wrapper ───────────────────────────────────────────────────

def _separable_dataset(n_per=40):
    """Synthetic: sox_chg_pct strongly determines class. Other features noise."""
    rng = np.random.default_rng(0)
    rows, labels = [], []
    specs = [(2.5, "up"), (0.0, "flat"), (-2.5, "down")]
    for center, lab in specs:
        for _ in range(n_per):
            row = {c: float(rng.normal(0, 0.2)) for c in FEATURE_COLS}
            row["sox_chg_pct"] = float(rng.normal(center, 0.3))
            row["vix_close"] = float(rng.normal(18, 1))
            rows.append(row)
            labels.append(lab)
    X = pd.DataFrame(rows)[FEATURE_COLS]
    y = pd.Series(labels, name="direction")
    return X, y


def test_logistic_proba_shape_and_order():
    from predictor_model import LogisticPredictor
    X, y = _separable_dataset()
    m = LogisticPredictor().fit(X, y)
    probs = m.predict_proba(X.iloc[0])
    assert len(probs) == 3
    assert pytest.approx(sum(probs), abs=1e-6) == 1.0
    assert all(0.0 <= p <= 1.0 for p in probs)


def test_logistic_learns_separable_pattern():
    from predictor_model import LogisticPredictor
    X, y = _separable_dataset()
    m = LogisticPredictor().fit(X, y)
    up   = X[y == "up"].iloc[0]
    down = X[y == "down"].iloc[0]
    assert m.predict_direction(up) == "up"
    assert m.predict_direction(down) == "down"


def test_walk_forward_shape():
    from predictor_model import walk_forward
    X, y = _separable_dataset(n_per=40)  # 120 rows
    # shuffle into a date-ordered series
    dates = pd.DatetimeIndex(pd.date_range("2025-01-01", periods=len(X), freq="D"))
    X = X.reset_index(drop=True)
    y = y.reset_index(drop=True)
    wf = walk_forward(X, y, dates, min_train=60, retrain_every=5)
    assert len(wf) == len(X) - 60
    assert set(wf["correct"].unique()) <= {0, 1}
    assert list(wf.columns) == ["date", "pred", "actual", "correct"]
