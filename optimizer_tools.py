"""optimizer_tools.py — Phase 2

The Optimizer Agent's single WRITE tool: `propose_strategy_version`.
Every other optimizer tool is read-only. This module owns the
bounded-change contract — the hard limits that keep an autonomous
optimizer from doing anything a human reviewer would not expect:

  * model_name is never changed (always inherited from the parent).
  * params: only {max_iter, token_budget, weights, thresholds} may
    change; max_iter in [1, 8], token_budget in [1000, 12000].
  * tools may only be REMOVED from the parent whitelist, never added.
  * system_prompt: only the text inside a single
    <!-- OPTIMIZER:WEIGHTS --> ... <!-- /OPTIMIZER:WEIGHTS --> block
    may change; everything outside stays byte-identical to the parent,
    and the replacement text may not exceed 1.5x the parent block length.

On any violation the tool returns {"error": "bounded_change_violation",
...} and writes nothing. On success it writes, in one transaction:
  - a new agent_strategy_profiles row (is_shadow=1, is_active=0,
    created_by='optimizer', parent_version=N) — clearing any prior
    shadow flag for the agent so the one-shadow invariant holds
  - an optimizer_proposals row (status='shadowing')
  - any prior 'shadowing' proposal for the agent is marked 'rejected'
    (decided_by='superseded')

The tool never raises — it is meant to run inside the optimizer's
ReAct loop, which must be able to read the error and decide.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from typing import Optional

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from dotenv import load_dotenv
from sqlalchemy import text

load_dotenv()
logger = logging.getLogger(__name__)


# ── Bounded-change contract constants ─────────────────────────────────────────

OPTIMIZER_CALLER = "optimizer_agent"

MARKER_START = "<!-- OPTIMIZER:WEIGHTS -->"
MARKER_END = "<!-- /OPTIMIZER:WEIGHTS -->"

ALLOWED_PARAM_KEYS = {"max_iter", "token_budget", "weights", "thresholds"}
MIN_ITER_LIMIT, MAX_ITER_LIMIT = 1, 8
TOKEN_BUDGET_MIN, TOKEN_BUDGET_MAX = 1000, 12000
MIN_REASONING_CHARS = 30

# A marker-block edit may not exceed GROWTH_FACTOR x the parent block length,
# floored at MIN_BUDGET chars so a tiny parent block can still grow sensibly.
# Stops the optimizer from quietly bloating the prompt.
MARKER_TEXT_GROWTH_FACTOR = 1.5
MARKER_TEXT_MIN_BUDGET = 200


class _BoundedChangeError(Exception):
    """Raised internally when a proposal violates the bounded-change contract.
    Converted to a {"error": "bounded_change_violation"} dict by the handler."""


# ── Validators ────────────────────────────────────────────────────────────────

def _validate_params(new_params: Optional[dict], parent_params: dict) -> dict:
    """Return the merged params dict (parent overlaid with new_params).
    Raises _BoundedChangeError on any out-of-contract change."""
    if new_params is None:
        return dict(parent_params)
    if not isinstance(new_params, dict):
        raise _BoundedChangeError("new_params must be an object")

    illegal = set(new_params) - ALLOWED_PARAM_KEYS
    if illegal:
        raise _BoundedChangeError(
            f"new_params may only contain {sorted(ALLOWED_PARAM_KEYS)}; "
            f"got disallowed key(s): {sorted(illegal)}"
        )

    if "max_iter" in new_params:
        mi = new_params["max_iter"]
        if not isinstance(mi, int) or isinstance(mi, bool):
            raise _BoundedChangeError("max_iter must be an integer")
        if not (MIN_ITER_LIMIT <= mi <= MAX_ITER_LIMIT):
            raise _BoundedChangeError(
                f"max_iter must be in [{MIN_ITER_LIMIT}, {MAX_ITER_LIMIT}]; got {mi}"
            )

    if "token_budget" in new_params:
        tb = new_params["token_budget"]
        if not isinstance(tb, int) or isinstance(tb, bool):
            raise _BoundedChangeError("token_budget must be an integer")
        if not (TOKEN_BUDGET_MIN <= tb <= TOKEN_BUDGET_MAX):
            raise _BoundedChangeError(
                f"token_budget must be in [{TOKEN_BUDGET_MIN}, {TOKEN_BUDGET_MAX}]; got {tb}"
            )

    for key in ("weights", "thresholds"):
        if key in new_params:
            val = new_params[key]
            if not isinstance(val, dict):
                raise _BoundedChangeError(f"{key} must be an object")
            for k, v in val.items():
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    raise _BoundedChangeError(f"{key}.{k} must be numeric")

    merged = dict(parent_params)
    merged.update(new_params)
    return merged


def _validate_tools(tools_to_remove: Optional[list], parent_whitelist: list) -> list:
    """Return the new tool whitelist (parent minus tools_to_remove).
    Raises _BoundedChangeError if a name is not in the parent whitelist
    (which would mean an attempt to add, or a typo)."""
    if not tools_to_remove:
        return list(parent_whitelist)
    if not isinstance(tools_to_remove, list):
        raise _BoundedChangeError("tools_to_remove must be an array")

    parent_set = set(parent_whitelist)
    unknown = [t for t in tools_to_remove if t not in parent_set]
    if unknown:
        raise _BoundedChangeError(
            f"tools_to_remove may only contain tools already in the parent "
            f"whitelist {sorted(parent_set)}; unknown: {unknown}"
        )
    remove_set = set(tools_to_remove)
    return [t for t in parent_whitelist if t not in remove_set]


def _splice_prompt(parent_prompt: str, new_marker_text: Optional[str]) -> str:
    """Return the new system_prompt. If new_marker_text is empty/None the
    prompt is returned unchanged. Otherwise the text between the single
    OPTIMIZER:WEIGHTS marker pair is replaced. Raises _BoundedChangeError
    if the parent has no such block or new_marker_text smuggles markers."""
    if not new_marker_text or not new_marker_text.strip():
        return parent_prompt

    if MARKER_START in new_marker_text or MARKER_END in new_marker_text:
        raise _BoundedChangeError(
            "new_prompt_marker_text must not contain OPTIMIZER:WEIGHTS marker tokens"
        )

    n_start = parent_prompt.count(MARKER_START)
    n_end = parent_prompt.count(MARKER_END)
    if n_start != 1 or n_end != 1:
        raise _BoundedChangeError(
            f"parent prompt must contain exactly one OPTIMIZER:WEIGHTS block "
            f"(found {n_start} start / {n_end} end markers); prompt edits are "
            f"only allowed inside such a block"
        )

    i_start = parent_prompt.index(MARKER_START)
    i_end = parent_prompt.index(MARKER_END)
    if i_end <= i_start:
        raise _BoundedChangeError("OPTIMIZER:WEIGHTS end marker precedes start marker")

    old_inner = parent_prompt[i_start + len(MARKER_START):i_end].strip()
    new_inner = new_marker_text.strip()
    max_len = max(int(len(old_inner) * MARKER_TEXT_GROWTH_FACTOR),
                  MARKER_TEXT_MIN_BUDGET)
    if len(new_inner) > max_len:
        raise _BoundedChangeError(
            f"new_prompt_marker_text length {len(new_inner)} exceeds the "
            f"{max_len}-char limit ({MARKER_TEXT_GROWTH_FACTOR}x the parent "
            f"block); keep weight/threshold edits concise"
        )

    before = parent_prompt[: i_start + len(MARKER_START)]
    after = parent_prompt[i_end:]
    return f"{before}\n{new_inner}\n{after}"


# ── The write tool ────────────────────────────────────────────────────────────

def propose_strategy_version(
    *,
    _caller: str,
    agent_name: str,
    parent_version: int,
    reasoning: str,
    score_predicted: float,
    new_params: Optional[dict] = None,
    new_prompt_marker_text: Optional[str] = None,
    tools_to_remove: Optional[list] = None,
) -> dict:
    """Propose a new shadow strategy version for `agent_name`, forked from
    `parent_version`. Never raises. Returns one of:

      success                  → {"ok": True, "agent_name", "proposed_version",
                                   "parent_version", "score_baseline",
                                   "sample_count", "diff_summary"}
      permission failure        → {"error": "permission_denied", ...}
      contract violation        → {"error": "bounded_change_violation", "detail"}
      bad parent / no-op / etc  → {"error": "<kind>", "detail"}
    """
    # D-6 permission — fail closed, but return a dict so the ReAct loop continues.
    try:
        from database_tools import validate_tool_permission
        validate_tool_permission("propose_strategy_version", _caller)
    except PermissionError as exc:
        return {"error": "permission_denied",
                "tool": "propose_strategy_version", "detail": str(exc)}

    try:
        return _propose_inner(
            agent_name=agent_name,
            parent_version=parent_version,
            reasoning=reasoning,
            score_predicted=score_predicted,
            new_params=new_params,
            new_prompt_marker_text=new_prompt_marker_text,
            tools_to_remove=tools_to_remove,
        )
    except _BoundedChangeError as exc:
        return {"error": "bounded_change_violation", "detail": str(exc)}
    except Exception as exc:
        logger.warning(f"[optimizer_tools] propose_strategy_version failed: {exc}")
        return {"error": "propose_failed", "detail": str(exc)[:300]}


def _propose_inner(
    *,
    agent_name: str,
    parent_version: int,
    reasoning: str,
    score_predicted: float,
    new_params: Optional[dict],
    new_prompt_marker_text: Optional[str],
    tools_to_remove: Optional[list],
) -> dict:
    from strategy_profile import load_profile_version
    from database_tools import _engine
    import optimizer_scoring

    # 1. Argument sanity (not contract violations — bad inputs from the LLM)
    if not isinstance(reasoning, str) or len(reasoning.strip()) < MIN_REASONING_CHARS:
        return {"error": "reasoning_too_short",
                "detail": f"reasoning must be >= {MIN_REASONING_CHARS} chars and cite evidence"}
    try:
        score_predicted = float(score_predicted)
    except (TypeError, ValueError):
        return {"error": "bad_score_predicted", "detail": "score_predicted must be numeric"}
    if not (0.0 <= score_predicted <= 1.0):
        return {"error": "bad_score_predicted", "detail": "score_predicted must be in [0, 1]"}

    parent = load_profile_version(agent_name, parent_version)
    if parent is None:
        return {"error": "parent_version_not_found",
                "detail": f"{agent_name} has no version {parent_version}"}

    # 2. Bounded-change validation (raises _BoundedChangeError on violation)
    merged_params = _validate_params(new_params, parent.params)
    new_whitelist = _validate_tools(tools_to_remove, parent.tool_whitelist)
    new_prompt = _splice_prompt(parent.system_prompt, new_prompt_marker_text)

    # 3. Require at least one real change
    params_changed = merged_params != parent.params
    prompt_changed = new_prompt != parent.system_prompt
    tools_changed = new_whitelist != parent.tool_whitelist
    if not (params_changed or prompt_changed or tools_changed):
        return {"error": "noop_proposal",
                "detail": "proposal makes no change relative to the parent version"}

    # 4. Objective baseline — computed here, not trusted from the LLM
    baseline = optimizer_scoring.score_version(agent_name, parent_version)
    score_baseline = baseline.get("score")
    sample_count = baseline.get("sample_count", 0)
    window_days = baseline.get("window_days", optimizer_scoring.DEFAULT_WINDOW_DAYS)

    diff_summary = {
        "params_changed": {
            k: {"from": parent.params.get(k), "to": merged_params.get(k)}
            for k in sorted(set(merged_params) | set(parent.params))
            if parent.params.get(k) != merged_params.get(k)
        },
        "prompt_changed": prompt_changed,
        "tools_removed": sorted(set(parent.tool_whitelist) - set(new_whitelist)),
        "model_name": parent.model_name,
    }

    # 5. One transaction: supersede old shadow, write profile + proposal
    with _engine().begin() as conn:
        new_version = int(conn.execute(
            text("SELECT COALESCE(MAX(version), 0) + 1 "
                 "FROM agent_strategy_profiles WHERE agent_name = :a"),
            {"a": agent_name},
        ).scalar())

        conn.execute(
            text("""
                UPDATE optimizer_proposals
                SET status = 'rejected', decided_at = NOW(), decided_by = 'superseded'
                WHERE agent_name = :a AND status = 'shadowing'
            """),
            {"a": agent_name},
        )
        conn.execute(
            text("UPDATE agent_strategy_profiles SET is_shadow = 0 "
                 "WHERE agent_name = :a AND is_shadow = 1"),
            {"a": agent_name},
        )
        conn.execute(
            text("""
                INSERT INTO agent_strategy_profiles
                    (agent_name, version, is_active, is_shadow, system_prompt,
                     params_json, tool_whitelist, model_name, max_tokens,
                     notes, created_by, parent_version)
                VALUES (:a, :v, 0, 1, :sp, :pj, :tw, :mn, :mt,
                        :nt, 'optimizer', :pv)
            """),
            {"a": agent_name, "v": new_version, "sp": new_prompt,
             "pj": json.dumps(merged_params, ensure_ascii=False),
             "tw": json.dumps(new_whitelist, ensure_ascii=False),
             "mn": parent.model_name, "mt": parent.max_tokens,
             "nt": f"optimizer proposal forked from v{parent_version}",
             "pv": parent_version},
        )
        conn.execute(
            text("""
                INSERT INTO optimizer_proposals
                    (agent_name, proposed_version, parent_version,
                     input_window_days, sample_count, score_baseline,
                     score_predicted, reasoning, diff_summary, status)
                VALUES (:a, :v, :pv, :iwd, :sc, :sb, :spred, :rsn, :diff, 'shadowing')
            """),
            {"a": agent_name, "v": new_version, "pv": parent_version,
             "iwd": window_days, "sc": sample_count,
             "sb": score_baseline, "spred": score_predicted,
             "rsn": reasoning.strip(),
             "diff": json.dumps(diff_summary, ensure_ascii=False, default=str)},
        )

    logger.info(f"[optimizer_tools] proposed {agent_name} v{new_version} "
                f"(parent v{parent_version})")
    return {
        "ok": True,
        "agent_name": agent_name,
        "proposed_version": new_version,
        "parent_version": parent_version,
        "score_baseline": score_baseline,
        "sample_count": sample_count,
        "diff_summary": diff_summary,
    }


# ── ToolSpec for the optimizer ReAct loop (registered by optimizer_agent) ──────

def build_propose_tool_spec():
    """Build the ToolSpec wrapping propose_strategy_version. Registered into
    tool_catalog by the optimizer agent (Day 4), kept out of the default
    catalog so pipeline agents can never see a write tool."""
    from tool_catalog import ToolSpec
    return ToolSpec(
        name="propose_strategy_version",
        description=(
            "提出一個新的 shadow 策略版本。只能做有界變更："
            "(1) new_params 僅允許 max_iter[1-8] / token_budget[1000-12000] / "
            "weights / thresholds；"
            "(2) tools_to_remove 只能移除父版既有工具，不能新增；"
            "(3) new_prompt_marker_text 只改 <!-- OPTIMIZER:WEIGHTS --> 區塊內文字，"
            "父版若無該區塊則不可改 prompt；"
            "(4) model_name 永遠沿用父版。"
            "違反任一規則回 bounded_change_violation 且不寫入。"
            "成功會寫入新 shadow 版本 + proposal 紀錄。每次 optimizer 執行最多呼叫一次。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "agent_name": {"type": "string"},
                "parent_version": {"type": "integer"},
                "reasoning": {
                    "type": "string",
                    "description": f"變更理由，必須引用具體 shadow_run id 或 lesson 佐證，"
                                   f"至少 {MIN_REASONING_CHARS} 字",
                },
                "score_predicted": {
                    "type": "number", "minimum": 0, "maximum": 1,
                    "description": "預估新版的 score（0-1），僅為估計",
                },
                "new_params": {
                    "type": "object",
                    "description": "僅 max_iter / token_budget / weights / thresholds；不改則省略",
                },
                "new_prompt_marker_text": {
                    "type": ["string", "null"],
                    "description": "OPTIMIZER:WEIGHTS 區塊的新內容；不改 prompt 則留空。"
                                   "長度不可超過父版區塊的 1.5 倍，請保持精簡。",
                },
                "tools_to_remove": {
                    "type": "array", "items": {"type": "string"},
                    "description": "要從父版 tool_whitelist 移除的工具名稱",
                },
            },
            "required": ["agent_name", "parent_version", "reasoning", "score_predicted"],
            "additionalProperties": False,
        },
        handler=propose_strategy_version,
        risk_level="high",
    )


def register_optimizer_write_tool() -> None:
    """Register propose_strategy_version into the shared tool_catalog registry."""
    from tool_catalog import register
    register(build_propose_tool_spec())
