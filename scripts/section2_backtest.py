#!/usr/bin/env python
"""scripts/section2_backtest.py — Phase 3 §2 Day 8.

Backtest the three weekly-screen indicators (signal_ma_bull / signal_macd_golden
/ signal_breakout_20d) over ~1 year of OHLCV per stock, record winrate / mean /
median / stddev of t+5 forward returns into `backtest_indicator_winrate`, and
cache OHLCV in `ohlcv_daily_cache` so reruns don't re-hit yfinance.

Day 8 stays observe-only — these stats are NOT used by section2_weekly's pick
ranking yet. Phase 4 Optimizer will read this table to learn per-indicator
weights and drop low-winrate signals.

Stock-list resolution (in order):
  1. --sample 2330,2317,...   explicit comma list
  2. --from-picks N           DISTINCT stock_id from section2_picks last N weeks
  3. (default)                compute_top_n_composite(top_n, lookback_days=30)

CLI:
    uv run python scripts/section2_backtest.py --sample 2330,2317 --dry-run
    uv run python scripts/section2_backtest.py --top-n 50
    uv run python scripts/section2_backtest.py --from-picks 12
"""
import argparse
import os
import sys
import time
from datetime import date, timedelta
from typing import Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from loguru import logger
from sqlalchemy import text

load_dotenv()

_FWD_DAYS         = 5      # forward-return horizon (trading days)
_BATCH_SIZE       = 10
_BATCH_SLEEP_SEC  = 2.0
_PERIOD_DEFAULT   = "1y"


# ── OHLCV cache layer ─────────────────────────────────────────────────────────

def _read_cache(stock_id: str, start: date, end: date
                ) -> tuple[Optional[pd.Series], Optional[pd.Series]]:
    """Return (closes, volumes) from ohlcv_daily_cache for the range, or (None,
    None) if fewer than ~200 trading rows (treat as insufficient → refetch)."""
    from database_tools import _engine
    with _engine().connect() as conn:
        rows = conn.execute(
            text("""
                SELECT trade_date, close_px, volume
                FROM ohlcv_daily_cache
                WHERE stock_id = :s AND trade_date BETWEEN :start AND :end
                ORDER BY trade_date
            """),
            {"s": stock_id, "start": start, "end": end},
        ).fetchall()
    if len(rows) < 200:
        return None, None
    idx     = [r[0] for r in rows]
    closes  = pd.Series([float(r[1]) for r in rows], index=idx, name="Close")
    volumes = pd.Series([int(r[2] or 0) for r in rows], index=idx, name="Volume")
    return closes, volumes


def _write_cache(stock_id: str, df: pd.DataFrame) -> None:
    """UPSERT all bars in df (date index, OHLCV columns) to ohlcv_daily_cache."""
    from database_tools import _engine
    if df.empty:
        return
    payload = []
    for ts, row in df.iterrows():
        d = ts.date() if hasattr(ts, "date") else ts
        payload.append({
            "sid": stock_id, "d": d,
            "o":  float(row.get("Open",  0) or 0),
            "h":  float(row.get("High",  0) or 0),
            "l":  float(row.get("Low",   0) or 0),
            "c":  float(row.get("Close", 0) or 0),
            "v":  int(row.get("Volume",  0) or 0),
        })
    with _engine().begin() as conn:
        # TiDB compatible UPSERT — using VALUES(col) so executemany params bind
        # only once. ON DUPLICATE refreshes prices on re-fetch (e.g. yfinance
        # split adjustment). uq_ohlcv_stock_date enforces the natural key.
        conn.execute(text("""
            INSERT INTO ohlcv_daily_cache
              (stock_id, trade_date, open_px, high_px, low_px, close_px, volume)
            VALUES (:sid, :d, :o, :h, :l, :c, :v)
            ON DUPLICATE KEY UPDATE
              open_px=VALUES(open_px), high_px=VALUES(high_px),
              low_px=VALUES(low_px),   close_px=VALUES(close_px),
              volume=VALUES(volume),   fetched_at=CURRENT_TIMESTAMP
        """), payload)


def _fetch_ohlcv_cached(stock_id: str, period: str = _PERIOD_DEFAULT
                        ) -> tuple[Optional[pd.Series], Optional[pd.Series]]:
    """Cache-backed OHLCV: read cache first, fall back to yfinance on miss.

    On yfinance fetch we always re-upsert into cache so the next run for this
    stock_id is a single SELECT. Returns (None, None) on fetch error / empty."""
    end   = date.today()
    start = end - timedelta(days=400)            # ~1y of trading + slack

    closes, volumes = _read_cache(stock_id, start, end)
    if closes is not None:
        return closes, volumes

    try:
        import yfinance as yf
        df = yf.Ticker(f"{stock_id}.TW").history(period=period)
        if df.empty:
            return None, None
        _write_cache(stock_id, df)
        return df["Close"], df["Volume"]
    except Exception as exc:
        logger.warning(f"[ohlcv] {stock_id} fetch failed: {exc}")
        return None, None


# ── Per-indicator backtest ────────────────────────────────────────────────────

def _empty_stats() -> dict:
    return {"sample_count": 0, "winrate_pct": 0.0,
            "mean_fwd_ret": None, "median_fwd_ret": None, "stddev_fwd_ret": None}


def _summarize_fwd_returns(closes: pd.Series, trigger: pd.Series,
                           fwd_days: int = _FWD_DAYS) -> dict:
    """For each True in `trigger` (excluding the last `fwd_days` bars), compute
    close[t+fwd]/close[t] - 1. Aggregate to sample_count / winrate_pct / mean /
    median / stddev (all percentages where applicable)."""
    closes_arr  = closes.values
    trigger_arr = trigger.fillna(False).values
    n = len(closes_arr)
    returns: list[float] = []
    for i in range(n - fwd_days):
        if not trigger_arr[i]:
            continue
        base, fwd = closes_arr[i], closes_arr[i + fwd_days]
        if base <= 0 or np.isnan(base) or np.isnan(fwd):
            continue
        returns.append((fwd / base - 1.0) * 100.0)         # → percentage
    if not returns:
        return _empty_stats()
    arr = np.array(returns, dtype=float)
    # Clip to DECIMAL(7,3) range ±999.999 — defensive against split artifacts.
    def _clip(v: float) -> float:
        return max(-999.999, min(999.999, float(v)))
    return {
        "sample_count":   int(len(arr)),
        "winrate_pct":    round(float((arr > 0).sum()) / len(arr) * 100.0, 2),
        "mean_fwd_ret":   _clip(arr.mean()),
        "median_fwd_ret": _clip(np.median(arr)),
        "stddev_fwd_ret": _clip(arr.std(ddof=0)) if len(arr) > 1 else 0.0,
    }


def _backtest_ma_bull(closes: pd.Series) -> dict:
    """Trigger = first day in a bullish phase (MA5>MA20>MA60 & close>MA20,
    False→True transition). Pure t+5 forward return — no stop / target."""
    if len(closes) < 60 + _FWD_DAYS:
        return _empty_stats()
    ma5  = closes.rolling(5).mean()
    ma20 = closes.rolling(20).mean()
    ma60 = closes.rolling(60).mean()
    bull = (ma5 > ma20) & (ma20 > ma60) & (closes > ma20)
    trigger = bull & ~bull.shift(1, fill_value=False)
    return _summarize_fwd_returns(closes, trigger)


def _backtest_macd_golden(closes: pd.Series,
                          fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """Trigger = day DIF crosses above its 9-EMA signal line. Drops the first
    `slow + signal` warmup bars to avoid spurious early crosses."""
    if len(closes) < slow + signal + _FWD_DAYS:
        return _empty_stats()
    ema_f = closes.ewm(span=fast, adjust=False).mean()
    ema_s = closes.ewm(span=slow, adjust=False).mean()
    dif   = ema_f - ema_s
    sig   = dif.ewm(span=signal, adjust=False).mean()
    cross = (dif > sig) & (dif.shift(1) <= sig.shift(1))
    warmup = slow + signal
    cross.iloc[:warmup] = False
    return _summarize_fwd_returns(closes, cross)


def _backtest_breakout_20d(closes: pd.Series, volumes: pd.Series) -> dict:
    """Trigger = close[t] >= rolling-20 high AND volume[t] > 1.2 × mean(
    volume[t-19:t-1] excluding today). Mirrors signal_breakout_20d's baseline."""
    if len(closes) < 20 + _FWD_DAYS:
        return _empty_stats()
    high_20  = closes.rolling(20).max()
    base_vol = volumes.shift(1).rolling(19).mean()        # excludes today
    trigger  = (closes >= high_20) & (volumes > base_vol * 1.2)
    return _summarize_fwd_returns(closes, trigger)


# ── DB UPSERT ─────────────────────────────────────────────────────────────────

def _upsert_winrate(stock_id: str, indicator: str, period: str,
                    as_of: date, stats: dict) -> None:
    from database_tools import _engine
    with _engine().begin() as conn:
        conn.execute(text("""
            INSERT INTO backtest_indicator_winrate
              (stock_id, indicator_name, backtest_period, as_of_date,
               sample_count, winrate_pct, mean_fwd_ret, median_fwd_ret, stddev_fwd_ret)
            VALUES (:sid, :ind, :p, :asof, :n, :wr, :mean, :median, :std)
            ON DUPLICATE KEY UPDATE
              sample_count=:n, winrate_pct=:wr,
              mean_fwd_ret=:mean, median_fwd_ret=:median, stddev_fwd_ret=:std,
              created_at=CURRENT_TIMESTAMP
        """), {
            "sid":    stock_id, "ind": indicator, "p": period, "asof": as_of,
            "n":      stats["sample_count"],
            "wr":     stats["winrate_pct"],
            "mean":   stats["mean_fwd_ret"],
            "median": stats["median_fwd_ret"],
            "std":    stats["stddev_fwd_ret"],
        })


# ── Stock-list resolution ─────────────────────────────────────────────────────

def _resolve_stock_list(args) -> list[str]:
    if args.sample:
        return [s.strip() for s in args.sample.split(",") if s.strip()]
    if args.from_picks and args.from_picks > 0:
        from database_tools import _engine
        cutoff = date.today() - timedelta(weeks=args.from_picks)
        with _engine().connect() as conn:
            rows = conn.execute(text("""
                SELECT DISTINCT stock_id FROM section2_picks
                WHERE trade_week >= :cutoff
                ORDER BY stock_id
            """), {"cutoff": cutoff}).fetchall()
        return [r[0] for r in rows]
    # Default: composite top-N over 30 days
    from sentiment_scorer import compute_top_n_composite
    return [sid for sid, _ in compute_top_n_composite(n=args.top_n, lookback_days=30)]


# ── Pipeline ──────────────────────────────────────────────────────────────────

def run_backtest(stock_ids: list[str], period: str = _PERIOD_DEFAULT,
                 as_of: Optional[date] = None, dry_run: bool = False) -> dict:
    if as_of is None:
        as_of = date.today()
    written = 0
    failed: list[tuple[str, str]] = []
    by_indicator_avg: dict[str, list[float]] = {
        "signal_ma_bull": [], "signal_macd_golden": [], "signal_breakout_20d": [],
    }

    t0 = time.monotonic()
    for i, sid in enumerate(stock_ids):
        if i > 0 and i % _BATCH_SIZE == 0:
            logger.info(f"[backtest] batch {i//_BATCH_SIZE} done — sleep {_BATCH_SLEEP_SEC}s")
            time.sleep(_BATCH_SLEEP_SEC)
        closes, volumes = _fetch_ohlcv_cached(sid, period)
        if closes is None or len(closes) < 60:
            failed.append((sid, "no_data"))
            logger.info(f"[backtest] {sid}: insufficient OHLCV — skip")
            continue
        stats = {
            "signal_ma_bull":      _backtest_ma_bull(closes),
            "signal_macd_golden":  _backtest_macd_golden(closes),
            "signal_breakout_20d": _backtest_breakout_20d(closes, volumes),
        }
        for ind, s in stats.items():
            if s["sample_count"] > 0:
                by_indicator_avg[ind].append(s["winrate_pct"])
        if dry_run:
            print(f"  {sid}: "
                  + " | ".join(f"{ind.replace('signal_', '')}={s['sample_count']}n/"
                               f"{s['winrate_pct']:.1f}%" for ind, s in stats.items()))
        else:
            for ind, s in stats.items():
                _upsert_winrate(sid, ind, period, as_of, s)
                written += 1

    elapsed = time.monotonic() - t0
    summary = {
        "stocks_processed": len(stock_ids) - len(failed),
        "stocks_failed":    len(failed),
        "rows_written":     written,
        "elapsed_seconds":  round(elapsed, 1),
        "avg_winrate_pct":  {ind: round(sum(v) / len(v), 2) if v else None
                             for ind, v in by_indicator_avg.items()},
    }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 3 §2 Day 8 backtest")
    parser.add_argument("--top-n", type=int, default=50,
                        help="Top-N by composite (default 50)")
    parser.add_argument("--period", default=_PERIOD_DEFAULT,
                        help="yfinance period (default 1y)")
    parser.add_argument("--sample", default=None,
                        help="Comma-separated stock_ids (overrides --top-n / --from-picks)")
    parser.add_argument("--from-picks", type=int, default=0,
                        help="Use DISTINCT stock_ids from section2_picks last N weeks (>0 enables)")
    parser.add_argument("--as-of", default=None,
                        help="as_of_date (YYYY-MM-DD); default today")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print per-stock summary; skip UPSERT")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stdout, format="{time:HH:mm:ss} | {level:<8} | {message}",
               level="INFO", colorize=False)

    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()

    stock_ids = _resolve_stock_list(args)
    if not stock_ids:
        logger.error("[backtest] empty stock list — abort")
        return 1
    logger.info(f"[backtest] backtest {len(stock_ids)} stocks "
                f"period={args.period} as_of={as_of} dry_run={args.dry_run}")

    summary = run_backtest(stock_ids, period=args.period,
                           as_of=as_of, dry_run=args.dry_run)
    logger.success(f"[backtest] done — {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
