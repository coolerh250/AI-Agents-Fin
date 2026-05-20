"""
agent_runtime.py
ReAct loop runner for the multi-agent transformation.

Wraps Anthropic SDK `messages.create(tools=...)` in a bounded
LLM↔tool_use loop. The loop terminates on the earliest of:
  * `stop_reason == 'end_turn'`           → success
  * `iterations >= max_iter`              → 'max_iter_reached'
  * `cum_input_tokens >= token_budget`    → 'budget_exhausted'
  * an unhandled exception                → 'error'

permission_denied / unknown_tool / tool_execution_failed responses from
tool_catalog.execute are fed back to the LLM as tool_result content so
the agent can decide whether to retry, switch tool, or stop. They never
raise.

The runtime reads its model / max_tokens / tool_whitelist / system_prompt
from a StrategyProfile (which comes from agent_strategy_profiles via
strategy_profile.load_active_profile or load_shadow_profile). All
behavior is thus tunable via the DB row — no code change needed for
Phase 2 Optimizer to adjust prompts or limits.
"""
import os
import time
from typing import Optional

import anthropic
from loguru import logger

from strategy_profile import StrategyProfile
from telemetry import emit_event, record_usage
from tool_catalog import execute, get_tools, to_anthropic_format


MAX_ITERATIONS_DEFAULT = 5
TOKEN_BUDGET_DEFAULT   = 8000      # cumulative input tokens across all iterations

# Pricing copied from market_analyst_agents._PRICING. Kept here to avoid
# importing that module from the runtime (which would create a cycle once
# market_analyst nodes start invoking the runtime).
_PRICING = {
    "claude-haiku-4-5-20251001": {"input": 1.00, "output":  5.00},
    "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00},
    "claude-opus-4-7":           {"input": 5.00, "output": 25.00},
}


def _estimate_cost(model: str, in_tok: int, out_tok: int) -> float:
    p = _PRICING.get(model)
    if not p:
        return 0.0
    return (in_tok * p["input"] + out_tok * p["output"]) / 1_000_000


def _content_for_anthropic(blocks) -> list:
    """Convert SDK ContentBlock objects to JSON-serializable dicts so we can
    feed them back into the next messages.create() call as 'assistant' content.
    Anthropic accepts both forms but we keep them as dicts for cleanliness."""
    out = []
    for b in blocks or []:
        t = getattr(b, "type", None)
        if t == "text":
            out.append({"type": "text", "text": getattr(b, "text", "")})
        elif t == "tool_use":
            out.append({
                "type": "tool_use",
                "id":   getattr(b, "id", ""),
                "name": getattr(b, "name", ""),
                "input": getattr(b, "input", {}) or {},
            })
    return out


def run_agent_loop(
    *,
    agent_name: str,
    run_id: Optional[str],
    profile: StrategyProfile,
    user_message: str,
    max_iter: Optional[int] = None,
    token_budget: Optional[int] = None,
) -> dict:
    """
    Run a ReAct loop for `agent_name` using `profile`'s prompt/model/tools.

    Returns:
        {
          "final_text":      str,           # last assistant text block(s)
          "tool_calls":      list[dict],    # all tool_use blocks the agent emitted
          "iterations":      int,
          "stopped_reason":  str,
          "input_tokens":    int,           # cumulative across iterations
          "output_tokens":   int,           # cumulative
          "cost_usd":        float,         # cumulative
          "error":           Optional[str], # only on stopped_reason='error'
        }
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.error(f"[{agent_name}] ANTHROPIC_API_KEY not set — aborting agent loop")
        return _make_result("", [], 0, "error", 0, 0, 0.0, error="ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key)

    max_iter = max_iter or int(profile.params.get("max_iter", MAX_ITERATIONS_DEFAULT))
    budget   = token_budget or int(profile.params.get("token_budget", TOKEN_BUDGET_DEFAULT))

    tool_specs = get_tools(profile.tool_whitelist or [])
    tools_payload = to_anthropic_format(tool_specs) if tool_specs else None

    messages: list[dict] = [{"role": "user", "content": user_message}]
    tool_calls_all: list[dict] = []

    cum_in_tok = 0
    cum_out_tok = 0
    cum_cost = 0.0
    iter_count = 0
    final_text = ""
    stopped_reason = "unknown"
    err: Optional[str] = None
    last_response = None

    while iter_count < max_iter:
        iter_count += 1
        start = time.monotonic()
        try:
            kwargs = {
                "model":      profile.model_name,
                "max_tokens": profile.max_tokens,
                "system":     profile.system_prompt,
                "messages":   messages,
            }
            if tools_payload:
                kwargs["tools"] = tools_payload
            response = client.messages.create(**kwargs)
        except Exception as exc:
            err = f"messages.create failed (iter {iter_count}): {exc}"
            logger.warning(f"[{agent_name}] {err}")
            stopped_reason = "error"
            break

        latency_ms = int((time.monotonic() - start) * 1000)
        last_response = response

        # cost / trace bookkeeping (per iteration)
        try:
            record_usage(
                agent_name=agent_name,
                model=profile.model_name,
                response=response,
                latency_ms=latency_ms,
                run_id=run_id,
                system_prompt=profile.system_prompt,
                user_content=user_message if iter_count == 1 else "(continued)",
                pricing=_PRICING,
            )
        except Exception as exc:
            logger.debug(f"[{agent_name}] record_usage failed: {exc}")

        # cumulative counts
        in_t  = int(getattr(response.usage, "input_tokens", 0) or 0)
        out_t = int(getattr(response.usage, "output_tokens", 0) or 0)
        cum_in_tok += in_t
        cum_out_tok += out_t
        cum_cost += _estimate_cost(profile.model_name, in_t, out_t)

        # collect any text blocks for partial final_text (gets overwritten each iter)
        text_parts = [getattr(b, "text", "") for b in response.content
                      if getattr(b, "type", None) == "text"]
        if text_parts:
            final_text = "\n".join(t for t in text_parts if t).strip()

        # collect tool_use blocks
        tool_use_blocks = [b for b in response.content
                           if getattr(b, "type", None) == "tool_use"]
        for b in tool_use_blocks:
            tool_calls_all.append({
                "iter": iter_count,
                "name": getattr(b, "name", ""),
                "input": getattr(b, "input", {}) or {},
            })

        # termination by stop_reason
        if response.stop_reason == "end_turn" and not tool_use_blocks:
            stopped_reason = "end_turn"
            break
        if response.stop_reason == "max_tokens":
            stopped_reason = "max_tokens"
            break

        # budget check (after counting this iteration)
        if cum_in_tok >= budget:
            stopped_reason = "budget_exhausted"
            emit_event(run_id, "budget_exhausted", agent_name,
                       {"cum_in_tok": cum_in_tok, "budget": budget,
                        "iterations": iter_count}, severity="warn")
            break

        # if no tool_use was emitted but stop_reason isn't end_turn either,
        # treat as done to avoid an infinite no-op loop
        if not tool_use_blocks:
            stopped_reason = response.stop_reason or "no_tool_no_end"
            break

        # execute each tool_use and collect tool_result messages
        # append the assistant response, then a user turn with tool_result blocks
        messages.append({"role": "assistant",
                         "content": _content_for_anthropic(response.content)})
        results_content = []
        for b in tool_use_blocks:
            tool_name = getattr(b, "name", "")
            tool_id   = getattr(b, "id", "")
            tool_args = getattr(b, "input", {}) or {}
            result = execute(tool_name, tool_args, caller=agent_name, run_id=run_id)
            # result is always a dict; serialize as JSON string for tool_result
            import json as _json
            results_content.append({
                "type":        "tool_result",
                "tool_use_id": tool_id,
                "content":     _json.dumps(result, ensure_ascii=False, default=str),
                "is_error":    bool(result.get("error") if isinstance(result, dict) else False),
            })
        messages.append({"role": "user", "content": results_content})

    if iter_count >= max_iter and stopped_reason == "unknown":
        stopped_reason = "max_iter_reached"
        emit_event(run_id, "max_iter_reached", agent_name,
                   {"iterations": iter_count, "max_iter": max_iter}, severity="warn")

    return _make_result(
        final_text=final_text,
        tool_calls=tool_calls_all,
        iterations=iter_count,
        stopped_reason=stopped_reason,
        input_tokens=cum_in_tok,
        output_tokens=cum_out_tok,
        cost_usd=cum_cost,
        error=err,
    )


def _make_result(
    final_text: str,
    tool_calls: list,
    iterations: int,
    stopped_reason: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    error: Optional[str] = None,
) -> dict:
    return {
        "final_text":     final_text,
        "tool_calls":     tool_calls,
        "iterations":     iterations,
        "stopped_reason": stopped_reason,
        "input_tokens":   input_tokens,
        "output_tokens":  output_tokens,
        "cost_usd":       cost_usd,
        "error":          error,
    }


# ── Shadow toggle (used by node wrappers in market_analyst_agents.py) ────────

def shadow_agents_from_env() -> set[str]:
    raw = os.getenv("SHADOW_AGENTS", "") or ""
    return {a.strip() for a in raw.split(",") if a.strip()}


def is_shadow_enabled(agent_name: str) -> bool:
    return agent_name in shadow_agents_from_env()
