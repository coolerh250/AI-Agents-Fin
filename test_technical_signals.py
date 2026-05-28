"""Pytest fixtures for technical_signals.py — synthetic OHLCV series.

Each test constructs a hand-rolled price/volume curve where the desired
signal answer is unambiguous, then asserts the function agrees.
"""
import pandas as pd

from technical_signals import (
    signal_ma_bull,
    signal_macd_golden,
    signal_breakout_20d,
    evaluate_all,
)


def _s(values):
    return pd.Series(values, dtype=float)


# ── signal_ma_bull ────────────────────────────────────────────────────────────

def test_ma_bull_ascending_trend():
    """Monotonically rising 60 bars → MA5 > MA20 > MA60 by construction."""
    assert signal_ma_bull(_s(list(range(1, 65)))) is True


def test_ma_bull_descending_trend_false():
    assert signal_ma_bull(_s(list(range(60, 0, -1)))) is False


def test_ma_bull_flat_false():
    # MA5 == MA20 == MA60 — strict inequality fails.
    assert signal_ma_bull(_s([100.0] * 60)) is False


def test_ma_bull_insufficient_data_false():
    assert signal_ma_bull(_s([100.0] * 30)) is False


# ── signal_macd_golden ────────────────────────────────────────────────────────

def test_macd_golden_recent_breakout():
    """Long flat 55 bars then 5-bar sharp rally — golden cross at bar 56,
    within lookback=5."""
    closes = _s([100.0] * 55 + [100, 105, 110, 115, 120])
    result = signal_macd_golden(closes, lookback=5)
    assert result is not None and result.startswith("golden_")


def test_macd_no_cross_when_flat():
    """All-flat → DIF == sig == 0; strict > fails so no cross detected."""
    assert signal_macd_golden(_s([100.0] * 60)) is None


def test_macd_no_cross_when_descending():
    """Pure downtrend keeps DIF below sig — no golden cross."""
    closes = _s([100 - i for i in range(60)])
    assert signal_macd_golden(closes) is None


def test_macd_insufficient_data_none():
    assert signal_macd_golden(_s([100, 101, 102, 103])) is None


# ── signal_breakout_20d ───────────────────────────────────────────────────────

def test_breakout_new_high_with_volume_true():
    closes  = _s([100] * 19 + [110])
    volumes = _s([1000] * 19 + [1500])  # 1.5× baseline
    assert signal_breakout_20d(closes, volumes) is True


def test_breakout_new_high_no_volume_false():
    """Breakout level achieved but volume not above 1.2× baseline."""
    closes  = _s([100] * 19 + [110])
    volumes = _s([1000] * 20)  # last_vol == baseline, not > 1.2×
    assert signal_breakout_20d(closes, volumes) is False


def test_breakout_not_at_high_false():
    """Today's close 105 < window high 110 — fails despite high volume."""
    closes  = _s([95, 102, 110, 108] + [100] * 15 + [105])
    volumes = _s([1500] * 20)
    assert signal_breakout_20d(closes, volumes) is False


def test_breakout_insufficient_data_false():
    assert signal_breakout_20d(_s([100, 101]), _s([1000, 1100])) is False


def test_breakout_zero_baseline_volume_false():
    """If baseline volume is 0 (delisted-style data) we cannot compute the
    1.2× threshold safely → reject."""
    closes  = _s([100] * 19 + [110])
    volumes = _s([0] * 19 + [1000])
    assert signal_breakout_20d(closes, volumes) is False


# ── evaluate_all aggregator ───────────────────────────────────────────────────

def test_evaluate_all_returns_score_field():
    """Strong-up trend with breakout volume → all three signals expected true."""
    closes  = _s([100 + i * 0.5 for i in range(60)] + [200])
    volumes = _s([1000] * 60 + [5000])
    result  = evaluate_all(closes, volumes)
    assert set(result) == {"ma_bull", "macd_golden", "breakout_20d", "score"}
    assert isinstance(result["score"], int)
    assert 0 <= result["score"] <= 3
