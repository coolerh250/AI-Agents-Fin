"""sentiment_curator_agent.py — Phase 3 §2 Day 6.

Stateless Haiku wrapper that turns the deterministic top-3 picks (already
written to section2_picks by scripts/section2_weekly.py) into a ~250–300
character Chinese narrative for the LINE report's §2 block.

  - Reads top-3 picks for `trade_week` + the most-recent
    chief_strategist gap_direction
  - One single-shot Anthropic SDK call against the sentiment_curator
    profile (Haiku, ~1024 max_tokens). Cost target < $0.005/run.
  - Writes the narrative back to section2_picks.narrative_text on the
    rank_in_week=1 row; Day 7's section2_loader_node reads it from there.

CLI:
    uv run python sentiment_curator_agent.py            # current upcoming-Mon week
    uv run python sentiment_curator_agent.py --week 2026-06-08
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from typing import Optional

from dotenv import load_dotenv
from loguru import logger
from sqlalchemy import text

load_dotenv()


# Last-resort prompt if agent_strategy_profiles is unreachable. Kept in sync
# with strategy_profile._SENTIMENT_CURATOR_SYSTEM (the seed) — duplicated here
# so the curator can run even on a DB outage and so the file is self-contained
# when audited.
_FALLBACK_PROMPT = (
    "你是台股聲量篩股敘事 Agent，將 Section 2 週篩結果（前 3 名候選個股 + 大盤方向）"
    "轉成適合 LINE 推播的繁體中文敘事。每檔以「【代碼 股名】」開頭，三段共約 250–300 字；"
    "每檔一行說明聲量來源 + 技術訊號（命中 N/3），一行寫進場區間、停損、目標；"
    "結尾以「整體配合大盤 {gap_direction} 方向」收束。不加 emoji、不加分隔線、不引述未提供的事實。"
)


def _upcoming_monday(d: date) -> date:
    """Mirror section2_weekly.upcoming_monday so default week matches."""
    days_until_mon = (0 - d.weekday()) % 7
    if days_until_mon == 0 and d.weekday() == 0:
        return d
    return d + timedelta(days=days_until_mon or 7)


def _load_week_payload(week_start: date) -> Optional[dict]:
    """Read top-3 picks + current gap_direction. Returns None if no picks
    exist for the week (curator skips silently in that case)."""
    from database_tools import _engine
    with _engine().connect() as conn:
        picks_rows = conn.execute(
            text("""
                SELECT rank_in_week, stock_id, stock_name, composite_score,
                       signal_score, signals_json, direction_match,
                       entry_band_low, entry_band_high, stop_loss, target_price,
                       selection_reason
                FROM section2_picks WHERE trade_week = :w
                ORDER BY rank_in_week
            """),
            {"w": week_start},
        ).fetchall()
        if not picks_rows:
            return None
        gap_row = conn.execute(text(
            "SELECT gap_direction FROM daily_briefs ORDER BY trade_date DESC LIMIT 1"
        )).fetchone()
    gap_dir = (gap_row[0] if gap_row else None) or "flat"

    picks = []
    for r in picks_rows:
        try:
            signals = json.loads(r[5]) if isinstance(r[5], str) else (r[5] or {})
        except (TypeError, ValueError):
            signals = {}
        picks.append({
            "rank":             int(r[0]),
            "stock_id":         r[1],
            "stock_name":       r[2] or "",
            "composite_score":  float(r[3]) if r[3] is not None else 0.0,
            "signal_score":     int(r[4] or 0),
            "signals":          signals,
            "entry_band_low":   float(r[7])  if r[7]  is not None else None,
            "entry_band_high":  float(r[8])  if r[8]  is not None else None,
            "stop_loss":        float(r[9])  if r[9]  is not None else None,
            "target_price":     float(r[10]) if r[10] is not None else None,
            "selection_reason": r[11],
        })
    return {"gap_direction": gap_dir, "picks": picks}


def _persist_narrative(week_start: date, narrative: str) -> None:
    """Save the full narrative on the rank_in_week=1 row for the week.
    Day 7's loader reads from there. Other rows keep narrative_text NULL."""
    from database_tools import _engine
    with _engine().begin() as conn:
        conn.execute(
            text("""
                UPDATE section2_picks
                SET narrative_text = :n
                WHERE trade_week = :w AND rank_in_week = 1
            """),
            {"n": narrative, "w": week_start},
        )


def run_curator(week_start: Optional[date] = None,
                run_id: Optional[str] = None) -> int:
    """Compose + persist the §2 narrative for `week_start`. Returns 0 on
    success, 1 on miss/error (caller treats nonzero as 'use placeholder')."""
    import anthropic

    if week_start is None:
        week_start = _upcoming_monday(date.today())

    payload = _load_week_payload(week_start)
    if payload is None:
        logger.warning(f"[curator] no picks for week {week_start} — skip")
        return 1

    from strategy_profile import load_profile_or_fallback
    from agent_runtime import _PRICING
    from telemetry import record_usage

    profile = load_profile_or_fallback(
        "sentiment_curator",
        fallback_prompt=_FALLBACK_PROMPT,
        fallback_params={},
        fallback_tools=[],
        fallback_model="claude-haiku-4-5-20251001",
        fallback_max_tokens=1024,
        run_id=run_id,
    )

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.error("[curator] ANTHROPIC_API_KEY not set — abort")
        return 1
    client = anthropic.Anthropic(api_key=api_key)

    user_message = json.dumps(payload, ensure_ascii=False, indent=2)
    start = time.monotonic()
    try:
        response = client.messages.create(
            model=profile.model_name,
            max_tokens=profile.max_tokens,
            system=profile.system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
    except Exception as exc:
        logger.error(f"[curator] messages.create failed: {exc}")
        return 1
    latency_ms = int((time.monotonic() - start) * 1000)

    text_blocks = [b.text for b in response.content
                   if getattr(b, "type", None) == "text"]
    narrative = "\n".join(t for t in text_blocks if t).strip()
    if not narrative:
        logger.error("[curator] LLM returned empty narrative — skip persist")
        return 1

    try:
        record_usage(
            agent_name="sentiment_curator",
            model=profile.model_name,
            response=response,
            latency_ms=latency_ms,
            run_id=run_id,
            system_prompt=profile.system_prompt,
            user_content=user_message,
            pricing=_PRICING,
        )
    except Exception as exc:
        logger.debug(f"[curator] record_usage failed (non-fatal): {exc}")

    _persist_narrative(week_start, narrative)
    in_t  = int(getattr(response.usage, "input_tokens",  0) or 0)
    out_t = int(getattr(response.usage, "output_tokens", 0) or 0)
    p     = _PRICING.get(profile.model_name, {"input": 0.0, "output": 0.0})
    cost  = (in_t * p["input"] + out_t * p["output"]) / 1_000_000
    logger.success(f"[curator] week={week_start} narrative={len(narrative)} chars "
                   f"tokens={in_t}/{out_t} cost=${cost:.4f} latency={latency_ms}ms")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 3 §2 Day 6 — Haiku narrative wrapper")
    parser.add_argument("--week", type=str, default=None,
                        help="trade_week (Mon YYYY-MM-DD); default = upcoming Monday")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stdout, format="{time:HH:mm:ss} | {level:<8} | {message}",
               level="INFO", colorize=False)

    if args.week:
        week_start = datetime.fromisoformat(args.week).date()
    else:
        week_start = None
    return run_curator(week_start)


if __name__ == "__main__":
    sys.exit(main())
