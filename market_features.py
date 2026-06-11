"""market_features.py — Prediction Step 2: deep-history feature + target builder.

Assembles a (features → next-day TAIEX direction) dataset from yfinance bulk
history, with strict no-lookahead point-in-time alignment. Self-contained: no
session_episodes / snapshot / LLM dependency. Used by predictor_model.py to fit
the logistic quant model and by the scoreboard's logistic_l2 predictor.

Point-in-time contract (the critical correctness property):
  For TWSE trade date D, every feature is the most recent global-market close
  STRICTLY BEFORE D. US/global markets close before TWSE opens (US date D-1
  close ≈ 04:00 Taipei on D, before the 09:00 TWSE open), so "index date < D"
  is exactly the pre-open overnight value. Enforced via merge_asof with
  allow_exact_matches=False and asserted in test_predictor_model.py.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

import numpy as np
import pandas as pd

# Target index + feature tickers (yfinance symbols).
_TARGET = "^TWII"          # TAIEX
_FEATURE_TICKERS = {
    "^SOX": "sox",
    "^NDX": "ndx",
    "^DJI": "djia",
    "TSM":  "tsm_adr",
    "^VIX": "vix",
    "TWD=X": "usdtwd",
    "^TNX": "us10y",        # 10y yield × 10
}

_FLAT_BAND = 0.3           # ±0.3% — system-wide direction threshold
DIRECTIONS = ("up", "flat", "down")

# The feature columns the model consumes, in a stable order.
FEATURE_COLS = [
    "sox_chg_pct", "ndx_chg_pct", "djia_chg_pct", "tsm_adr_chg_pct",
    "vix_close", "usdtwd_chg_pct", "us10y_chg",
]


def _direction(gap_pct: float, band: float = _FLAT_BAND) -> str:
    if gap_pct > band:
        return "up"
    if gap_pct < -band:
        return "down"
    return "flat"


# ── yfinance fetch ─────────────────────────────────────────────────────────────

def _fetch_close_series(ticker: str, period: str = "2y") -> Optional[pd.Series]:
    """Daily close series for a ticker, tz-naive date index. None on failure."""
    try:
        import yfinance as yf
        df = yf.Ticker(ticker).history(period=period)
        if df.empty:
            return None
        s = df["Close"].copy()
        # Normalize index to plain dates (drop tz + intraday time).
        s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
        s = s[~s.index.duplicated(keep="last")]
        return s
    except Exception:
        return None


def _pct_change_frame(close: pd.Series) -> pd.DataFrame:
    """Build a per-ticker daily frame with both level and pct-change, indexed
    by the ticker's own market dates."""
    df = pd.DataFrame({"close": close})
    df["chg_pct"] = close.pct_change() * 100.0
    return df


# ── Feature assembly (no-lookahead) ────────────────────────────────────────────

def _build_feature_frame(closes: dict[str, pd.Series]) -> pd.DataFrame:
    """Per-ticker frames indexed by that ticker's own dates. Returns a dict-like
    long structure later aligned onto TWSE dates via merge_asof."""
    frames = {}
    for sym, name in _FEATURE_TICKERS.items():
        s = closes.get(sym)
        if s is None:
            continue
        frames[name] = _pct_change_frame(s)
    return frames


def _align_feature(twse_dates: pd.DatetimeIndex, feat_df: pd.DataFrame,
                   value_col: str) -> pd.Series:
    """For each TWSE date D, pick feat_df[value_col] at the most recent feature
    date STRICTLY BEFORE D (no-lookahead). Returns a Series indexed by the
    (sorted) twse_dates. Built without reset_index to avoid index-name fragility."""
    left = pd.DataFrame({"date": pd.DatetimeIndex(twse_dates)}).sort_values("date")
    right = pd.DataFrame({
        "date": pd.DatetimeIndex(feat_df.index),
        value_col: feat_df[value_col].to_numpy(),
    }).sort_values("date")
    merged = pd.merge_asof(
        left, right, on="date",
        direction="backward", allow_exact_matches=False,  # STRICTLY before D
    )
    return pd.Series(merged[value_col].to_numpy(),
                     index=pd.DatetimeIndex(merged["date"].to_numpy()))


def build_dataset(years: int = 2) -> tuple[pd.DataFrame, pd.Series, pd.DatetimeIndex]:
    """Assemble (X features, y direction, dates) over `years` of history.

    Each row is one TWSE trading day D: features = pre-open overnight values
    (strictly before D), y = TAIEX close-to-close direction on D. Rows with any
    missing feature or missing target are dropped. Returns (X_df[FEATURE_COLS],
    y_series, date_index) sorted ascending by date."""
    period = f"{max(1, years)}y"
    twii = _fetch_close_series(_TARGET, period=period)
    if twii is None or len(twii) < 30:
        raise RuntimeError("market_features: could not fetch ^TWII history")
    twii = twii.sort_index()

    # Target: TAIEX close-to-close % on D → direction on D.
    twii_chg = twii.pct_change() * 100.0
    twse_dates = pd.DatetimeIndex(twii.index)

    closes = {sym: _fetch_close_series(sym, period=period)
              for sym in _FEATURE_TICKERS}
    frames = _build_feature_frame(closes)

    cols: dict[str, pd.Series] = {}
    # chg_pct features
    for name, col in [("sox", "sox_chg_pct"), ("ndx", "ndx_chg_pct"),
                      ("djia", "djia_chg_pct"), ("tsm_adr", "tsm_adr_chg_pct"),
                      ("usdtwd", "usdtwd_chg_pct"), ("us10y", "us10y_chg")]:
        if name in frames:
            cols[col] = _align_feature(twse_dates, frames[name], "chg_pct")
        else:
            cols[col] = pd.Series(np.nan, index=twse_dates)
    # level feature: VIX close
    if "vix" in frames:
        cols["vix_close"] = _align_feature(twse_dates, frames["vix"], "close")
    else:
        cols["vix_close"] = pd.Series(np.nan, index=twse_dates)

    X = pd.DataFrame(cols, index=twse_dates)[FEATURE_COLS]
    y = pd.Series([_direction(v) if pd.notna(v) else None for v in twii_chg.values],
                  index=twse_dates, name="direction")

    full = X.copy()
    full["direction"] = y
    full = full.dropna()
    X_clean = full[FEATURE_COLS]
    y_clean = full["direction"]
    return X_clean, y_clean, pd.DatetimeIndex(full.index)


def features_for_date(target_date: date, years: int = 2) -> Optional[pd.Series]:
    """The single pre-open feature row for a given TWSE date (for live / scoreboard
    prediction). Returns None if the date isn't covered or features are missing."""
    X, _, dates = build_dataset(years=years)
    ts = pd.Timestamp(target_date)
    if ts in dates:
        return X.loc[ts]
    # Fall back to the most recent covered date <= target (e.g. today before
    # ^TWII has printed) — still strictly-before features by construction.
    prior = dates[dates <= ts]
    if len(prior) == 0:
        return None
    return X.loc[prior[-1]]
