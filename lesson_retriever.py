"""
lesson_retriever.py
Adaptive Flywheel Phase 1 — retrieve regime-matched strategy lessons for chief_strategist.
No LLM calls. Returns a compact context string (≤ max_chars).
"""
from typing import Optional

from loguru import logger


def _compute_regime(raw_market_data: dict) -> dict:
    """Derive regime tags from raw_market_data (same thresholds as log_session_episode)."""
    sox = raw_market_data.get("sox_chg_pct")
    foreign = raw_market_data.get("foreign_oi_net")
    div = raw_market_data.get("divergence_signal")

    if sox is None:
        regime_sox = "neutral"
    else:
        try:
            v = float(sox)
            regime_sox = "strong" if v > 1.0 else ("weak" if v < -1.0 else "neutral")
        except (TypeError, ValueError):
            regime_sox = "neutral"

    if foreign is None:
        regime_foreign_oi = "neutral"
    else:
        try:
            v = float(foreign)
            regime_foreign_oi = "bearish" if v < -10000 else ("bullish" if v > 0 else "neutral")
        except (TypeError, ValueError):
            regime_foreign_oi = "neutral"

    divergence_signal: Optional[int] = None
    if div is not None:
        try:
            divergence_signal = int(bool(div))
        except (TypeError, ValueError):
            pass

    return {
        "regime_sox": regime_sox,
        "regime_foreign_oi": regime_foreign_oi,
        "divergence_signal": divergence_signal,
    }


def _format_lesson(row: dict, max_lesson_chars: int = 200) -> str:
    """Format one lesson row as a bullet line."""
    dt = str(row.get("trade_date", ""))[:10]
    etype = row.get("error_type", "unknown")
    correct = row.get("direction_correct", 0)
    mark = "✓" if correct else "✗"

    label_map = {
        "direction_error": "direction",
        "overconfidence_error": "overconfidence",
        "magnitude_error": "magnitude",
        "missing_data_error": "missing_data",
        "stale_snapshot_error": "stale_snapshot",
        "correct_prediction": "correct",
    }
    label = label_map.get(etype, etype)

    lesson = (row.get("lesson_text") or "").replace("\n", " ").strip()
    if len(lesson) > max_lesson_chars:
        lesson = lesson[:max_lesson_chars] + "…"

    return f"● [{dt}] {label} {mark}: {lesson}"


def get_lesson_context(
    raw_market_data: dict,
    limit: int = 3,
    max_chars: int = 600,
) -> str:
    """
    Return a compact context string of regime-matched past lessons.
    Returns "" when no lessons exist or DB is unavailable.
    """
    try:
        regime = _compute_regime(raw_market_data)
        regime_sox = regime["regime_sox"]
        regime_foreign_oi = regime["regime_foreign_oi"]
        divergence_signal = regime["divergence_signal"]

        from database_tools import get_relevant_lessons
        lessons = get_relevant_lessons(
            regime_sox=regime_sox,
            regime_foreign_oi=regime_foreign_oi,
            divergence_signal=divergence_signal,
            limit=limit,
        )

        if not lessons:
            return ""

        header = f"【歷史策略教訓（SOX {regime_sox} / 外資 {regime_foreign_oi}）】"
        lines = [header]
        for row in lessons:
            lines.append(_format_lesson(row))

        result = "\n".join(lines)
        if len(result) > max_chars:
            result = result[:max_chars].rsplit("\n", 1)[0]
        return result

    except Exception as exc:
        logger.debug(f"[LessonRetriever] get_lesson_context 失敗（略過）: {exc}")
        return ""
