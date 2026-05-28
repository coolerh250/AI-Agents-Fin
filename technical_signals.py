"""Technical-signal functions for Phase 3 §2 weekly screening.

Three pure-pandas functions, no IO. Used by scripts/section2_weekly.py to
score sentiment-shortlisted candidates against historically winrate-tracked
indicator combos. Backtest of these same functions over 1-year OHLCV is
done by scripts/section2_backtest.py (Day 8) and lands in the
backtest_indicator_winrate table.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd


def signal_ma_bull(closes: pd.Series) -> bool:
    """均線多頭排列 + 站上 MA20.

    True when MA5 > MA20 > MA60 AND last close > MA20. Requires ≥60 bars;
    returns False on insufficient data."""
    if len(closes) < 60:
        return False
    ma5  = float(closes.iloc[-5:].mean())
    ma20 = float(closes.iloc[-20:].mean())
    ma60 = float(closes.iloc[-60:].mean())
    return ma5 > ma20 > ma60 and float(closes.iloc[-1]) > ma20


def signal_macd_golden(closes: pd.Series,
                       fast: int = 12, slow: int = 26, signal: int = 9,
                       lookback: int = 3) -> Optional[str]:
    """MACD 黃金交叉 within the last `lookback` trading days.

    Cross-up = DIF (= EMA_fast - EMA_slow) crosses above its 9-EMA signal
    line. Returns 'golden_t-0' (today), 't-1', or 't-2' for the offset of
    the most recent cross, or None if no cross in the window.

    Requires len(closes) ≥ slow + signal + lookback to avoid spurious
    crosses from EMA warmup."""
    min_bars = slow + signal + lookback
    if len(closes) < min_bars:
        return None
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    sig = dif.ewm(span=signal, adjust=False).mean()
    for offset in range(lookback):
        i = -1 - offset
        # Strict > on the "today" side and <= on "yesterday" — handles ties
        # cleanly without double-counting a cross that already triggered.
        if dif.iloc[i] > sig.iloc[i] and dif.iloc[i - 1] <= sig.iloc[i - 1]:
            return f"golden_t-{offset}"
    return None


def signal_breakout_20d(closes: pd.Series, volumes: pd.Series) -> bool:
    """20 日新高突破 + 量增確認.

    True when last close >= max(last 20 closes) AND last volume > 1.2 ×
    mean(last 20 volumes, excluding today). Volume confirmation rejects
    soft / illiquid breakouts. Returns False on < 20 bars."""
    if len(closes) < 20 or len(volumes) < 20:
        return False
    last_close = float(closes.iloc[-1])
    high_20    = float(closes.iloc[-20:].max())
    # Exclude today's bar from the baseline so the comparison is
    # "today vs the 19 days preceding it" — symmetric with breakout logic.
    baseline_vol = float(volumes.iloc[-20:-1].mean())
    last_vol     = float(volumes.iloc[-1])
    if baseline_vol <= 0:
        return False
    return last_close >= high_20 and last_vol > baseline_vol * 1.2


def evaluate_all(closes: pd.Series, volumes: pd.Series) -> dict:
    """Run the three signals on one stock's OHLCV. Returns a flat dict
    used by section2_weekly: {ma_bull, macd_golden, breakout_20d, score}
    where score is the count of true signals (macd counts as 1 if any
    'golden_t-*' offset)."""
    sigs = {
        "ma_bull":      signal_ma_bull(closes),
        "macd_golden":  signal_macd_golden(closes),
        "breakout_20d": signal_breakout_20d(closes, volumes),
    }
    score = sum(1 for v in sigs.values() if v)
    return {**sigs, "score": score}
