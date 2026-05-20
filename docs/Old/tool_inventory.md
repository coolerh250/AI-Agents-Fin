# Tool Inventory
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Scope

This inventory covers every callable unit of work across three categories:

| Category | Definition | Count |
|----------|-----------|-------|
| **MCP Tools** | Formal `@mcp.tool()` decorated functions served over stdio transport | 6 |
| **Direct Function Calls** | Python helpers invoked inside LangGraph nodes via direct import | 14 |
| **LLM Invocations** | `ChatAnthropic.invoke()` calls serving as reasoning tools within agent nodes | 7 |

**Critical finding**: The main production workflow (`investment_workflow.py`) does **not** use MCP tool calls. It calls Python functions directly. The MCP layer exists in two isolated servers that are spawned as subprocesses but are only used by `test_collection.py` and `agent_orchestrator.py`. Tools `save_brief_to_db` and `send_brief_to_user` exist in the MCP server but are **never called** by any workflow client.

---

## Section 1: MCP Tools

### MCP Server A — `mcp_servers/system_inspector.py`
Transport: stdio | Framework: FastMCP | Consumer: `agent_orchestrator.py`

---

#### Tool 1: `get_system_stats`

| Field | Value |
|-------|-------|
| **File** | `mcp_servers/system_inspector.py:14` |
| **Purpose** | Return CPU, memory, and disk usage metrics as structured JSON |
| **External API** | None — reads from local OS via `psutil` |
| **Authentication** | None required (local process) |
| **Risk Level** | 🟢 LOW |
| **Token Usage Impact** | None (no LLM call) |
| **Retry Logic** | None (raises on failure) |
| **Timeout** | psutil `cpu_percent(interval=1)` blocks 1 second |
| **Output Size** | ~400 bytes |
| **Called By** | `agent_orchestrator.py` → `_fetch_mcp_stats()` via MCP session |
| **Notes** | Cold-start subprocess penalty ~1–2 s per invocation; no persistent MCP session |

---

### MCP Server B — `mcp_servers/finance_mcp_server.py`
Transport: stdio | Framework: FastMCP | Consumer: `test_collection.py` only

---

#### Tool 2: `get_tw_future_chips`

| Field | Value |
|-------|-------|
| **File** | `mcp_servers/finance_mcp_server.py:62` |
| **Purpose** | Fetch TAIFEX institutional futures open interest (三大法人) for today |
| **External API** | `https://www.taifex.com.tw/cht/3/futContractsDate` (POST, HTML scrape) |
| **Authentication** | None (public gov website) |
| **Risk Level** | 🟡 MEDIUM |
| **Token Usage Impact** | None (no LLM call) |
| **Retry Logic** | `_retry(fn, retries=1, delay=2.0)` — 1 retry with 2s sleep; falls back from TXF filter to unfiltered |
| **Timeout** | `httpx.Client(timeout=15.0)` |
| **Output Size** | ~300 bytes |
| **Called By** | `test_collection.py` via MCP session |
| **Risk Details** | Screen-scraping TAIFEX HTML (`class="12bk"`, `class="table_f"`) — brittle; government sites change layout without notice. No data available on weekends/holidays → returns `{"error": true}` |

---

#### Tool 3: `get_us_market_summary`

| Field | Value |
|-------|-------|
| **File** | `mcp_servers/finance_mcp_server.py:144` |
| **Purpose** | Fetch previous-day closing prices for DJIA, NASDAQ 100, PHLX SOX, TSMC ADR |
| **External API** | `yfinance` (primary) → `query1.finance.yahoo.com/v8/finance/chart` (fallback) |
| **Authentication** | None (unofficial Yahoo Finance API) |
| **Risk Level** | 🟡 MEDIUM |
| **Token Usage Impact** | None (no LLM call) |
| **Retry Logic** | Per-symbol try/except with yfinance → Yahoo v8 HTTP fallback |
| **Timeout** | yfinance: library default; Yahoo v8 fallback: `httpx.Client(timeout=10.0)` |
| **Output Size** | ~600 bytes |
| **Called By** | `test_collection.py` via MCP session |
| **Risk Details** | yfinance ToS violation risk (unofficial API, no SLA). Yahoo v8 endpoint deprecated historically. Symbol `^NDX` / `^SOX` sometimes return `None` from `fast_info` |

---

#### Tool 4: `get_financial_news`

| Field | Value |
|-------|-------|
| **File** | `mcp_servers/finance_mcp_server.py:197` |
| **Purpose** | Fetch top 15 Taiwan stock news headlines from Anue (鉅亨網) |
| **External API** | `https://api.cnyes.com/media/api/v1/newslist/category/tw_stock` |
| **Authentication** | None (public API, no key required) |
| **Risk Level** | 🟢 LOW |
| **Token Usage Impact** | News titles flow into LLM context via snapshot → data_collector → downstream nodes |
| **Retry Logic** | `_retry(_fetch, retries=1, delay=2.0)` |
| **Timeout** | `httpx.Client(timeout=10.0)` |
| **Output Size** | ~2 KB (15 headlines with titles, times, categories, URLs) |
| **Called By** | `test_collection.py` via MCP session |
| **Risk Details** | News titles appear in LLM prompt without sanitization — **potential prompt injection vector** (a manipulated headline could attempt to override system prompt). Anue API is unofficial and undocumented |

---

#### Tool 5: `save_brief_to_db` ⚠️ ORPHANED

| Field | Value |
|-------|-------|
| **File** | `mcp_servers/finance_mcp_server.py:233` |
| **Purpose** | Save a daily brief to TiDB `daily_briefs` table |
| **External API** | TiDB (MySQL-compatible, remote) |
| **Authentication** | `TIDB_USER`, `TIDB_PASSWORD` env vars (inherited from parent process) |
| **Risk Level** | 🔴 HIGH |
| **Token Usage Impact** | None (no LLM call) |
| **Retry Logic** | None |
| **Timeout** | None (SQLAlchemy default, unbounded) |
| **Output Size** | `{"success": bool, "row_id": int}` |
| **Called By** | **NOBODY** — declared in MCP server, no client calls it |
| **Risk Details** | Unrestricted write to production database. `trade_date` is string — only validated by `date.fromisoformat()`. No duplicate prevention (no UNIQUE constraint on `daily_briefs.trade_date`). Finance MCP server subprocess has full DB write access via inherited credentials |

---

#### Tool 6: `send_brief_to_user` ⚠️ ORPHANED

| Field | Value |
|-------|-------|
| **File** | `mcp_servers/finance_mcp_server.py:256` |
| **Purpose** | Push investment brief to LINE Messaging API and Telegram |
| **External API** | LINE `api.line.me/v2/bot/message/push`, Telegram `api.telegram.org` |
| **Authentication** | `LINE_CHANNEL_ACCESS_TOKEN`, `LINE_USER_ID`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` env vars |
| **Risk Level** | 🔴 HIGH |
| **Token Usage Impact** | None (no LLM call) |
| **Retry Logic** | None |
| **Timeout** | `httpx.Client(timeout=10.0)` inside `messenger_tools` |
| **Output Size** | `{"line": {...}, "telegram": {...}}` |
| **Called By** | **NOBODY** — declared in MCP server, no client calls it |
| **Risk Details** | Can push arbitrary text to the user's LINE account. No input length validation. Finance MCP server subprocess has LINE/Telegram credentials via env inheritance. If an MCP client (e.g., Claude Desktop) were connected, an LLM could call this tool to send messages |

---

## Section 2: Direct Function Calls (Non-MCP "Tools")

These functions are called via Python `import` inside LangGraph nodes. They are not wrapped in MCP or any tool-calling protocol.

### Group A: Database Layer — `database_tools.py`

| Function | Purpose | Operation | Auth | Risk |
|----------|---------|-----------|------|------|
| `save_brief()` | Insert daily brief | `INSERT INTO daily_briefs` | TiDB credentials (env) | 🟡 MEDIUM — no UNIQUE guard |
| `get_brief()` | Read daily brief | `SELECT ... LIMIT 1` | TiDB credentials (env) | 🟢 LOW |
| `save_actual()` | Upsert TAIEX actuals | `INSERT ... ON DUPLICATE KEY UPDATE market_actuals` | TiDB credentials (env) | 🟢 LOW |
| `get_actual()` | Read TAIEX actuals | `SELECT * FROM market_actuals` | TiDB credentials (env) | 🟢 LOW |
| `log_cost()` | Append cost record | `INSERT INTO cost_logs` | TiDB credentials (env) | 🟢 LOW |
| `get_cost_summary()` | Aggregate cost stats | `SELECT ... GROUP BY` | TiDB credentials (env) | 🟢 LOW |
| `get_cost_trend()` | Daily cost trend | `SELECT DATE(logged_at)...` | TiDB credentials (env) | 🟢 LOW |
| `get_portfolio()` | Read all holdings | `SELECT * FROM user_portfolio` | TiDB credentials (env) | 🟢 LOW |
| `add_portfolio_item()` | Insert holding | `INSERT INTO user_portfolio` | TiDB credentials (env) | 🟡 MEDIUM — Streamlit form input |
| `delete_portfolio_item()` | Delete holding by ID | `DELETE FROM user_portfolio WHERE id=?` | TiDB credentials (env) | 🟡 MEDIUM — no ownership check |
| `update_portfolio_item()` | Update holding fields | `UPDATE user_portfolio WHERE id=?` | TiDB credentials (env) | 🟡 MEDIUM — no ownership check |
| `get_recent_accuracy()` | JOIN briefs+actuals | `SELECT ... LEFT JOIN` | TiDB credentials (env) | 🟢 LOW |

**Key risk**: `_engine()` creates a new SQLAlchemy connection pool on every call. With 6+ LLM nodes each calling `log_cost()` plus dashboard calls, this creates 6–10+ pools per workflow run. See T3-C in production recommendation.

### Group B: Portfolio Layer — `portfolio_tools.py`

| Function | Purpose | External API | Auth | Risk |
|----------|---------|-------------|------|------|
| `get_user_portfolio()` | Proxy to `database_tools.get_portfolio()` | TiDB | env vars | 🟢 LOW |
| `calculate_pnl()` | Enrich holdings with live prices via yfinance | `yfinance` (Yahoo Finance) | None | 🟡 MEDIUM — unofficial API, fallback to entry_price silently |

**Token usage impact**: `calculate_pnl()` result (~200 tokens/holding) flows into `portfolio_manager_node` Sonnet prompt.

### Group C: Notification Layer — `messenger_tools.py`

| Function | Purpose | External API | Auth | Risk |
|----------|---------|-------------|------|------|
| `format_brief()` | Regex-extract sections from brief | None | None | 🟢 LOW |
| `send_line()` | Push message to LINE | `api.line.me/v2/bot/message/push` | `LINE_CHANNEL_ACCESS_TOKEN` bearer | 🔴 HIGH |
| `send_telegram()` | Push message to Telegram | `api.telegram.org/bot{token}/sendMessage` | `TELEGRAM_BOT_TOKEN` | 🔴 HIGH |
| `send_brief()` | Orchestrate LINE + Telegram | Both above | Both tokens | 🔴 HIGH |

**Risk**: No retry logic. No rate limiting. No message length enforcement before LINE API call (LINE max 5000 chars). `format_brief()` regex extracts sections but does not sanitize LLM-generated text before pushing to real users.

### Group D: Market Data Layer — `twse_fetcher.py`

| Function | Purpose | External API | Auth | Risk |
|----------|---------|-------------|------|------|
| `get_taiex_actuals()` | Fetch TAIEX close/change% | `https://www.twse.com.tw/exchangeReport/MI_INDEX` | None (public) | 🟡 MEDIUM |

**Critical risk**: `verify=False` disables TLS certificate verification for TWSE endpoint — susceptible to MITM attack. TWSE uses a legacy TW government CA that lacks Subject Key Identifier; this is documented in the code but not mitigated. On a production system connected to a financial workflow, this is exploitable.

---

## Section 3: LLM Invocations

These are `ChatAnthropic.invoke()` calls — each is a "reasoning tool" that transforms data.

**Important**: None of these use LangChain `bind_tools()` / function calling. All structure is enforced via prompt engineering (JSON output instructions in system prompts), not tool schemas. This means no `tool_use` blocks, no argument validation, and no schema enforcement.

| LLM Node | File | Model | System Prompt Goal | Input Size | Output Size | Token Impact |
|----------|------|-------|-------------------|-----------|-------------|-------------|
| `data_collector_node` | `market_analyst_agents.py:163` | Haiku 4.5 | Extract 9 numeric fields as JSON | ~3 KB snapshot | ~200 bytes | 🟢 LOW |
| `chip_analyst_node` | `market_analyst_agents.py:192` | Sonnet 4.6 | Analyze 3 OI numbers → JSON sentiment | ~200 bytes | ~300 bytes | 🟢 LOW |
| `tech_analyst_node` | `market_analyst_agents.py:215` | Sonnet 4.6 | Weight 4 US market pcts → gap prediction JSON | ~200 bytes | ~300 bytes | 🟢 LOW |
| `chief_strategist_node` | `market_analyst_agents.py:239` | Opus 4.7 + Thinking | Synthesize chip+tech → investment brief | ~600 bytes | ~2 KB | 🔴 HIGH (unbounded thinking) |
| `portfolio_manager_node` | `market_analyst_agents.py:261` | Sonnet 4.6 | Apply brief to portfolio → per-holding advice | ~3 KB | ~1 KB/holding | 🟡 MEDIUM |
| `format_agent_node` | `market_analyst_agents.py:297` | Haiku 4.5 | Reformat brief as LINE message ≤2000 chars | ~3 KB | ~2 KB | 🟢 LOW |
| `evaluate_node` | `backtest_agent.py:106` | Haiku 4.5 | Compare prediction vs actual → accuracy report | ~2 KB | ~1 KB | 🟢 LOW |
| `think_node` | `agent_orchestrator.py:91` | Haiku 4.5 | Interpret system stats → READY/WARNING/CRITICAL | ~500 bytes | ~500 bytes | 🟢 LOW |

---

## Section 4: MCP Resources (Non-Tool)

| Resource URI | File | Purpose | Auth | Risk |
|-------------|------|---------|------|------|
| `finance://backtest/report` | `mcp_servers/finance_mcp_server.py:270` | Return last 5 accuracy rows as Markdown table | TiDB env vars | 🟢 LOW (read-only) |

Called via `session.read_resource()` — not invoked by any current client.

---

## Section 5: Tool Architecture Summary

```
┌─────────────────────────────────────────────────────────┐
│  PRODUCTION WORKFLOW (investment_workflow.py)           │
│  ─ Does NOT use MCP at runtime                         │
│  ─ Direct Python imports for ALL side effects:         │
│                                                         │
│   database_tools.save_brief()      ← TiDB write        │
│   database_tools.log_cost()        ← TiDB write        │
│   portfolio_tools.calculate_pnl()  ← yfinance HTTP     │
│   messenger_tools.send_brief()     ← LINE + Telegram   │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│  DATA COLLECTION (test_collection.py)                   │
│  ─ Uses MCP via stdio subprocess                        │
│                                                         │
│   finance_mcp_server → get_tw_future_chips   ← TAIFEX  │
│   finance_mcp_server → get_us_market_summary ← yfinance│
│   finance_mcp_server → get_financial_news    ← Anue    │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│  MAINTENANCE AGENT (agent_orchestrator.py)              │
│  ─ Uses MCP via stdio subprocess                        │
│                                                         │
│   system_inspector → get_system_stats        ← psutil  │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│  ORPHANED MCP TOOLS (finance_mcp_server.py)             │
│  ─ Declared but never called by any client              │
│                                                         │
│   save_brief_to_db    (bypassed: direct DB call)        │
│   send_brief_to_user  (bypassed: direct messenger call) │
└─────────────────────────────────────────────────────────┘
```

---

## Section 6: Authentication Methods Summary

| Credential | Used By | Storage | Rotation Policy |
|-----------|---------|---------|-----------------|
| `ANTHROPIC_API_KEY` | All LLM nodes | `.env` file | None defined |
| `TIDB_HOST/PORT/USER/PASSWORD/DB` | All database_tools calls | `.env` file | None defined |
| `LINE_CHANNEL_ACCESS_TOKEN` | `send_line()`, orphaned MCP tool | `.env` file | Manual (LINE dashboard) |
| `LINE_USER_ID` | `send_line()` | `.env` file | Static user ID |
| `TELEGRAM_BOT_TOKEN` | `send_telegram()`, orphaned MCP tool | `.env` file | None defined |
| `TELEGRAM_CHAT_ID` | `send_telegram()` | `.env` file | Static chat ID |

**Critical gap**: MCP subprocesses inherit the parent environment (`env=None` in `StdioServerParameters`). This means both MCP servers have access to ALL credentials, including LINE/Telegram tokens, even when they only need TAIFEX/yfinance access (which require no credentials).
