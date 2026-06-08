#!/usr/bin/env python
"""scripts/optimizer_run.py — Phase 2

Weekly cron entry for the Optimizer Agent. For each agent listed in
OPTIMIZER_AGENTS it:
  1. counts recent shadow_runs; skips if fewer than MIN_SHADOW_RUNS
  2. runs one optimizer pass (optimizer_agent.run_optimizer)
  3. backfills the proposal's optimizer_cost_usd

Gated by OPTIMIZER_ENABLED (default 0) — when disabled the script logs
and exits without spending anything. Scheduled Sunday 03:00 Taipei.

Run manually:  uv run python scripts/optimizer_run.py
"""
import os
import sys
import uuid

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from dotenv import load_dotenv
from sqlalchemy import text

load_dotenv()

MIN_SHADOW_RUNS = 7      # lowered 10→7 (2026-06-08): tech_analyst sits at 9 after v3 promote stopped self-shadow, would otherwise be gated forever
SHADOW_WINDOW_DAYS = 14


def _enabled() -> bool:
    return os.getenv("OPTIMIZER_ENABLED", "0").strip() == "1"


def _agents() -> list[str]:
    raw = os.getenv("OPTIMIZER_AGENTS", "") or ""
    return [a.strip() for a in raw.split(",") if a.strip()]


def _count_shadow_runs(agent: str, days: int) -> int:
    from database_tools import get_recent_shadow_runs
    return len(get_recent_shadow_runs(agent_name=agent, days=days, limit=500))


def _backfill_cost(agent: str, cost: float) -> None:
    """Set optimizer_cost_usd on the proposal this run just wrote (the latest
    'shadowing' row for the agent with a NULL cost)."""
    from database_tools import _engine
    try:
        with _engine().begin() as conn:
            row = conn.execute(
                text("""
                    SELECT id FROM optimizer_proposals
                    WHERE agent_name = :a AND status = 'shadowing'
                      AND optimizer_cost_usd IS NULL
                    ORDER BY created_at DESC LIMIT 1
                """),
                {"a": agent},
            ).fetchone()
            if row:
                conn.execute(
                    text("UPDATE optimizer_proposals SET optimizer_cost_usd = :c "
                         "WHERE id = :id"),
                    {"c": cost, "id": int(row[0])},
                )
    except Exception as exc:
        print(f"[optimizer_run] WARN: cost backfill for {agent} failed: {exc}")


def main() -> int:
    agents = _agents()
    enabled = _enabled()
    print(f"[optimizer_run] enabled={enabled} agents={agents}")

    if not enabled:
        print("[optimizer_run] OPTIMIZER_ENABLED != 1 — nothing to do")
        return 0
    if not agents:
        print("[optimizer_run] OPTIMIZER_AGENTS empty — nothing to do")
        return 0

    from optimizer_agent import run_optimizer
    from telemetry import emit_event

    for agent in agents:
        run_id = str(uuid.uuid4())
        n = _count_shadow_runs(agent, SHADOW_WINDOW_DAYS)
        if n < MIN_SHADOW_RUNS:
            print(f"[optimizer_run] {agent}: only {n} shadow runs "
                  f"(<{MIN_SHADOW_RUNS}) — skip")
            emit_event(run_id, "optimizer_skip_insufficient_data", agent,
                       {"shadow_runs": n, "required": MIN_SHADOW_RUNS},
                       severity="info")
            continue

        print(f"[optimizer_run] {agent}: {n} shadow runs — running optimizer "
              f"(run_id={run_id})")
        try:
            result = run_optimizer(agent, run_id=run_id)
        except Exception as exc:
            print(f"[optimizer_run] ERROR: {agent} optimizer raised: {exc}")
            emit_event(run_id, "optimizer_run_error", agent,
                       {"detail": str(exc)[:300]}, severity="error")
            continue

        if result.get("proposal_ok"):
            _backfill_cost(agent, result["cost_usd"])

        print(f"[optimizer_run] {agent}: proposed={result['proposed']} "
              f"proposal_ok={result['proposal_ok']} "
              f"cost=${result['cost_usd']:.4f} "
              f"iterations={result['iterations']} "
              f"stopped={result['stopped_reason']} "
              f"cost_exceeded={result['cost_exceeded']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
