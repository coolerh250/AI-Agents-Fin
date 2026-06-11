"""predictor_model.py — Prediction Step 2: the fitted logistic quant model.

Trains a multinomial logistic regression on the deep-history dataset from
market_features.build_dataset, with strict walk-forward (train on rows STRICTLY
before each prediction date). Exposes:
  - LogisticPredictor: fit / predict_proba → (p_up, p_flat, p_down) / direction
  - walk_forward(): out-of-sample predictions + hit rate over the whole history
  - CLI `report`: OOS hit rate + coefficients (which features carry sign)

This is the head-to-head answer to "does a fitted model beat the hand-tuned
weighted rule?" — scored on the same ±0.3% target the rest of the system uses.
"""
from __future__ import annotations

import sys
from typing import Optional

import numpy as np
import pandas as pd

from market_features import DIRECTIONS, FEATURE_COLS, build_dataset

_MIN_TRAIN = 60          # don't predict until this many prior samples exist
_L2_C = 1.0              # inverse regularization strength


class LogisticPredictor:
    """Thin wrapper over a StandardScaler + multinomial LogisticRegression
    pipeline. Probabilities are returned in DIRECTIONS (up, flat, down) order
    regardless of the class order the model learned."""

    def __init__(self, C: float = _L2_C):
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
        self._pipe = Pipeline([
            ("scale", StandardScaler()),
            ("clf", LogisticRegression(
                C=C, max_iter=1000, class_weight="balanced",
            )),
        ])
        self._classes: list[str] = []

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "LogisticPredictor":
        self._pipe.fit(X[FEATURE_COLS].to_numpy(), y.to_numpy())
        self._classes = list(self._pipe.named_steps["clf"].classes_)
        return self

    def predict_proba(self, x: pd.Series) -> tuple[float, float, float]:
        raw = self._pipe.predict_proba(
            np.asarray(x[FEATURE_COLS], dtype=float).reshape(1, -1)
        )[0]
        by_class = dict(zip(self._classes, raw))
        return (float(by_class.get("up", 0.0)),
                float(by_class.get("flat", 0.0)),
                float(by_class.get("down", 0.0)))

    def predict_direction(self, x: pd.Series) -> str:
        probs = self.predict_proba(x)
        return DIRECTIONS[int(np.argmax(probs))]


def walk_forward(X: pd.DataFrame, y: pd.Series, dates: pd.DatetimeIndex,
                 min_train: int = _MIN_TRAIN, retrain_every: int = 5
                 ) -> pd.DataFrame:
    """Expanding-window walk-forward. For each date i >= min_train, train on
    rows [0, i) and predict row i. Retrains every `retrain_every` steps for
    speed (the model between retrains is the last fitted one — still trained
    only on strictly-prior data). Returns a frame with pred/actual/correct."""
    n = len(dates)
    rows = []
    model: Optional[LogisticPredictor] = None
    for i in range(min_train, n):
        if model is None or (i - min_train) % retrain_every == 0:
            model = LogisticPredictor().fit(X.iloc[:i], y.iloc[:i])
        pred = model.predict_direction(X.iloc[i])
        actual = y.iloc[i]
        rows.append({
            "date": dates[i], "pred": pred, "actual": actual,
            "correct": int(pred == actual),
        })
    return pd.DataFrame(rows)


def _report() -> int:
    print("[model] building dataset (yfinance, 2y)...", flush=True)
    X, y, dates = build_dataset(years=2)
    print(f"[model] {len(X)} samples, {len(FEATURE_COLS)} features, "
          f"{dates[0].date()} → {dates[-1].date()}")
    print(f"[model] class balance: "
          + ", ".join(f"{d}={int((y == d).sum())}" for d in DIRECTIONS))

    wf = walk_forward(X, y, dates)
    if wf.empty:
        print("[model] not enough data for walk-forward")
        return 1
    hit = wf["correct"].mean() * 100

    # Apples-to-apples: score the Step-1 rule predictors over the SAME deep
    # history (stateless functions of each day's features), so we can tell
    # "fitting is weak" apart from "the recent 15-day window was easy".
    import predictors as _P
    wf_dates = set(pd.Timestamp(d) for d in wf["date"])
    rule_hits = naive_hits = maj_hits = total = 0
    trailing: list[str] = []
    for i in range(len(dates)):
        d = dates[i]
        actual = y.iloc[i]
        if pd.Timestamp(d) in wf_dates:
            feat = X.iloc[i].to_dict()
            if _P.predict_weighted_rule(feat)[0] == actual:
                rule_hits += 1
            if _P.predict_naive_nightfutures(feat)[0] == actual:
                naive_hits += 1
            if _P.predict_unconditional_majority(trailing[-60:])[0] == actual:
                maj_hits += 1
            total += 1
        trailing.append(actual)
    rule = rule_hits / total * 100 if total else 0
    naive = naive_hits / total * 100 if total else 0
    maj = maj_hits / total * 100 if total else 0

    print(f"\n=== Deep-history walk-forward OOS (n={len(wf)}, same dates) ===")
    print(f"  logistic_l2            : {hit:.1f}%")
    print(f"  weighted_rule          : {rule:.1f}%")
    print(f"  naive_nightfutures     : {naive:.1f}%")
    print(f"  unconditional_majority : {maj:.1f}%  (null)")
    print(f"  logistic edge vs null  : {hit - maj:+.1f}pp")

    # coefficients from a full-history fit (interpretability)
    model = LogisticPredictor().fit(X, y)
    clf = model._pipe.named_steps["clf"]
    classes = list(clf.classes_)
    print("\n=== LogisticRegression coefficients (standardized features) ===")
    header = "feature".ljust(18) + "".join(c.rjust(10) for c in classes)
    print(header)
    for j, feat in enumerate(FEATURE_COLS):
        line = feat.ljust(18) + "".join(f"{clf.coef_[k][j]:>10.3f}"
                                        for k in range(len(classes)))
        print(line)
    print("\n(Positive coef → that feature pushes toward that class. "
          "Compare logistic hit rate to Step 1's 73% rule / 47% null.)\n")
    return 0


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Prediction Step 2 quant model")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("report")
    args = parser.parse_args()
    if args.cmd == "report":
        return _report()
    return 2


if __name__ == "__main__":
    sys.exit(main())
