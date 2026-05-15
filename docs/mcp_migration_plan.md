# MCP Migration Plan
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Current MCP Status

```
Already MCP (stdio server):     get_system_stats, get_tw_future_chips,
                                get_us_market_summary, get_financial_news,
                                save_brief_to_db (orphaned), send_brief_to_user (orphaned)

Not MCP (direct imports):       ALL database_tools functions
                                messenger_tools.send_brief / send_line / send_telegram
                                portfolio_tools.calculate_pnl
                                twse_fetcher.get_taiex_actuals
```

**The core problem**: The production workflow (`investment_workflow.py`) bypasses the existing MCP layer entirely. The investment workflow's side effects (DB writes, LINE push) are direct Python calls with no observability, no permission control, and no standard interface.

---

## Analysis: Which Tools to MCP-ify

### Category A: Already MCP — Keep and Improve

These tools are correctly placed as MCP tools. The gaps are governance-related, not architectural.

| Tool | Keep? | Improvement Needed |
|------|-------|-------------------|
| `get_system_stats` | ✅ YES | Add structured error response instead of `raise` |
| `get_tw_future_chips` | ✅ YES | Add `date` param for historical queries; fix blocking `_retry()` |
| `get_us_market_summary` | ✅ YES | Add TTL cache to avoid repeated yfinance calls in same session |
| `get_financial_news` | ✅ YES | Add title sanitization for prompt injection defense |
| `save_brief_to_db` | ✅ YES (redesign) | Remove from finance_mcp_server; move to dedicated write-server with auth |
| `send_brief_to_user` | ✅ YES (redesign) | Remove from finance_mcp_server; move to dedicated notification-server with auth |

---

### Category B: Should Be MCP-ified (High Priority)

These tools are currently direct-call Python functions with real external side effects. Wrapping them as MCP tools brings observability, timeouts, and standard error handling.

#### B1: `twse_fetcher.get_taiex_actuals` → MCP Tool

**Why MCP**: TWSE HTTP call with SSL risk (`verify=False`). Making it MCP isolates the TLS bypass to a single well-defined boundary point that can be improved (custom CA bundle) without touching workflow code.

**New tool spec**:
```python
@mcp.tool()
def get_taiex_actuals(trade_date: str, max_lookback: int = 5) -> dict:
    """Fetch TAIEX close/change data from TWSE for a given trade date (YYYY-MM-DD).
    Returns up to max_lookback days of lookback if the exact date has no data."""
```

**MCP server**: New `mcp_servers/market_data_server.py` (see Section C below).

---

#### B2: `messenger_tools.send_brief` / `send_line` / `send_telegram` → MCP Tools

**Why MCP**: LINE and Telegram pushes are the highest-impact side effects in the system. Wrapping them as MCP tools enables:
1. Audit logging of every push (what was sent, when, by which workflow run)
2. Rate limiting (prevent duplicate pushes on same day)
3. Credential isolation (notification server receives only LINE/Telegram tokens, not DB credentials)

**New tool specs**:
```python
@mcp.tool()
def push_notification(channel: str, message: str) -> dict:
    """Push a message to a notification channel.
    channel: 'line' | 'telegram' | 'all'
    message: UTF-8 text, max 4000 chars
    Returns: {"channel": str, "status": "ok"|"skipped"|"error", ...}
    """

@mcp.tool()
def push_investment_brief(brief_text: str) -> dict:
    """Format and push investment brief to all configured channels.
    Applies format_brief() transformation before sending.
    Returns per-channel delivery status."""
```

**MCP server**: New `mcp_servers/notification_server.py`.

---

#### B3: `portfolio_tools.calculate_pnl` → MCP Tool

**Why MCP**: This function calls yfinance (external HTTP) and TiDB. Making it MCP:
1. Makes yfinance calls observable (latency, error rate)
2. Isolates the yfinance ToS risk to one subprocess
3. Enables caching at the MCP layer (same price used by both `portfolio_manager_node` and `dashboard.py`)

**New tool spec**:
```python
@mcp.tool()
def get_portfolio_with_pnl(cache_ttl_seconds: int = 300) -> dict:
    """Return user portfolio enriched with live P&L from yfinance.
    Includes price_stale flag when yfinance returns no data.
    Returns: {"holdings": [...], "fetched_at": str, "stale_symbols": [...]}"""
```

**MCP server**: New `mcp_servers/portfolio_server.py` (or merged into market_data_server).

---

### Category C: Should NOT Be MCP-ified

These functions should remain as direct Python calls.

| Function | Reason to Keep Direct |
|----------|----------------------|
| `database_tools.log_cost()` | Internal telemetry, called ~6×/run. MCP overhead would double latency per node |
| `database_tools.save_brief()` | Already has MCP counterpart (`save_brief_to_db`). The direct call is acceptable within the same process |
| `database_tools.get_*()` queries | Read-only, internal to workflows. No external API, low risk, no observability need |
| `messenger_tools.format_brief()` | Pure string transformation, no I/O. Keep as utility function |
| `_llm()` / `_llm_opus()` factory | LLM invocations are not "tools" in the MCP sense; they're the agent reasoning layer |

**Rule of thumb**: Tools that have external side effects (network I/O, DB writes, message delivery) benefit from MCP. Pure computation and internal read-only DB queries do not.

---

### Category D: Should Be Split

These tools currently combine multiple concerns that should be separated.

#### D1: `get_us_market_summary` — Split into two

Currently combines:
- US index fetching (DJIA, NASDAQ, SOX)
- TSMC ADR fetching (Taiwan-specific)

These have different use cases and consumers. Split into:

```python
@mcp.tool()
def get_us_indices() -> dict:
    """Fetch DJIA, NASDAQ 100, PHLX SOX closing data."""

@mcp.tool()
def get_tsm_adr() -> dict:
    """Fetch TSMC ADR (TSM) closing price and change."""
```

This allows `tech_analyst_node` to receive disaggregated data and express TSM ADR as a special signal rather than averaging it with US indices.

#### D2: `finance_mcp_server.py` — Split into three servers

Currently one server mixes read-only market data tools (`get_tw_future_chips`, `get_us_market_summary`, `get_financial_news`) with write-permission tools (`save_brief_to_db`, `send_brief_to_user`). This violates least privilege.

**Split into**:
- `mcp_servers/market_data_server.py` — read-only market data (TAIFEX, yfinance, Anue)
- `mcp_servers/persistence_server.py` — DB write tools (`save_brief_to_db`, `save_actual`)
- `mcp_servers/notification_server.py` — LINE/Telegram push tools

---

### Category E: Security Risk — Requires Immediate Action Before MCP Expansion

These tools have security issues that must be fixed **before** exposing them to any LLM-accessible MCP client.

| Tool | Security Issue | Action Before MCP |
|------|---------------|-------------------|
| `send_brief_to_user` | Any MCP client can push to user's LINE | Add `_api_key` param + env var check |
| `save_brief_to_db` | Any MCP client can INSERT to production DB | Add `_api_key` param + env var check |
| `get_financial_news` | News titles → LLM prompts without sanitization | Add keyword filter on titles |
| All tools | Subprocess inherits all env vars | Use explicit env whitelist per subprocess |

---

## Migration Phases

### Phase 1: Security Hardening (Before any new MCP work)
**Estimated effort**: 2 hours

1. Remove `save_brief_to_db` and `send_brief_to_user` from `finance_mcp_server.py` (or add `_api_key` guard)
2. Add explicit env whitelist to all `StdioServerParameters(env=...)` calls
3. Add news title sanitization in `get_financial_news`
4. Fix `verify=False` in `twse_fetcher.py` (custom CA or bypass with documented exception)

### Phase 2: MCP Server Restructuring
**Estimated effort**: 3 hours

1. Create `mcp_servers/market_data_server.py`:
   - Move `get_tw_future_chips` from finance_mcp_server
   - Move `get_us_market_summary` → split into `get_us_indices` + `get_tsm_adr`
   - Move `get_financial_news` from finance_mcp_server
   - Add `get_taiex_actuals` (currently `twse_fetcher.py`)

2. Create `mcp_servers/notification_server.py`:
   - Move `send_brief_to_user` (renamed `push_investment_brief`)
   - Add `push_notification(channel, message)` for raw push
   - Add call logging to `notification_logs` table
   - Load only `LINE_*` and `TELEGRAM_*` env vars (no DB credentials)

3. Deprecate `mcp_servers/finance_mcp_server.py`

### Phase 3: Workflow Integration
**Estimated effort**: 4 hours

Modify `investment_workflow.py` to use MCP for its side effects instead of direct imports:

```python
# Current (direct import):
from messenger_tools import send_brief
results = send_brief(report)

# After migration (MCP client):
async with stdio_client(notification_server_params) as (read, write):
    async with ClientSession(read, write) as session:
        result = await session.call_tool("push_investment_brief", {"brief_text": report})
```

**Note**: This requires the workflow to become async (`async def main()` + `await graph.ainvoke()`). Coordinate with T3-E async migration.

### Phase 4: LLM Function Calling (Optional, Long-term)
**Estimated effort**: 8+ hours

Currently all LLM nodes use prompt engineering for structured output. Migrating to formal function calling (bind_tools) would add:
- Schema validation on LLM outputs
- Retry on malformed tool call arguments
- Observable tool_use blocks in Claude responses

This is highest-effort for lowest immediate risk. Defer until Phase 1–3 are complete.

---

## Tools Suitable for Async Execution

| Tool | Current | Async Benefit | Priority |
|------|---------|--------------|----------|
| `get_tw_future_chips` | sync (in MCP, but `_retry` uses `time.sleep`) | High — removes blocking sleep from event loop | P1 |
| `get_us_market_summary` | sync (in MCP) | Medium — parallel symbol fetching | P1 |
| `get_financial_news` | sync (in MCP) | Low — single call, fast | P2 |
| `send_line()` | sync (direct) | High — remove blocking HTTP from workflow thread | P1 |
| `send_telegram()` | sync (direct) | High — same | P1 |
| `calculate_pnl()` | sync (direct) | High — one yfinance call per holding, run concurrently | P1 |
| `get_taiex_actuals()` | sync (direct) | Medium — single TWSE call | P2 |
| `database_tools.*` | sync (direct) | Low — SQLAlchemy async possible but complex | P3 |

### Async Migration Pattern for MCP Tools

```python
# Current _retry() in finance_mcp_server.py uses blocking time.sleep():
def _retry(fn, retries=1, delay=2.0):
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception:
            time.sleep(delay)   # ← blocks entire MCP server process

# Replace with async version:
async def _aretry(coro_fn, retries=1, delay=2.0):
    for attempt in range(retries + 1):
        try:
            return await coro_fn()
        except Exception:
            if attempt < retries:
                await asyncio.sleep(delay)   # ← non-blocking
    raise
```

### Async `calculate_pnl()` Pattern

```python
async def calculate_pnl_async(holdings: list[dict]) -> list[dict]:
    """Fetch live prices for all holdings concurrently."""
    async def _fetch_price(stock_id: str) -> float:
        loop = asyncio.get_event_loop()
        # yfinance is sync; run in thread pool to avoid blocking event loop
        df = await loop.run_in_executor(
            None,
            lambda: yf.Ticker(f"{stock_id}.TW").history(period="1d")
        )
        return float(df["Close"].iloc[-1]) if not df.empty else None

    prices = await asyncio.gather(*[_fetch_price(h["stock_id"]) for h in holdings])
    # Enrich as before, using prices[i] for holdings[i]
```

---

## Migration Risk Assessment

| Migration Step | Risk | Mitigation |
|---------------|------|-----------|
| Splitting finance_mcp_server | 🟠 MEDIUM — test_collection.py breaks if server name changes | Update TOOLS list in test_collection.py; run concurrent test |
| Adding explicit env whitelist | 🟢 LOW — subprocess may fail if required var missing | Test each MCP server in isolation after change |
| Workflow → MCP for notifications | 🟡 HIGH — adds async complexity to synchronous workflow | Coordinate with T3-E; test on non-market days |
| Removing orphaned tools | 🟢 LOW — no consumers | Verify with grep before deletion |
| Adding news sanitization | 🟢 LOW — may filter legitimate titles | Log filtered titles for review; tune keywords |

---

## Summary Recommendation Table

| Tool | Action | Priority | Effort |
|------|--------|----------|--------|
| `save_brief_to_db` (orphaned) | Remove from finance_mcp_server OR add auth guard | P0 | 15 min |
| `send_brief_to_user` (orphaned) | Remove from finance_mcp_server OR add auth guard | P0 | 15 min |
| Env whitelist for subprocesses | Add explicit env dict to StdioServerParameters | P0 | 20 min |
| `get_financial_news` title sanitization | Add keyword filter | P0 | 20 min |
| `twse_fetcher` verify=False | Replace with custom CA bundle | P1 | 30 min |
| `get_us_market_summary` split | Separate US indices from TSM ADR | P2 | 1 hr |
| `finance_mcp_server` split into 3 | market_data / persistence / notification | P2 | 3 hr |
| `get_taiex_actuals` → MCP | Move to market_data_server | P2 | 1 hr |
| `calculate_pnl` → async | Run per-holding in thread pool | P2 | 1 hr |
| `push_notification` / `push_investment_brief` | New notification_server with call log | P3 | 2 hr |
| Workflow → MCP for notifications | Replace direct `send_brief()` import | P3 | 3 hr |
| LLM function calling (`bind_tools`) | Replace prompt-JSON with formal tool schemas | P4 | 8+ hr |
