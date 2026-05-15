"""
telemetry.py
Observability helper — thin convenience layer for agents to record
usage, trace LLM calls, and emit structured events.

Usage (in any agent node):
    from telemetry import record_usage, emit_event

All heavy DB logic lives in database_tools.py; this module only adds
the thinking-token extraction and log_llm_trace call on top.
"""
import time
from typing import Optional

from loguru import logger


def record_usage(
    agent_name: str,
    model: str,
    response,
    latency_ms: int,
    run_id: Optional[str] = None,
    system_prompt: str = "",
    user_content: str = "",
    pricing: Optional[dict] = None,
) -> None:
    """
    Record cost + optional LLM trace for one agent invocation.
    Fails silently — never crashes the workflow.
    """
    try:
        from database_tools import log_cost, log_llm_trace

        usage        = response.usage_metadata or {}
        in_tok       = usage.get("input_tokens", 0)
        out_tok      = usage.get("output_tokens", 0)
        thinking_tok = usage.get("thinking_tokens", 0)

        if pricing and model in pricing:
            p    = pricing[model]
            cost = (in_tok * p["input"] + out_tok * p["output"]) / 1_000_000
        else:
            cost = 0.0

        finish_reason = getattr(response, "stop_reason", None)
        raw_text      = _extract_text(response)

        log_cost(
            agent_name=agent_name,
            model_name=model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            thinking_tokens=thinking_tok,
            estimated_cost_usd=cost,
            latency_ms=latency_ms,
            run_id=run_id,
        )
        logger.debug(
            f"[{agent_name}] tokens={in_tok}+{out_tok}"
            + (f"+{thinking_tok}thinking" if thinking_tok else "")
            + f" cost=${cost:.6f} latency={latency_ms}ms"
        )

        if run_id:
            log_llm_trace(
                run_id=run_id,
                agent_name=agent_name,
                model_name=model,
                system_prompt=system_prompt,
                user_content=user_content,
                raw_response=raw_text,
                finish_reason=finish_reason,
                input_tokens=in_tok,
                output_tokens=out_tok,
                thinking_tokens=thinking_tok,
                latency_ms=latency_ms,
            )
    except Exception as exc:
        logger.warning(f"[telemetry] record_usage failed for {agent_name}: {exc}")


def emit_event(
    run_id: Optional[str],
    event_type: str,
    node_name: Optional[str] = None,
    detail: Optional[dict] = None,
    severity: str = "info",
) -> None:
    """Emit a structured workflow event. No-op if run_id is None."""
    if not run_id:
        return
    try:
        from database_tools import log_event
        log_event(run_id, event_type, node_name, detail, severity)
    except Exception as exc:
        logger.warning(f"[telemetry] emit_event failed: {exc}")


def _extract_text(response) -> str:
    c = response.content
    if isinstance(c, str):
        return c.strip()
    return "\n".join(
        b["text"] for b in c if isinstance(b, dict) and b.get("type") == "text"
    ).strip()


def timed_invoke(llm, messages: list) -> tuple:
    """Invoke an LLM and return (response, latency_ms)."""
    start = time.monotonic()
    response = llm.invoke(messages)
    return response, int((time.monotonic() - start) * 1000)
