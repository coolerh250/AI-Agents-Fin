"""
lesson_writer.py
Adaptive Flywheel — extract, score, and persist strategy lessons from backtest reports.
Fail-silent. Called from backtest_agent.save_accuracy_node.
"""
import os
import re
from datetime import date
from typing import Optional

from loguru import logger


def extract_lesson_text(accuracy_report: str) -> str:
    """Extract 【反省與學習】 section text; fallback to full report."""
    m = re.search(r"【反省與學習】\s*(.+?)(?=【|$)", accuracy_report, re.DOTALL)
    return m.group(1).strip() if m else accuracy_report.strip()


def _get_snapshot_age(trade_date: date) -> Optional[int]:
    """Query workflow_runs for snapshot_age_seconds on trade_date. Returns None on failure."""
    try:
        from database_tools import _engine
        from sqlalchemy import text
        with _engine().connect() as conn:
            row = conn.execute(
                text("""
                    SELECT snapshot_age_seconds FROM workflow_runs
                    WHERE DATE(started_at) = :td AND run_type = 'investment'
                    ORDER BY started_at DESC LIMIT 1
                """),
                {"td": trade_date},
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else None
    except Exception:
        return None


def classify_error(
    direction_correct: int,
    predicted_gap_pct: Optional[float],
    actual_gap_pct: Optional[float],
    composite_score: Optional[float],
    snapshot_age_seconds: Optional[int],
    session_episode: Optional[dict],
) -> str:
    """
    Classify the backtest result into one error_type string.
    Priority: stale_snapshot > missing_data > overconfidence > direction > magnitude > correct
    """
    # 1. Stale snapshot: data older than 6 hours
    if snapshot_age_seconds is not None and snapshot_age_seconds > 21600:
        return "stale_snapshot_error"

    # 2. Missing data: ≥2 critical fields are None in session_episode
    if session_episode:
        critical = ["foreign_oi_net", "sox_chg_pct", "djia_chg_pct"]
        missing_count = sum(1 for f in critical if session_episode.get(f) is None)
        if missing_count >= 2:
            return "missing_data_error"

    gap_error_abs: Optional[float] = None
    if predicted_gap_pct is not None and actual_gap_pct is not None:
        gap_error_abs = abs(predicted_gap_pct - actual_gap_pct)

    # 3. Overconfidence: wrong direction with high composite score
    if direction_correct == 0 and composite_score is not None and composite_score > 75:
        return "overconfidence_error"

    # 4. Direction error (general)
    if direction_correct == 0:
        return "direction_error"

    # 5. Magnitude error: direction correct but gap error too large
    if gap_error_abs is not None and gap_error_abs > 1.0:
        return "magnitude_error"

    # 6. Correct prediction (only record if gap_error_abs > 0.5%)
    return "correct_prediction"


def _score_lesson(lesson_text: str) -> Optional[float]:
    """Call Haiku to rate lesson quality 1.0–5.0. Returns None on any failure."""
    try:
        from langchain_anthropic import ChatAnthropic
        llm = ChatAnthropic(
            model="claude-haiku-4-5-20251001",
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            max_tokens=20,
            temperature=0,
        )
        prompt = (
            "請為以下交易策略反省教訓評分（1–5 分，5 分最佳）。\n"
            "評分標準：具體性、可操作性、清晰度。\n"
            "只回覆一個數字（如 3.5），不加任何說明。\n\n"
            f"教訓內容：\n{lesson_text[:500]}"
        )
        result = llm.invoke(prompt).content.strip()
        m = re.search(r"[1-5](?:\.[0-9])?", result)
        if not m:
            return None
        return max(1.0, min(5.0, float(m.group())))
    except Exception as exc:
        logger.debug(f"[LessonWriter] lesson quality scoring failed: {exc}")
        return None


def write_lesson(
    trade_date: date,
    accuracy_report: str,
    direction_correct: int,
    predicted_direction: Optional[str],
    actual_direction: Optional[str],
    predicted_gap_pct: Optional[float],
    actual_gap_pct: Optional[float],
    composite_score: Optional[float],
    eval_run_id: Optional[int] = None,
) -> bool:
    """
    Extract lesson, classify error, look up regime, persist to strategy_lessons.
    Skips correct_prediction if gap_error_abs <= 0.5%. Fully fail-silent.
    Returns True on successful write, False otherwise.
    """
    try:
        lesson_text = extract_lesson_text(accuracy_report)
        if not lesson_text:
            logger.debug("[LessonWriter] 空白 lesson_text，略過")
            return False

        gap_error_abs: Optional[float] = None
        if predicted_gap_pct is not None and actual_gap_pct is not None:
            gap_error_abs = abs(predicted_gap_pct - actual_gap_pct)

        # Query supporting data
        snapshot_age = _get_snapshot_age(trade_date)

        session_episode: Optional[dict] = None
        try:
            from database_tools import get_session_episode
            session_episode = get_session_episode(trade_date)
        except Exception:
            pass

        error_type = classify_error(
            direction_correct=direction_correct,
            predicted_gap_pct=predicted_gap_pct,
            actual_gap_pct=actual_gap_pct,
            composite_score=composite_score,
            snapshot_age_seconds=snapshot_age,
            session_episode=session_episode,
        )

        # Skip near-perfect correct predictions
        if error_type == "correct_prediction" and (gap_error_abs is None or gap_error_abs <= 0.5):
            logger.debug(f"[LessonWriter] 完美預測（誤差 {gap_error_abs}%），略過寫入")
            return False

        # Extract regime tags from session_episode
        regime_sox: Optional[str] = None
        regime_foreign_oi: Optional[str] = None
        divergence_signal: Optional[int] = None
        if session_episode:
            regime_sox = session_episode.get("regime_sox")
            regime_foreign_oi = session_episode.get("regime_foreign_oi")
            div = session_episode.get("divergence_signal")
            divergence_signal = int(div) if div is not None else None

        quality_score = _score_lesson(lesson_text)
        if quality_score is not None:
            logger.debug(f"[LessonWriter] 教訓品質分：{quality_score}/5")

        from database_tools import save_strategy_lesson
        save_strategy_lesson(
            trade_date=trade_date,
            error_type=error_type,
            lesson_text=lesson_text,
            direction_correct=direction_correct,
            predicted_direction=predicted_direction,
            actual_direction=actual_direction,
            predicted_gap_pct=predicted_gap_pct,
            actual_gap_pct=actual_gap_pct,
            gap_error_abs=gap_error_abs,
            composite_score=composite_score,
            regime_sox=regime_sox,
            regime_foreign_oi=regime_foreign_oi,
            divergence_signal=divergence_signal,
            eval_run_id=eval_run_id,
            lesson_quality_score=quality_score,
        )
        logger.success(f"[LessonWriter] {trade_date} 教訓已寫入（{error_type}，品質分 {quality_score}）")
        return True

    except Exception as exc:
        logger.warning(f"[LessonWriter] write_lesson 失敗（略過）: {exc}")
        return False
