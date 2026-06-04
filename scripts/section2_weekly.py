#!/usr/bin/env python
"""scripts/section2_weekly.py — Phase 3 §2 weekly orchestration.

Sunday cron (30 19 * * 6 UTC = Sun 03:30 Taipei) runs this end-to-end:
  1. Refresh CNYES + TWSE top-volume + PTT collectors over the past week
  2. Composite sentiment top-N (sentiment_scorer; default N=10)
  3. Per-candidate 90-day OHLCV via yfinance
  4. Score each with the three technical indicators (technical_signals)
  5. Pull the most recent chief_strategist gap_direction as the tiebreak
  6. Pick top-3 by (signal_score, direction-aligned signals, composite)
  7. Compute entry-band / stop / target with simple ±1% / -5% / +10% rules
  8. UPSERT into section2_picks (replaces this week's rows if re-run)
  9. Day 6 sentiment_curator narrative is invoked separately

CLI:
    uv run python scripts/section2_weekly.py            # full run
    uv run python scripts/section2_weekly.py --dry-run  # skip collectors + UPSERT
"""
import argparse
import json
import os
import sys
import time
from datetime import date, timedelta

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from dotenv import load_dotenv
from loguru import logger
from sqlalchemy import text

load_dotenv()


# ── Helpers ───────────────────────────────────────────────────────────────────

def upcoming_monday(d: date) -> date:
    """Monday of the trading week these picks should apply to.

    Cron fires Sat 19:30 UTC (= Sun 03:30 Taipei) — `date.today()` in UTC is
    Saturday at that moment, so `this week's Monday` would point at the past
    week. Always project forward to the next Monday-on-or-after `d`."""
    days_until_mon = (0 - d.weekday()) % 7
    if days_until_mon == 0 and d.weekday() == 0:
        return d                                    # already Mon → keep
    return d + timedelta(days=days_until_mon or 7)  # else next Mon


def _fetch_ohlcv(stock_id: str, period: str = "90d"):
    """Single-ticker yfinance fetch. Returns (closes: pd.Series, volumes: pd.Series)
    or (None, None) on error / no data. Day 8 will replace this with a
    cache-backed reader sitting on ohlcv_daily_cache."""
    try:
        import yfinance as yf
        df = yf.Ticker(f"{stock_id}.TW").history(period=period)
        if df.empty:
            return None, None
        return df["Close"], df["Volume"]
    except Exception as exc:
        logger.warning(f"[ohlcv] {stock_id} fetch failed: {exc}")
        return None, None


def _current_gap_direction() -> str:
    """Latest chief_strategist gap_direction; defaults to 'flat' if no brief."""
    from database_tools import _engine
    with _engine().connect() as conn:
        row = conn.execute(text(
            "SELECT gap_direction FROM daily_briefs "
            "ORDER BY trade_date DESC LIMIT 1"
        )).fetchone()
    return (row[0] if row else None) or "flat"


def _direction_alignment(signals: dict, gap_direction: str) -> int:
    """Sort-tiebreak score: how many of the 3 signals point the same way as the
    chief_strategist's gap_direction. ma_bull and breakout_20d are bull cues;
    macd_golden (cross-up) is also bull. So this function is symmetric: it
    returns +N when gap is up, -N when gap is down, 0 when flat (so flat picks
    fall back to composite score for tiebreak)."""
    bull = int(bool(signals.get("ma_bull"))) \
         + int(bool(signals.get("macd_golden"))) \
         + int(bool(signals.get("breakout_20d")))
    if gap_direction == "up":   return bull
    if gap_direction == "down": return -bull
    return 0


def _pick_best_three(scored: list[dict], gap_direction: str) -> list[dict]:
    return sorted(
        scored,
        key=lambda x: (
            -x["signal_score"],
            -_direction_alignment(x["signals"], gap_direction),
            -x["composite_score"],
        ),
    )[:3]


def _entry_stop_target(last_close: float) -> dict:
    """Symmetric ±1% entry band, -5% hard stop, +10% target. Day 8 will
    replace these with backtest-derived levels per-indicator."""
    return {
        "entry_band_low":  round(last_close * 0.99, 2),
        "entry_band_high": round(last_close * 1.01, 2),
        "stop_loss":       round(last_close * 0.95, 2),
        "target_price":    round(last_close * 1.10, 2),
    }


def _selection_reason(signals: dict) -> str:
    parts = []
    if signals.get("ma_bull"):      parts.append("均線多頭排列")
    if signals.get("macd_golden"):  parts.append(f"MACD {signals['macd_golden']}")
    if signals.get("breakout_20d"): parts.append("20日新高突破")
    return " + ".join(parts) if parts else "聲量上位（無顯著技術訊號）"


def _upsert_picks(week_start: date, picks: list[dict]) -> int:
    """Clear this week's prior picks, then INSERT the new ranking. Idempotent
    on re-run (last-writer-wins for the same trade_week)."""
    from database_tools import _engine
    with _engine().begin() as conn:
        conn.execute(text("DELETE FROM section2_picks WHERE trade_week=:w"),
                     {"w": week_start})
        for rank, p in enumerate(picks, 1):
            conn.execute(text("""
                INSERT INTO section2_picks
                  (trade_week, rank_in_week, stock_id, stock_name,
                   composite_score, signal_score, signals_json, direction_match,
                   entry_band_low, entry_band_high, stop_loss, target_price,
                   selection_reason)
                VALUES (:w, :r, :sid, :sn, :cs, :ss, :sj, :dm,
                        :ebl, :ebh, :sl, :tp, :sr)
            """), {
                "w": week_start, "r": rank,
                "sid": p["stock_id"], "sn": p.get("stock_name"),
                "cs": p["composite_score"], "ss": p["signal_score"],
                "sj": json.dumps(p["signals"], ensure_ascii=False, default=str),
                "dm": p.get("direction_match"),
                "ebl": p["entry_band_low"], "ebh": p["entry_band_high"],
                "sl":  p["stop_loss"],      "tp":  p["target_price"],
                "sr":  p.get("selection_reason", "")[:200],
            })
    return len(picks)


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run_weekly(dry_run: bool = False, top_n: int = 10) -> int:
    week_start = upcoming_monday(date.today())
    logger.info(f"[section2] weekly run for week {week_start} (dry_run={dry_run})")

    # Step 1: collectors (skip in dry-run — they touch live HTTP)
    if not dry_run:
        from sentiment_collectors import (collect_cnyes_mentions,
                                          collect_twse_top_volume,
                                          collect_ptt_stock_buzz)
        collect_cnyes_mentions(days=7)
        collect_twse_top_volume(top_n=30)
        collect_ptt_stock_buzz(pages=5)

    # Step 2: composite top-N
    from sentiment_scorer import compute_top_n_composite
    top = compute_top_n_composite(n=top_n, lookback_days=7)
    if not top:
        logger.error("[section2] composite pool empty — abort")
        return 1
    logger.info(f"[section2] top-{len(top)} composites: "
                + ", ".join(f"{s}({c:.2f})" for s, c in top))

    # Steps 3-4: per-candidate OHLCV + technical signals
    from technical_signals import evaluate_all
    from database_tools import get_stock_name
    scored: list[dict] = []
    for sid, composite_score in top:
        time.sleep(0.3)
        closes, volumes = _fetch_ohlcv(sid)
        if closes is None or len(closes) < 60:
            logger.info(f"[section2] {sid}: insufficient OHLCV (<60 bars) — skip")
            continue
        sig = evaluate_all(closes, volumes)
        try:
            sname = get_stock_name(sid)
        except Exception:
            sname = None
        scored.append({
            "stock_id":        sid,
            "stock_name":      sname,
            "composite_score": float(composite_score),
            "signal_score":    int(sig["score"]),
            "signals": {
                "ma_bull":      bool(sig["ma_bull"]),
                "macd_golden":  sig["macd_golden"],  # str or None
                "breakout_20d": bool(sig["breakout_20d"]),
            },
            "last_close":      float(closes.iloc[-1]),
        })

    if not scored:
        logger.error("[section2] no candidates survived OHLCV step — abort")
        return 1

    # Step 5: chief_strategist gap_direction
    gap_dir = _current_gap_direction()
    logger.info(f"[section2] tiebreak gap_direction={gap_dir}")

    # Step 6: top-3
    top3 = _pick_best_three(scored, gap_dir)

    # Step 7: entry/stop/target + reason
    final = []
    for p in top3:
        final.append({
            **p,
            **_entry_stop_target(p["last_close"]),
            "direction_match":  gap_dir,
            "selection_reason": _selection_reason(p["signals"]),
        })

    # Step 8 (display always; persist unless dry-run)
    print()
    print(f"=== Section 2 picks for week of {week_start} ===")
    for i, p in enumerate(final, 1):
        print(f"  #{i} {p['stock_id']} {p.get('stock_name') or '(no name)'}")
        print(f"     composite={p['composite_score']:.3f}  "
              f"signal_score={p['signal_score']}/3  "
              f"signals={p['signals']}")
        print(f"     last_close={p['last_close']}  "
              f"entry=[{p['entry_band_low']}, {p['entry_band_high']}]  "
              f"stop={p['stop_loss']}  target={p['target_price']}")
        print(f"     reason: {p['selection_reason']}")
        print()

    if dry_run:
        logger.info("[section2] --dry-run — skip UPSERT")
        return 0
    n = _upsert_picks(week_start, final)
    logger.success(f"[section2] persisted {n} picks for week {week_start}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 3 §2 weekly orchestration")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip collector refresh and UPSERT; print picks only")
    parser.add_argument("--top-n", type=int, default=10,
                        help="Composite top-N candidate pool size (default 10)")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stdout, format="{time:HH:mm:ss} | {level:<8} | {message}",
               level="INFO", colorize=False)

    return run_weekly(dry_run=args.dry_run, top_n=args.top_n)


if __name__ == "__main__":
    sys.exit(main())
