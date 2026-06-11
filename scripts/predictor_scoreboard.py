#!/usr/bin/env python
"""scripts/predictor_scoreboard.py — Prediction Step 1.

Runs the baseline predictors (predictors.py) over session_episodes features and
writes each day's prediction to predictor_predictions, then scores everyone
against actuals with hit-rate + Brier (database_tools.get_predictor_leaderboard).

Because session_episodes already stores per-day features AND backfilled actuals,
`backfill` replays the entire history at once — the leaderboard is answerable
immediately, no weeks of waiting.

Predictors written:
  unconditional_majority · naive_nightfutures · weighted_rule · llm_tech_analyst

CLI:
  uv run python scripts/predictor_scoreboard.py backfill        # replay all history
  uv run python scripts/predictor_scoreboard.py append --date today
  uv run python scripts/predictor_scoreboard.py append --date 2026-06-11
  uv run python scripts/predictor_scoreboard.py score --days 60 # print leaderboard
"""
import argparse
import os
import sys
from datetime import date, datetime
from typing import Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from dotenv import load_dotenv
from loguru import logger
from sqlalchemy import text

import predictors as P

load_dotenv()

_MAJORITY_WINDOW = 60   # trailing trading days for the majority null hypothesis


# ── Feature loading ────────────────────────────────────────────────────────────

def _feat_from_row(row) -> dict:
    """Build a predictor feature dict from a session_episodes row mapping.
    DB returns Decimal/None; predictors cast to float / handle None."""
    return {
        "sox_chg_pct":           row["sox_chg_pct"],
        "ndx_chg_pct":           row["ndx_chg_pct"],
        "tsm_adr_chg_pct":       row["tsm_adr_chg_pct"],
        "djia_chg_pct":          row["djia_chg_pct"],
        "night_futures_chg_pct": row["night_futures_chg_pct"],
    }


def _load_episodes() -> list[dict]:
    """All session_episodes ordered by trade_date asc, as plain dicts."""
    from database_tools import _engine
    with _engine().connect() as conn:
        rows = conn.execute(text("""
            SELECT trade_date, predicted_direction, predicted_gap_pct,
                   actual_direction,
                   sox_chg_pct, ndx_chg_pct, tsm_adr_chg_pct, djia_chg_pct,
                   night_futures_chg_pct
            FROM session_episodes
            ORDER BY trade_date ASC
        """)).fetchall()
    return [dict(r._mapping) for r in rows]


# ── Writing predictions ────────────────────────────────────────────────────────

def _upsert_prediction(conn, trade_date, name: str, direction: str,
                       gap: Optional[float], probs: tuple) -> None:
    conn.execute(text("""
        INSERT INTO predictor_predictions
            (trade_date, predictor_name, pred_direction, pred_gap_pct,
             prob_up, prob_flat, prob_down)
        VALUES (:td, :name, :dir, :gap, :pu, :pf, :pd)
        ON DUPLICATE KEY UPDATE
            pred_direction = VALUES(pred_direction),
            pred_gap_pct   = VALUES(pred_gap_pct),
            prob_up        = VALUES(prob_up),
            prob_flat      = VALUES(prob_flat),
            prob_down      = VALUES(prob_down)
    """), {"td": trade_date, "name": name, "dir": direction,
           "gap": gap, "pu": probs[0], "pf": probs[1], "pd": probs[2]})


def _predictions_for_day(feat: dict, trailing_actuals: list[str],
                         llm_dir: Optional[str], llm_gap) -> list[tuple]:
    """Return [(predictor_name, direction, gap_pct, probs), ...] for one day.
    The LLM entry is omitted when no stored direction exists for that day."""
    out = [
        ("unconditional_majority",
         *P.predict_unconditional_majority(trailing_actuals)),
        ("naive_nightfutures",
         *P.predict_naive_nightfutures(feat)),
        ("weighted_rule",
         *P.predict_weighted_rule(feat)),
    ]
    if llm_dir:
        gap = float(llm_gap) if llm_gap is not None else None
        out.append(("llm_tech_analyst", llm_dir, gap,
                    P.llm_probs(gap)))
    return out


# ── Commands ───────────────────────────────────────────────────────────────────

def cmd_backfill() -> int:
    """Replay all history. Walk-forward: each day's majority uses only the
    trailing actuals strictly before it."""
    from database_tools import _engine, ensure_predictor_predictions_table
    ensure_predictor_predictions_table()

    episodes = _load_episodes()
    if not episodes:
        logger.error("[scoreboard] no session_episodes rows — nothing to backfill")
        return 1

    trailing: list[str] = []     # actual_directions seen so far (walk-forward)
    written = 0
    with _engine().begin() as conn:
        for ep in episodes:
            td = ep["trade_date"]
            feat = _feat_from_row(ep)
            window = trailing[-_MAJORITY_WINDOW:]
            for name, direction, gap, probs in _predictions_for_day(
                feat, window, ep["predicted_direction"], ep["predicted_gap_pct"]
            ):
                _upsert_prediction(conn, td, name, direction, gap, probs)
                written += 1
            # only AFTER predicting do we admit this day's actual into the window
            if ep["actual_direction"]:
                trailing.append(ep["actual_direction"])

    logger.success(f"[scoreboard] backfill wrote {written} predictions "
                   f"over {len(episodes)} trading days")
    return 0


def cmd_append(day: date) -> int:
    """Append predictions for a single trade_date from its stored features."""
    from database_tools import _engine, ensure_predictor_predictions_table
    ensure_predictor_predictions_table()

    with _engine().connect() as conn:
        ep_row = conn.execute(text("""
            SELECT trade_date, predicted_direction, predicted_gap_pct,
                   sox_chg_pct, ndx_chg_pct, tsm_adr_chg_pct, djia_chg_pct,
                   night_futures_chg_pct
            FROM session_episodes WHERE trade_date = :td
        """), {"td": day}).fetchone()
        if ep_row is None:
            logger.warning(f"[scoreboard] no session_episodes for {day} — skip "
                           f"(workflow may not have run yet)")
            return 1
        ep = dict(ep_row._mapping)
        trailing = [r[0] for r in conn.execute(text("""
            SELECT actual_direction FROM session_episodes
            WHERE trade_date < :td AND actual_direction IS NOT NULL
            ORDER BY trade_date DESC LIMIT :w
        """), {"td": day, "w": _MAJORITY_WINDOW}).fetchall()][::-1]

    feat = _feat_from_row(ep)
    with _engine().begin() as conn:
        n = 0
        for name, direction, gap, probs in _predictions_for_day(
            feat, trailing, ep["predicted_direction"], ep["predicted_gap_pct"]
        ):
            _upsert_prediction(conn, day, name, direction, gap, probs)
            n += 1
    logger.success(f"[scoreboard] appended {n} predictions for {day}")
    return 0


def cmd_score(days: int) -> int:
    from database_tools import get_predictor_leaderboard
    board = get_predictor_leaderboard(days)
    if not board:
        logger.error("[scoreboard] empty leaderboard — run backfill first, "
                     "and ensure market_actuals has matched dates")
        return 1

    # majority is the reference bar everyone must beat
    base = next((r for r in board if r["predictor_name"] == "unconditional_majority"), None)
    base_hr = base["hit_rate"] if base else None

    print(f"\n=== Predictor leaderboard (last {days} days) ===")
    print(f"{'predictor':<24} {'n':>4} {'hit_rate':>9} {'Δ vs base':>10} {'Brier':>8}")
    print("-" * 60)
    for r in board:
        hr = r["hit_rate"] * 100
        delta = (f"{(r['hit_rate'] - base_hr) * 100:+.1f}pp"
                 if base_hr is not None else "—")
        brier = f"{r['brier']:.4f}" if r["brier"] is not None else "—"
        flag = "  ← LLM" if r["predictor_name"] == "llm_tech_analyst" else ""
        print(f"{r['predictor_name']:<24} {r['n']:>4} {hr:>8.1f}% {delta:>10} {brier:>8}{flag}")
    print("-" * 60)
    print("Lower Brier = better calibration. Δ vs base = hit-rate edge over "
          "the unconditional-majority null.\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Prediction Step 1 scoreboard")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("backfill")
    ap = sub.add_parser("append")
    ap.add_argument("--date", default="today", help="trade_date or 'today'")
    sc = sub.add_parser("score")
    sc.add_argument("--days", type=int, default=60)
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stdout, format="{time:HH:mm:ss} | {level:<8} | {message}",
               level="INFO", colorize=False)

    if args.cmd == "backfill":
        return cmd_backfill()
    if args.cmd == "append":
        day = date.today() if args.date == "today" else \
            datetime.fromisoformat(args.date).date()
        return cmd_append(day)
    if args.cmd == "score":
        return cmd_score(args.days)
    return 2


if __name__ == "__main__":
    sys.exit(main())
