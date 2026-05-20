# Recommended MCP Architecture
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Executive Summary

The current tool architecture has a structural gap: MCP exists as a data collection layer (`test_collection.py`) but the production workflow bypasses it entirely, using direct Python imports for all side effects. This leaves DB writes, portfolio price fetching, and LINE/Telegram notifications outside any governance boundary.

The recommended architecture defines **four purpose-scoped MCP servers**, each carrying only the credentials it needs. The production workflow calls these servers over `stdio` (or optionally `SSE/HTTP` for long-running connections). Every tool invocation is logged to a `tool_audit_log` table. No tool with write or notify permissions is accessible without an internal API key.

**This is not a rewrite.** The LangGraph node logic, state schema, and graph topology remain unchanged. The change is in how nodes reach their external dependencies.

---

## Architectural Principles

1. **Least privilege per server**: Each MCP server receives only the credentials it needs. A server that only reads market data gets no DB password. A server that only pushes notifications gets no TiDB credentials.

2. **Every side effect is a named tool**: DB writes, message delivery, and market data fetches are all `@mcp.tool()` calls — observable, auditable, and replaceable without changing workflow code.

3. **Write tools require internal tokens**: Any tool that modifies state (`save_brief_to_db`, `push_notification`) requires an `_api_key` parameter checked against an environment variable. LLM-accessible tools never have write access by default.

4. **The workflow stays synchronous until T3-E**: MCP calls from the workflow use `asyncio.run()` or a lightweight sync wrapper. Full async migration happens in T3-E (see production_architecture_recommendation.md).

5. **No orphaned tools**: Every tool in every server has at least one named consumer. Tools with no consumers are removed.

---

## Target Architecture: Four MCP Servers

```
┌─────────────────────────────────────────────────────────────────┐
│  CRON PIPELINE (weekdays)                                       │
│                                                                 │
│  08:00 ── test_collection.py ──────► market_data_server        │
│  │            reads 3 tools                (TAIFEX, yfinance,  │
│  │            saves snapshot.json           Anue, TWSE)        │
│  │                                                              │
│  08:20 ── investment_workflow.py                                │
│  │            LangGraph 8-node graph                           │
│  │            ├── data_collector → reads snapshot              │
│  │            ├── chip_analyst (Sonnet)                        │
│  │            ├── tech_analyst (Sonnet)                        │
│  │            ├── chief_strategist (Opus)                      │
│  │            ├── portfolio_manager ──► market_data_server     │
│  │            │                         (get_portfolio_pnl)    │
│  │            ├── format_agent (Haiku)                         │
│  │            ├── save_to_db ──────────► persistence_server    │
│  │            │                          (save_brief_to_db)    │
│  │            └── send_notification ──► notification_server    │
│  │                                       (push_investment_brief)│
│  │                                                              │
│  09:00 ── backtest_agent.py                                     │
│              ├── load_brief ────────────► persistence_server   │
│              │                            (get_brief)          │
│              ├── fetch_actual ──────────► market_data_server   │
│              │                            (get_taiex_actuals)  │
│              └── evaluate (Haiku) ──────► persistence_server   │
│                                           (save_accuracy)      │
│                                                                 │
│  MANUAL ── agent_orchestrator.py ───────► system_server        │
│               think (Haiku)               (get_system_stats)   │
└─────────────────────────────────────────────────────────────────┘
```

---

## Server 1: `mcp_servers/market_data_server.py`

**Purpose**: Read-only external market data fetching  
**Replaces**: `mcp_servers/finance_mcp_server.py` (tools only)  
**Credentials needed**: None (all public APIs)  
**Consumers**: `test_collection.py`, `portfolio_manager_node`, `backtest_agent.py`

### Tools

```python
from mcp.server.fastmcp import FastMCP
import httpx, yfinance as yf, asyncio, os
from datetime import date, datetime, timezone

mcp = FastMCP("market-data")

@mcp.tool()
async def get_tw_future_chips() -> dict:
    """Fetch TAIFEX institutional traders futures open interest (三大法人).
    Returns data for today. Returns {"error": true} on holiday/failure."""

@mcp.tool()
async def get_us_indices() -> dict:
    """Fetch DJIA, NASDAQ 100, PHLX SOX previous-day closing data."""

@mcp.tool()
async def get_tsm_adr() -> dict:
    """Fetch TSMC ADR (TSM) previous-day closing price and change %."""

@mcp.tool()
async def get_financial_news(max_items: int = 15) -> dict:
    """Fetch Taiwan stock news headlines from Anue. Titles sanitized."""

@mcp.tool()
async def get_taiex_actuals(trade_date: str, max_lookback: int = 5) -> dict:
    """Fetch TAIEX close/change data from TWSE for trade_date (YYYY-MM-DD).
    Looks back up to max_lookback days on holiday. Requires custom CA bundle.
    Returns: {trade_date, open_price, close_price, actual_gap_pct, source}"""

@mcp.tool()
async def get_portfolio_pnl(cache_ttl_seconds: int = 300) -> dict:
    """Return user portfolio from TiDB enriched with live yfinance prices.
    Includes price_stale flag per holding when yfinance returns no data.
    Credentials: TIDB_* (read-only)"""
```

**Key changes from current**:
- `get_us_market_summary` split into `get_us_indices` + `get_tsm_adr`
- `get_financial_news` adds keyword-based title sanitization
- `get_taiex_actuals` promoted from direct call to MCP tool
- `get_portfolio_pnl` promoted from direct call to MCP tool with `price_stale` flag
- All functions are `async` — no blocking `time.sleep()` in retry logic
- `verify=False` replaced with `verify="/etc/ssl/certs/twse-ca.crt"` (custom CA)

**Env vars required**:
```
# market_data_server only
TIDB_HOST=...         # for get_portfolio_pnl only
TIDB_USER=...
TIDB_PASSWORD=...
TIDB_PORT=...
TIDB_DB=...
# LINE/Telegram: NOT present
# ANTHROPIC_API_KEY: NOT present
```

---

## Server 2: `mcp_servers/persistence_server.py`

**Purpose**: TiDB read and write operations  
**Replaces**: Direct `database_tools` imports in workflow nodes  
**Credentials needed**: TiDB only  
**Consumers**: `investment_workflow.py`, `backtest_agent.py`, `dashboard.py` (read-only tab)

### Tools

```python
mcp = FastMCP("persistence")

# ── Write tools (require _api_key) ────────────────────────────

@mcp.tool()
def save_brief(
    trade_date: str,
    brief_text: str,
    predicted_gap_pct: Optional[float],
    gap_direction: Optional[str],
    _api_key: str = "",
) -> dict:
    """Save daily brief to TiDB daily_briefs. Requires internal API key.
    trade_date: YYYY-MM-DD. Returns {"success": bool, "row_id": int}"""
    if _api_key != os.getenv("MCP_WRITE_TOKEN", ""):
        return {"success": False, "error": "unauthorized"}

@mcp.tool()
def save_actual(
    trade_date: str,
    open_price: float,
    close_price: float,
    actual_gap_pct: float,
    notes: str = "",
    _api_key: str = "",
) -> dict:
    """Upsert TAIEX actuals to market_actuals. Requires internal API key."""
    if _api_key != os.getenv("MCP_WRITE_TOKEN", ""):
        return {"success": False, "error": "unauthorized"}

@mcp.tool()
def save_accuracy_report(
    trade_date: str,
    score: int,
    report_text: str,
    _api_key: str = "",
) -> dict:
    """Persist backtest accuracy report. Requires internal API key.
    Score: 0–100. Creates accuracy_logs table row."""
    if _api_key != os.getenv("MCP_WRITE_TOKEN", ""):
        return {"success": False, "error": "unauthorized"}

# ── Read tools (no auth required) ─────────────────────────────

@mcp.tool()
def get_brief(trade_date: str) -> dict:
    """Fetch most recent brief for trade_date (YYYY-MM-DD)."""

@mcp.tool()
def get_recent_accuracy(days: int = 7) -> list:
    """Return last N trade days joining daily_briefs + market_actuals."""

@mcp.tool()
def get_cost_summary(days: int = 30) -> list:
    """Aggregated LLM cost by agent/model for last N days."""
```

**Env vars required**:
```
# persistence_server only
TIDB_HOST=...
TIDB_USER=...
TIDB_PASSWORD=...
TIDB_PORT=...
TIDB_DB=...
MCP_WRITE_TOKEN=<32-char-hex>
# LINE/Telegram: NOT present
# ANTHROPIC_API_KEY: NOT present
```

---

## Server 3: `mcp_servers/notification_server.py`

**Purpose**: LINE and Telegram push notifications  
**Replaces**: Direct `messenger_tools` imports in `send_notification_node`  
**Credentials needed**: LINE and Telegram tokens only (no DB credentials)  
**Consumers**: `investment_workflow.py` (`send_notification_node`)

### Tools

```python
mcp = FastMCP("notification")

@mcp.tool()
def push_investment_brief(brief_text: str, _api_key: str = "") -> dict:
    """Format and push investment brief to all configured channels.
    Applies format_brief() transformation. Deduplicates by trade date.
    Requires internal API key.
    Returns: {"line": {...}, "telegram": {...}, "dedup_skipped": bool}"""
    if _api_key != os.getenv("MCP_NOTIFY_TOKEN", ""):
        return {"error": "unauthorized"}

    # Deduplication: check if already sent today
    if _already_sent_today():
        logger.warning("[Notification] Already sent today, skipping")
        return {"line": {"status": "skipped", "reason": "already_sent_today"},
                "telegram": {"status": "skipped", "reason": "already_sent_today"},
                "dedup_skipped": True}

    message = format_brief(brief_text)
    if len(message) > 4000:
        message = message[:4000]  # LINE hard limit

    line_result = send_line(message)
    tg_result   = send_telegram(message)

    # Persist notification log (uses NOTIFICATION_LOG_DB or writes to file)
    _log_notification(line_result, tg_result)

    return {"line": line_result, "telegram": tg_result, "dedup_skipped": False}


@mcp.tool()
def push_raw(channel: str, message: str, _api_key: str = "") -> dict:
    """Push raw text to a single channel. For testing/manual alerts.
    channel: 'line' | 'telegram'. Requires internal API key."""
    if _api_key != os.getenv("MCP_NOTIFY_TOKEN", ""):
        return {"error": "unauthorized"}
```

**Env vars required**:
```
# notification_server only
LINE_CHANNEL_ACCESS_TOKEN=...
LINE_USER_ID=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
MCP_NOTIFY_TOKEN=<32-char-hex>
# TIDB: NOT present
# ANTHROPIC_API_KEY: NOT present
```

---

## Server 4: `mcp_servers/system_server.py`

**Purpose**: Local system health monitoring  
**Replaces**: `mcp_servers/system_inspector.py` (rename only — no content change)  
**Credentials needed**: None  
**Consumers**: `agent_orchestrator.py`

No changes required beyond rename for consistency with server naming scheme.

---

## Tool Audit Log — Cross-Cutting Concern

Every MCP tool call should be logged to a `tool_audit_log` table:

```sql
CREATE TABLE tool_audit_log (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    called_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    server      VARCHAR(50) NOT NULL,
    tool_name   VARCHAR(100) NOT NULL,
    caller      VARCHAR(50),         -- workflow name, e.g. "investment_workflow"
    status      VARCHAR(20) NOT NULL, -- "ok" | "error" | "unauthorized" | "skipped"
    latency_ms  INT,
    error_msg   TEXT,
    INDEX idx_called_at (called_at),
    INDEX idx_tool_name (tool_name)
);
```

Add to each MCP server as a `@mcp.middleware()` or as a wrapper decorator:

```python
def _audit(server: str, tool_name: str, caller: str = ""):
    """Context manager that logs tool invocation outcome."""
    import contextlib, time
    @contextlib.contextmanager
    def _ctx():
        t0 = time.monotonic()
        status = "ok"
        error_msg = None
        try:
            yield
        except Exception as exc:
            status = "error"
            error_msg = str(exc)
            raise
        finally:
            latency = int((time.monotonic() - t0) * 1000)
            try:
                _log_audit(server, tool_name, caller, status, latency, error_msg)
            except Exception:
                pass  # audit failures must never crash tool execution
    return _ctx()
```

---

## How Investment Workflow Calls MCP Tools (Transition Pattern)

For the transition period (before full async migration), workflow nodes can call MCP tools using a lightweight sync wrapper:

```python
# utils/mcp_call.py — sync wrapper for MCP tool calls
import asyncio, json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def call_mcp_tool_sync(
    server_script: str,
    tool_name: str,
    arguments: dict,
    timeout: float = 30.0,
) -> dict:
    """Synchronously call a single MCP tool from a stdio server.
    Safe to call from synchronous LangGraph nodes.
    Raises RuntimeError if called from inside a running event loop (use ainvoke instead).
    """
    async def _inner():
        params = StdioServerParameters(
            command="uv",
            args=["run", server_script],
            env=_minimal_env(server_script),  # credential whitelist per server
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                return json.loads(result.content[0].text)

    return asyncio.run(_inner())
```

Usage inside a LangGraph node:
```python
def save_to_db_node(state: WorkflowState) -> dict:
    from utils.mcp_call import call_mcp_tool_sync
    result = call_mcp_tool_sync(
        server_script="mcp_servers/persistence_server.py",
        tool_name="save_brief",
        arguments={
            "trade_date": state["trade_date"],
            "brief_text": state["final_brief"],
            "predicted_gap_pct": gap_pct,
            "gap_direction": gap_dir,
            "_api_key": os.getenv("MCP_WRITE_TOKEN"),
        },
    )
    return {"db_row_id": result.get("row_id")}
```

**Performance cost**: Each `asyncio.run()` spawns a subprocess (~1–2 s cold start). For a daily cron job with 3 MCP tool calls at the end of a 40-second workflow, the overhead is ~4–6 s. Acceptable short-term.

For `portfolio_manager_node` (called mid-workflow), the subprocess cold start adds to the critical path. This is the primary reason to prioritize async migration for this node.

---

## Migration from Current State

### Immediate (P0, <1 hour total)

1. Remove `save_brief_to_db` and `send_brief_to_user` from `finance_mcp_server.py`
2. Add explicit env whitelist to all `StdioServerParameters` calls
3. Add title sanitization to `get_financial_news`

### Week 1 (alongside LangGraph hardening)

1. Create `market_data_server.py` (based on `finance_mcp_server.py`, read-only tools only)
2. Add `get_taiex_actuals` to market_data_server
3. Add `get_portfolio_pnl` to market_data_server (with `price_stale` flag)
4. Create `notification_server.py` with auth guard and dedup
5. Update `test_collection.py` to use `market_data_server` instead of `finance_mcp_server`

### Week 2 (persistence layer)

1. Create `persistence_server.py` with auth guards on write tools
2. Add `save_accuracy_report` tool (implements T2-D from production recommendation)
3. Create `tool_audit_log` table in TiDB
4. Update `backtest_agent.py` to use MCP for `save_actual` and `save_accuracy_report`

### Month 2 (full workflow integration)

1. Update `investment_workflow.py` to use MCP for `save_to_db` and `send_notification`
2. Async migration of workflow nodes (coordinate with T3-E)
3. Replace `asyncio.run()` wrappers with `await session.call_tool()` via `graph.ainvoke()`

---

## Architecture Diagram (Target State)

```
┌────────────────────────────────────────────────────────────────────────┐
│  TOOL GOVERNANCE BOUNDARY                                              │
│                                                                        │
│  ┌─ market_data_server ──────────┐  Env: (no credentials needed)      │
│  │  get_tw_future_chips    🌐    │  Consumers: test_collection,        │
│  │  get_us_indices         🌐    │             portfolio_manager_node  │
│  │  get_tsm_adr            🌐    │             backtest_agent          │
│  │  get_financial_news     🌐    │                                     │
│  │  get_taiex_actuals      🌐    │  Rate limit: 10 calls/hour          │
│  │  get_portfolio_pnl      🌐📖  │  Title sanitization: enabled        │
│  └───────────────────────────────┘  TLS: custom CA (no verify=False)  │
│                                                                        │
│  ┌─ persistence_server ──────────┐  Env: TIDB_* only                  │
│  │  save_brief           ✍️ 🔑   │  Consumers: save_to_db_node         │
│  │  save_actual          ✍️ 🔑   │             fetch_actual_node       │
│  │  save_accuracy_report ✍️ 🔑   │             backtest_agent          │
│  │  get_brief            📖      │  Write: requires MCP_WRITE_TOKEN    │
│  │  get_recent_accuracy  📖      │  Read: no auth (internal use)       │
│  │  get_cost_summary     📖      │  Engine: @lru_cache singleton       │
│  └───────────────────────────────┘  Audit: tool_audit_log             │
│                                                                        │
│  ┌─ notification_server ─────────┐  Env: LINE_* + TELEGRAM_* only     │
│  │  push_investment_brief 📣 🔑  │  Consumers: send_notification_node  │
│  │  push_raw              📣 🔑  │  Write: requires MCP_NOTIFY_TOKEN   │
│  └───────────────────────────────┘  Dedup: by trade date              │
│                                      Audit: notification_log           │
│                                                                        │
│  ┌─ system_server ───────────────┐  Env: none                         │
│  │  get_system_stats      📖     │  Consumers: agent_orchestrator      │
│  └───────────────────────────────┘                                     │
└────────────────────────────────────────────────────────────────────────┘
```

---

## Security Checklist for Target Architecture

| Check | Current | Target |
|-------|---------|--------|
| Write tools require auth | ❌ None | ✅ `MCP_WRITE_TOKEN` required |
| Notify tools require auth | ❌ None | ✅ `MCP_NOTIFY_TOKEN` required |
| Subprocess credential isolation | ❌ Inherits all env | ✅ Per-server env whitelist |
| News content sanitized | ❌ Raw titles in LLM | ✅ Keyword filter applied |
| TLS enforcement | ❌ `verify=False` for TWSE | ✅ Custom CA bundle |
| Notification deduplication | ❌ None | ✅ By trade date |
| Tool invocation audit | ❌ None | ✅ `tool_audit_log` table |
| Orphaned tools with permissions | ❌ 2 orphaned tools | ✅ Removed or guarded |
| Portfolio price stale flag | ❌ Silent fallback | ✅ `price_stale` in response |
| DB connection pool singleton | ❌ Per-call pool | ✅ `@lru_cache` singleton |

---

## Cost Impact of MCP Migration

| Change | Current Cost | After Change | Delta |
|--------|-------------|-------------|-------|
| `asyncio.run()` per node | 0 s (sync) | +2–3 s cold start per MCP call | +5 s/run |
| Portfolio pnl via MCP | Same yfinance call | No change | $0 |
| `save_to_db` via MCP | Direct TiDB call | Same TiDB call | $0 |
| `send_notification` via MCP | Direct httpx | Same httpx inside server | $0 |
| Audit log writes | Not tracked | +2–3 tiny TiDB inserts/run | ~$0.0001/run |
| **Total incremental cost** | | | **~$0/run (negligible)** |

The cold-start overhead (~5 s added) is the only meaningful cost. This is resolved by T3-E async migration which allows persistent MCP sessions rather than per-call subprocess spawns.
