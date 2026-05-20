"""
tool_catalog.py
Tool registry + executor for the multi-agent runtime.

Phase 1 substrate. Wraps existing MCP tools and direct functions in a
uniform `ToolSpec` shape so the ReAct loop in `agent_runtime.py` can:
  1. Build the Anthropic API `tools=[...]` payload from a per-agent
     whitelist (which lives in agent_strategy_profiles.tool_whitelist).
  2. Execute the LLM-chosen tool, enforcing D-6 permissions and writing
     a D-7 audit row via telemetry.audited_direct_call.

Why this layer instead of letting each agent call MCP / direct fns
directly: it gives the ReAct loop one place to (a) translate tool names
to handlers, (b) enforce the per-agent tool whitelist before execution,
(c) convert raw exceptions / permission denials into structured
`{"error": ...}` dicts that the LLM can read and recover from.

All current tools are read-only. No tool in this catalog mutates
production state.
"""
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from loguru import logger


# ── ToolSpec ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ToolSpec:
    name:         str                                # matches Anthropic tool_use "name"
    description:  str                                # what the LLM sees when choosing
    input_schema: dict                               # JSON Schema for tool input
    handler:      Callable[..., dict]                # invoked with **kwargs + _caller
    risk_level:   str                                # 'low' | 'medium' | 'high'
    audit_log:    bool = True                        # whether to write tool_audit_log


_TOOL_REGISTRY: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> None:
    """Idempotent: overwrites if same name re-registered."""
    _TOOL_REGISTRY[spec.name] = spec


def get_tools(whitelist: list[str]) -> list[ToolSpec]:
    """Return ToolSpec list filtered by the per-agent whitelist (preserves order)."""
    return [_TOOL_REGISTRY[n] for n in whitelist if n in _TOOL_REGISTRY]


def to_anthropic_format(specs: list[ToolSpec]) -> list[dict]:
    """Strip handler/risk/audit and emit the Anthropic API shape."""
    return [
        {"name": s.name, "description": s.description, "input_schema": s.input_schema}
        for s in specs
    ]


def execute(
    name: str,
    args: dict,
    *,
    caller: str,
    run_id: Optional[str] = None,
) -> dict:
    """
    Look up a tool by name, run its handler with `_caller=caller`.

    Returns a dict that's safe to feed back as a tool_result to the LLM:
      - on success: the handler's own dict (most are {"data": ..., "error"?})
      - on permission denial: {"error": "permission_denied", "tool": name, ...}
      - on unknown tool: {"error": "unknown_tool", "name": name}
      - on handler exception: {"error": "tool_execution_failed", "detail": str(exc)}

    Never raises — the ReAct loop should keep going and let the LLM
    decide whether to retry, switch tool, or stop.
    """
    spec = _TOOL_REGISTRY.get(name)
    if spec is None:
        return {"error": "unknown_tool", "name": name}

    from database_tools import log_tool_call

    start = time.monotonic()
    try:
        result = spec.handler(_caller=caller, **(args or {}))
        latency = int((time.monotonic() - start) * 1000)
        if spec.audit_log:
            log_tool_call(
                tool_id=name, tool_type="agent_loop", caller=caller, run_id=run_id,
                status="ok", latency_ms=latency,
            )
        return result if isinstance(result, dict) else {"data": result}
    except PermissionError as exc:
        latency = int((time.monotonic() - start) * 1000)
        log_tool_call(
            tool_id=name, tool_type="agent_loop", caller=caller, run_id=run_id,
            status="denied", latency_ms=latency, error_message=str(exc)[:500],
        )
        return {"error": "permission_denied", "tool": name, "detail": str(exc)}
    except Exception as exc:
        latency = int((time.monotonic() - start) * 1000)
        log_tool_call(
            tool_id=name, tool_type="agent_loop", caller=caller, run_id=run_id,
            status="error", latency_ms=latency, error_message=str(exc)[:500],
        )
        logger.warning(f"[tool_catalog] {name} handler raised: {exc}")
        return {"error": "tool_execution_failed", "tool": name, "detail": str(exc)[:300]}


# ── Built-in handlers ─────────────────────────────────────────────────────────

def _handler_mcp(server_script: str, tool_name: str):
    """Factory: build a handler that calls an MCP tool via stdio.
    The handler accepts _caller (logged via audit) plus tool-specific kwargs."""
    def _h(_caller: str, **kwargs) -> dict:
        from utils.mcp_call import call_mcp_tool_sync
        # MCP wrappers (notification/persistence) require api_key when guarded.
        # Market data tools do not require auth.
        return call_mcp_tool_sync(
            server_script=server_script,
            tool_name=tool_name,
            arguments=kwargs,
        )
    return _h


def _handler_get_user_portfolio(_caller: str, user_id: Optional[str] = None) -> dict:
    from portfolio_tools import get_user_portfolio
    holdings = get_user_portfolio(user_id=user_id, _caller=_caller)
    return {"holdings": holdings, "count": len(holdings)}


def _handler_calculate_pnl(_caller: str, holdings: list) -> dict:
    from portfolio_tools import calculate_pnl
    enriched = calculate_pnl(holdings, _caller=_caller)
    return {"holdings": enriched, "count": len(enriched)}


def _handler_get_stock_name(_caller: str, stock_id: str) -> dict:
    from database_tools import get_stock_name
    return {"stock_id": stock_id, "company_name": get_stock_name(stock_id)}


# ── Catalog seeding (called at module import) ─────────────────────────────────

def _bootstrap_catalog() -> None:
    """Register the seven Phase 1 tools. Pure data; no I/O."""

    # 1. US market summary (MCP, read-only, no args)
    register(ToolSpec(
        name="get_us_market_summary",
        description="美股當日 4 大指標收盤 + 漲跌幅（DJIA, NASDAQ-100, PHLX SOX, TSMC ADR）。"
                    "用於 gap 預測前確認海外影響。回傳 markets list。",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_handler_mcp("mcp_servers/market_data_server.py", "get_us_market_summary"),
        risk_level="low",
    ))

    # 2. Taiwan night futures (MCP, read-only)
    register(ToolSpec(
        name="get_tw_night_futures",
        description="台指期 (TXF) 夜盤收盤價與漲跌幅。預測 gap 時最高權重信號。"
                    "夜盤未開或資料缺時回 {error:true}。",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_handler_mcp("mcp_servers/market_data_server.py", "get_tw_night_futures"),
        risk_level="low",
    ))

    # 3. Taiwan futures chips (MCP, read-only)
    register(ToolSpec(
        name="get_tw_future_chips",
        description="TAIFEX 三大法人台指期留倉部位（外資/投信/自營商的 oi_long, oi_short, oi_net）。"
                    "用於籌碼面分析。",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_handler_mcp("mcp_servers/market_data_server.py", "get_tw_future_chips"),
        risk_level="low",
    ))

    # 4. Financial news (MCP, read-only, optional max_items)
    register(ToolSpec(
        name="get_financial_news",
        description="台股相關財經新聞標題（鉅亨網），預設 15 則。標題已過濾 prompt injection。"
                    "用於識別重大事件對盤勢的潛在影響。",
        input_schema={
            "type": "object",
            "properties": {
                "max_items": {
                    "type": "integer", "minimum": 1, "maximum": 30,
                    "description": "Number of headlines to fetch (default 15)",
                },
            },
            "additionalProperties": False,
        },
        handler=_handler_mcp("mcp_servers/market_data_server.py", "get_financial_news"),
        risk_level="medium",  # input is injected into LLM prompt
    ))

    # 5. Read user portfolio (direct DB)
    register(ToolSpec(
        name="get_user_portfolio",
        description="讀取指定 LINE user 的持倉清單（stock_id, entry_price, quantity, "
                    "stop_loss_level, strategy_type）。user_id 留空則用預設 owner。",
        input_schema={
            "type": "object",
            "properties": {
                "user_id": {"type": ["string", "null"],
                            "description": "LINE userId; null means default owner"},
            },
            "additionalProperties": False,
        },
        handler=_handler_get_user_portfolio,
        risk_level="medium",
    ))

    # 6. Calculate PnL on a holdings list (yfinance + TWSE T86)
    register(ToolSpec(
        name="calculate_pnl",
        description="對給定持倉計算未實現損益、RSI14、MA5/MA20、TWSE 三大法人當日買賣超。"
                    "輸入：get_user_portfolio 的 holdings。中等延遲（每股 1~3s yfinance 呼叫）。",
        input_schema={
            "type": "object",
            "properties": {
                "holdings": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "List of holding dicts (output of get_user_portfolio).",
                },
            },
            "required": ["holdings"],
            "additionalProperties": False,
        },
        handler=_handler_calculate_pnl,
        risk_level="medium",
    ))

    # 7. Stock code → company name (cheap DB lookup)
    register(ToolSpec(
        name="get_stock_name",
        description="把股票代號（如 2330）查回公司中文名（如 台積電）。用於避免在輸出中誤植名稱。",
        input_schema={
            "type": "object",
            "properties": {
                "stock_id": {"type": "string", "pattern": "^[0-9A-Z]{4,6}$"},
            },
            "required": ["stock_id"],
            "additionalProperties": False,
        },
        handler=_handler_get_stock_name,
        risk_level="low",
    ))


_bootstrap_catalog()


# ── Public helpers ────────────────────────────────────────────────────────────

def registered_tool_names() -> list[str]:
    return sorted(_TOOL_REGISTRY.keys())
