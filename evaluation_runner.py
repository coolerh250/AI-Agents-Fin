"""
evaluation_runner.py
Agent Evaluation Framework — CLI orchestration script.
Reads DB outputs, runs rule-based scoring, persists to eval_runs + eval_results.

Usage:
    uv run python evaluation_runner.py              # evaluate today
    uv run python evaluation_runner.py --date 2026-05-14
"""
import argparse
import sys
from datetime import date, datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from loguru import logger

load_dotenv()

logger.remove()
logger.add(
    sys.stdout,
    format="{time:HH:mm:ss} | {level:<8} | {message}",
    level="DEBUG",
    colorize=False,
)


def _resolve_run_id(trade_date: date) -> Optional[str]:
    """Find the most recent investment workflow_run for trade_date."""
    try:
        from database_tools import _engine
        from sqlalchemy import text
        with _engine().connect() as conn:
            row = conn.execute(
                text("""
                    SELECT id FROM workflow_runs
                    WHERE DATE(started_at) = :td AND run_type = 'investment'
                      AND status = 'success'
                    ORDER BY started_at DESC
                    LIMIT 1
                """),
                {"td": trade_date},
            ).fetchone()
            return str(row[0]) if row else None
    except Exception as exc:
        logger.debug(f"[eval] _resolve_run_id failed: {exc}")
        return None


def _load_agent_outputs(run_id_ref: Optional[str], trade_date: date) -> dict:
    """
    Load raw LLM text outputs keyed by agent_name.
    Primary path: llm_traces WHERE run_id = run_id_ref.
    Fallback: reconstruct from daily_briefs + session_episodes.
    """
    outputs = {
        "data_collector":   "",
        "chip_analyst":     "",
        "tech_analyst":     "",
        "chief_strategist": "",
        "format_agent":     "",
    }

    if run_id_ref:
        try:
            from database_tools import _engine
            from sqlalchemy import text
            with _engine().connect() as conn:
                rows = conn.execute(
                    text("""
                        SELECT agent_name, raw_response, finish_reason
                        FROM llm_traces
                        WHERE run_id = :rid
                        ORDER BY id ASC
                    """),
                    {"rid": run_id_ref},
                ).fetchall()
            # ReAct loops (Phase 1.5+) write one llm_trace per iteration.
            # Iter 1 typically finish_reason='tool_use' (Chinese commentary
            # explaining why tools are about to be called) — that text is NOT
            # the production output. The terminal iteration finishes with
            # 'end_turn' and contains the actual JSON/final answer.
            # First pass: prefer the earliest end_turn trace per agent.
            # Shadow traces are written AFTER primary, so the first end_turn
            # is always the primary's — same property the old "first non-empty"
            # filter relied on for shadow isolation.
            for row in rows:
                name, raw, finish = row[0], row[1], row[2]
                if name in outputs and raw and finish == "end_turn" and not outputs[name]:
                    outputs[name] = str(raw)
            # Second pass: agents without an end_turn trace (errored / hit
            # max_tokens / single-shot agents that still get a tool_use stop)
            # fall back to the first non-empty trace.
            for row in rows:
                name, raw = row[0], row[1]
                if name in outputs and raw and not outputs[name]:
                    outputs[name] = str(raw)
            logger.debug(f"[eval] Loaded {len(rows)} llm_trace rows for run_id={run_id_ref}")
        except Exception as exc:
            logger.warning(f"[eval] llm_traces load failed: {exc}")

    # Fallback for chief_strategist: use brief_text from daily_briefs
    if not outputs["chief_strategist"]:
        try:
            from database_tools import get_brief
            brief = get_brief(trade_date)
            if brief:
                outputs["chief_strategist"] = brief.get("brief_text", "")
        except Exception:
            pass

    return outputs


def _build_raw_market_data(episode: Optional[dict]) -> dict:
    """Reconstruct raw_market_data dict from session_episodes row.

    Numeric columns come back as int (oi_net) or decimal.Decimal (pct
    columns are DECIMAL(6,3)). eval_data_collector checks
    isinstance(v, (int, float)) which rejects Decimal, so we normalize
    every numeric to a plain float here."""
    if not episode:
        return {}

    def _f(v):
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return v
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    return {
        "foreign_oi_net":   _f(episode.get("foreign_oi_net")),
        "trust_oi_net":     _f(episode.get("trust_oi_net")),
        "dealer_oi_net":    _f(episode.get("dealer_oi_net")),
        "djia_chg_pct":     _f(episode.get("djia_chg_pct")),
        "ndx_chg_pct":      _f(episode.get("ndx_chg_pct")),
        "sox_chg_pct":      _f(episode.get("sox_chg_pct")),
        "tsm_adr_chg_pct":  _f(episode.get("tsm_adr_chg_pct")),
        "data_ok":          True,  # episode row exists → data_collector ran
    }


def _calc_actual_direction(actual_gap_pct: Optional[float]) -> Optional[str]:
    """Mirror dashboard._calc_accuracy_kpi thresholds."""
    if actual_gap_pct is None:
        return None
    if actual_gap_pct > 0.3:
        return "up"
    if actual_gap_pct < -0.3:
        return "down"
    return "flat"


def run_evaluation(trade_date: date) -> Optional[int]:
    """
    Full evaluation pipeline for one trade_date.
    Returns the eval_run_id on success, None on fatal error.
    """
    from database_tools import (
        ensure_eval_tables,
        get_brief,
        get_actual,
        get_session_episode,
        create_eval_run,
        save_eval_result,
    )
    from evaluation_metrics import (
        eval_data_collector,
        eval_chip_analyst,
        eval_tech_analyst,
        eval_chief_strategist,
        eval_format_agent,
        compute_brief_quality_score,
    )

    ensure_eval_tables()

    logger.info(f"[eval] Starting evaluation for {trade_date}")

    # --- Load data ---
    brief_row   = get_brief(trade_date)
    actual_row  = get_actual(str(trade_date))
    episode     = get_session_episode(trade_date)
    run_id_ref  = _resolve_run_id(trade_date)

    if not brief_row:
        logger.warning(f"[eval] No daily_briefs row for {trade_date} — aborting")
        return None

    agent_outputs = _load_agent_outputs(run_id_ref, trade_date)

    # --- Per-agent evaluations ---
    raw_market_data = _build_raw_market_data(episode)
    agent_results = {
        "data_collector":   eval_data_collector(raw_market_data),
        "chip_analyst":     eval_chip_analyst(agent_outputs["chip_analyst"]),
        "tech_analyst":     eval_tech_analyst(agent_outputs["tech_analyst"]),
        "chief_strategist": eval_chief_strategist(agent_outputs["chief_strategist"]),
        "format_agent":     eval_format_agent(agent_outputs["format_agent"]),
    }

    # --- Composite score ---
    agent_scores = {k: v["quality_score"] for k, v in agent_results.items()}
    quality_score = compute_brief_quality_score(agent_scores)

    # --- Direction accuracy ---
    predicted_direction = brief_row.get("gap_direction")
    actual_gap_pct: Optional[float] = None
    actual_direction: Optional[str] = None
    direction_correct: Optional[int] = None

    if actual_row:
        try:
            actual_gap_pct = float(actual_row.get("actual_gap_pct") or 0)
            actual_direction = _calc_actual_direction(actual_gap_pct)
            if predicted_direction and actual_direction:
                direction_correct = 1 if predicted_direction == actual_direction else 0
        except (TypeError, ValueError):
            pass

    # --- Persist eval_run ---
    eval_run_id = create_eval_run(
        trade_date=trade_date,
        run_id_ref=run_id_ref,
        triggered_by="cron",
        brief_quality_score=quality_score,
        direction_correct=direction_correct,
        predicted_direction=predicted_direction,
        actual_direction=actual_direction,
    )

    if eval_run_id < 0:
        logger.error("[eval] create_eval_run returned invalid id — aborting result writes")
        return None

    # --- Persist per-agent results ---
    for agent_name, result in agent_results.items():
        save_eval_result(
            eval_run_id=eval_run_id,
            trade_date=trade_date,
            agent_name=agent_name,
            quality_score=result["quality_score"],
            schema_valid=result["schema_valid"],
            missing_fields=result["missing_fields"],
            hallucination_flags=result["hallucination_flags"],
            extra_metrics=result["extra_metrics"],
        )

    # --- Summary log ---
    dir_label = (
        f"{predicted_direction}→{actual_direction} "
        f"({'✓' if direction_correct == 1 else '✗' if direction_correct == 0 else '—'})"
        if predicted_direction else "—"
    )
    hallucination_count = sum(len(r["hallucination_flags"]) for r in agent_results.values())
    schema_failures = [k for k, r in agent_results.items() if not r["schema_valid"]]

    logger.success(
        f"[eval] Done: trade_date={trade_date} eval_run_id={eval_run_id} "
        f"quality={quality_score:.1f} direction={dir_label} "
        f"schema_fail={schema_failures} hallucination={hallucination_count}"
    )
    return eval_run_id


def main() -> None:
    parser = argparse.ArgumentParser(description="Run agent evaluation for a given trade date")
    parser.add_argument("--date", type=str, default=None,
                        help="Trade date YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    if args.date:
        trade_date = date.fromisoformat(args.date)
    else:
        trade_date = date.today()

    result = run_evaluation(trade_date)
    sys.exit(0 if result is not None else 1)


if __name__ == "__main__":
    main()
