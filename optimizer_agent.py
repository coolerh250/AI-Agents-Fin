"""optimizer_agent.py — Phase 2

The Optimizer Agent. Once a week (driven by scripts/optimizer_run.py) it
reviews one pipeline agent's recent shadow performance and, when it sees a
defensible improvement, proposes a new shadow strategy version.

It is itself a ReAct-loop agent — it reuses agent_runtime.run_agent_loop
with a read-only toolset plus the single write tool
propose_strategy_version. Model: Opus 4.7 (user decision A).

Hard bounds:
  * max_iter / token_budget passed via the in-code optimizer profile
  * a post-run cost check emits 'optimizer_cost_exceeded' above the cap

The optimizer can never promote anything: propose_strategy_version only
writes is_shadow=1 rows. Activation stays human-gated via promote_profile.
"""
from __future__ import annotations

import logging
import os
import sys
import uuid
from typing import Optional

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

from optimizer_tools import OPTIMIZER_CALLER, MARKER_START, build_propose_tool_spec

# ── Optimizer model / bounds ──────────────────────────────────────────────────

OPTIMIZER_MODEL = "claude-opus-4-7"
OPTIMIZER_MAX_ITER = 4
OPTIMIZER_TOKEN_BUDGET = 25000      # cumulative input tokens
OPTIMIZER_MAX_TOKENS = 2200         # per-iteration output cap
OPTIMIZER_COST_CAP_USD = float(os.getenv("OPTIMIZER_COST_CAP_USD", "0.30"))

_OPTIMIZER_TOOL_WHITELIST = [
    "get_agent_strategy_state",
    "get_recent_shadow_runs",
    "get_recent_lessons",
    "get_market_actuals",
    "propose_strategy_version",
]


# ── Read-tool handlers ────────────────────────────────────────────────────────

def _h_get_agent_strategy_state(_caller: str, agent_name: str) -> dict:
    """Active + shadow profile snapshot plus the active version's score."""
    from strategy_profile import load_active_profile, load_shadow_profile
    import optimizer_scoring

    active = load_active_profile(agent_name)
    if active is None:
        return {"error": "no_active_profile", "agent_name": agent_name}
    shadow = load_shadow_profile(agent_name)
    score = optimizer_scoring.score_version(agent_name, active.version)
    return {
        "agent_name": agent_name,
        "active": {
            "version": active.version,
            "params": active.params,
            "tool_whitelist": active.tool_whitelist,
            "model_name": active.model_name,
            "max_tokens": active.max_tokens,
            "system_prompt_preview": active.system_prompt[:600],
            "has_optimizer_marker": MARKER_START in active.system_prompt,
        },
        "shadow": (
            {"version": shadow.version, "params": shadow.params,
             "tool_whitelist": shadow.tool_whitelist}
            if shadow else None
        ),
        "active_score": score,
    }


def _h_get_recent_shadow_runs(_caller: str, agent_name: str, days: int = 14) -> dict:
    from database_tools import get_recent_shadow_runs
    runs = get_recent_shadow_runs(agent_name=agent_name, days=days, limit=50)
    return {"agent_name": agent_name, "days": days, "count": len(runs), "runs": runs}


def _h_get_recent_lessons(_caller: str, limit: int = 14) -> dict:
    from database_tools import get_recent_lessons
    return {"lessons": get_recent_lessons(limit=limit)}


def _h_get_market_actuals(_caller: str, days: int = 14) -> dict:
    from database_tools import _engine
    from sqlalchemy import text
    try:
        with _engine().connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT trade_date, actual_gap_pct, open_price, close_price
                    FROM market_actuals
                    WHERE trade_date >= CURDATE() - INTERVAL :d DAY
                    ORDER BY trade_date DESC
                """),
                {"d": days},
            ).fetchall()
        return {"days": days, "actuals": [dict(r._mapping) for r in rows]}
    except Exception as exc:
        logger.warning(f"[optimizer_agent] get_market_actuals failed: {exc}")
        return {"error": "market_actuals_query_failed", "detail": str(exc)[:200]}


# ── Tool registration ─────────────────────────────────────────────────────────

def register_optimizer_tools() -> None:
    """Register the optimizer's 4 read tools + the write tool into the shared
    tool_catalog registry. Idempotent."""
    from tool_catalog import ToolSpec, register

    register(ToolSpec(
        name="get_agent_strategy_state",
        description="目標 agent 目前 active 版本的 params / tools / prompt 摘要 / score，"
                    "以及 shadow 版本（若有）。優化前必先呼叫。",
        input_schema={
            "type": "object",
            "properties": {"agent_name": {"type": "string"}},
            "required": ["agent_name"],
            "additionalProperties": False,
        },
        handler=_h_get_agent_strategy_state,
        risk_level="low",
    ))
    register(ToolSpec(
        name="get_recent_shadow_runs",
        description="目標 agent 最近 N 天的 shadow_runs：divergence_score、shadow/primary 成本、"
                    "shadow_error。用於看行為穩定度與成本趨勢。",
        input_schema={
            "type": "object",
            "properties": {
                "agent_name": {"type": "string"},
                "days": {"type": "integer", "minimum": 1, "maximum": 60},
            },
            "required": ["agent_name"],
            "additionalProperties": False,
        },
        handler=_h_get_recent_shadow_runs,
        risk_level="low",
    ))
    register(ToolSpec(
        name="get_recent_lessons",
        description="最近的 strategy_lessons（事後檢討教訓）：error_type、direction_correct、"
                    "regime、教訓摘要。用於找改善方向的佐證。",
        input_schema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50}},
            "additionalProperties": False,
        },
        handler=_h_get_recent_lessons,
        risk_level="low",
    ))
    register(ToolSpec(
        name="get_market_actuals",
        description="最近 N 天台股實際開盤跳空（actual_gap_pct）與開/收盤價。"
                    "用於核對 agent 預測方向是否正確。",
        input_schema={
            "type": "object",
            "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 60}},
            "additionalProperties": False,
        },
        handler=_h_get_market_actuals,
        risk_level="low",
    ))
    register(build_propose_tool_spec())


# ── System prompt ─────────────────────────────────────────────────────────────

_OPTIMIZER_SYSTEM_PROMPT = """你是「策略優化器」(Optimizer Agent)，負責檢視單一 pipeline agent 的近期 shadow 表現，並在有充分證據時提出「一個」更好的 shadow 策略版本。

【你的目標】
讓目標 agent 的投資判斷更準確、更穩定、成本更低。不要做大改動 —— 基於資料做小幅、可解釋的調整。

【嚴格的有界變更規則】
你的提案只能落在以下範圍，propose_strategy_version 會強制檢查，違反一律退回且不寫入：
1. params：只能改 max_iter（1-8）、token_budget（1000-12000）、weights、thresholds。
2. tools：只能從父版 tool_whitelist「移除」工具，不能新增。
3. prompt：只能改寫 <!-- OPTIMIZER:WEIGHTS --> 標記區塊內的文字。父版若無此區塊（has_optimizer_marker=false），就完全不能改 prompt。
4. model_name：永遠不可改，工具會自動沿用父版。

【工作流程】
1. get_agent_strategy_state(agent_name)：取得 active 版本的 params / tools / prompt 摘要 / score。
2. get_recent_shadow_runs(agent_name, days=14)：看 divergence_score 趨勢、shadow_cost、shadow_error 比例。
3. 視需要 get_recent_lessons、get_market_actuals 找佐證。
4. 形成判斷：是否有「資料支持」的明確改善點？
5. 有 → 呼叫一次 propose_strategy_version，parent_version 用目前的 active 版本。
   沒有 → 不要提案，用文字簡述為何維持現狀即可。

【鐵則】
- reasoning 必須引用具體證據：shadow_run 的 id、lesson 的 trade_date、或明確的數值趨勢。空泛理由不被接受。
- score_predicted 是你的估計，不是承諾，請誠實估計。
- 一次執行最多提出一個提案。
- 寧可不提案，也不要提沒有證據支持的變更。維持現狀是完全可接受的結果。
- 你無法讓任何版本上線；提案只會成為 shadow，由人類審核後才可能採用。
"""


def _build_optimizer_profile():
    """In-code StrategyProfile for the optimizer itself (not stored in DB)."""
    from strategy_profile import StrategyProfile
    return StrategyProfile(
        agent_name=OPTIMIZER_CALLER,
        version=0,
        system_prompt=_OPTIMIZER_SYSTEM_PROMPT,
        params={"max_iter": OPTIMIZER_MAX_ITER, "token_budget": OPTIMIZER_TOKEN_BUDGET},
        tool_whitelist=list(_OPTIMIZER_TOOL_WHITELIST),
        model_name=OPTIMIZER_MODEL,
        max_tokens=OPTIMIZER_MAX_TOKENS,
        is_active=False,
        is_shadow=False,
    )


def _build_user_message(agent_name: str) -> str:
    return (
        f"請分析 pipeline agent「{agent_name}」最近的 shadow 表現，"
        f"判斷是否能提出一個更好的 shadow 策略版本。\n\n"
        f"先用 get_agent_strategy_state(\"{agent_name}\") 取得目前狀態，"
        f"再用 get_recent_shadow_runs(\"{agent_name}\", days=14) 看趨勢，"
        f"必要時查 lessons 與 market_actuals。\n"
        f"若找到有資料支持的改善點就呼叫 propose_strategy_version（parent_version 用 active 版本）；"
        f"若沒有明確證據，就不要提案、用文字說明原因。"
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def run_optimizer(agent_name: str, run_id: Optional[str] = None) -> dict:
    """Run one optimizer pass for `agent_name`. Returns:

        {
          "agent_name":     str,
          "run_id":         str,
          "proposed":       bool,        # did it call propose_strategy_version?
          "proposal_ok":    bool,        # did that call succeed (no violation)?
          "iterations":     int,
          "stopped_reason": str,
          "cost_usd":       float,
          "cost_exceeded":  bool,
          "final_text":     str,
          "tool_calls":     list[dict],
          "error":          Optional[str],
        }
    """
    from agent_runtime import run_agent_loop
    from telemetry import emit_event
    from tool_catalog import execute as _exec  # noqa: F401 (ensures catalog import)

    run_id = run_id or str(uuid.uuid4())
    register_optimizer_tools()

    profile = _build_optimizer_profile()
    user_msg = _build_user_message(agent_name)

    result = run_agent_loop(
        agent_name=OPTIMIZER_CALLER,
        run_id=run_id,
        profile=profile,
        user_message=user_msg,
    )

    cost = float(result.get("cost_usd", 0.0) or 0.0)
    cost_exceeded = cost > OPTIMIZER_COST_CAP_USD
    if cost_exceeded:
        logger.warning(f"[optimizer_agent] cost ${cost:.4f} exceeded cap "
                       f"${OPTIMIZER_COST_CAP_USD:.2f} for {agent_name}")
        emit_event(run_id, "optimizer_cost_exceeded", agent_name,
                   {"cost_usd": round(cost, 6), "cap": OPTIMIZER_COST_CAP_USD},
                   severity="warn")

    propose_calls = [tc for tc in result.get("tool_calls", [])
                     if tc.get("name") == "propose_strategy_version"]
    proposed = bool(propose_calls)

    # Determine whether the proposal actually wrote a row: re-check the DB for a
    # 'shadowing' proposal created in this run window. Simpler + truthful than
    # parsing tool_result text out of the loop.
    proposal_ok = False
    if proposed:
        proposal_ok = _proposal_was_written(agent_name)

    emit_event(run_id, "optimizer_run_complete", agent_name,
               {"proposed": proposed, "proposal_ok": proposal_ok,
                "iterations": result.get("iterations"),
                "stopped_reason": result.get("stopped_reason"),
                "cost_usd": round(cost, 6)},
               severity="info")

    return {
        "agent_name": agent_name,
        "run_id": run_id,
        "proposed": proposed,
        "proposal_ok": proposal_ok,
        "iterations": result.get("iterations", 0),
        "stopped_reason": result.get("stopped_reason", "unknown"),
        "cost_usd": round(cost, 6),
        "cost_exceeded": cost_exceeded,
        "final_text": result.get("final_text", ""),
        "tool_calls": result.get("tool_calls", []),
        "error": result.get("error"),
    }


def _proposal_was_written(agent_name: str) -> bool:
    """True if a 'shadowing' optimizer proposal exists for the agent created
    within the last few minutes (i.e. by the run that just finished)."""
    from database_tools import _engine
    from sqlalchemy import text
    try:
        with _engine().connect() as conn:
            row = conn.execute(
                text("""
                    SELECT 1 FROM optimizer_proposals
                    WHERE agent_name = :a AND status = 'shadowing'
                      AND created_at >= NOW() - INTERVAL 10 MINUTE
                    LIMIT 1
                """),
                {"a": agent_name},
            ).fetchone()
        return row is not None
    except Exception as exc:
        logger.debug(f"[optimizer_agent] _proposal_was_written failed: {exc}")
        return False


if __name__ == "__main__":
    import argparse
    import json

    p = argparse.ArgumentParser(description="Run one Optimizer Agent pass.")
    p.add_argument("agent_name")
    args = p.parse_args()

    out = run_optimizer(args.agent_name)
    print(json.dumps(out, default=str, ensure_ascii=False, indent=2))
