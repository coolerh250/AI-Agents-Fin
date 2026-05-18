# MCP Governance Phase 1 — Implementation Plan
**AI Agent Studio — Taiwan Stock Futures Analysis Team**  
_Plan date: 2026-05-16 | Status: DESIGN — pending implementation_

---

## Problem Statement

The production workflow (`investment_workflow.py`) bypasses the existing MCP layer entirely.
LINE/Telegram pushes, TiDB writes, and portfolio price fetching are direct Python imports
with no governance boundary, no audit trail, and no permission control.

Two orphaned MCP tools (`save_brief_to_db`, `send_brief_to_user`) carry production-database
and notification credentials inside `finance_mcp_server.py` — accessible to any connected
MCP client with no auth check. The single `finance_mcp_server` mixes read-only market data
tools with write-permission tools, violating least privilege.

**Reference audits**: `tool_inventory.md §5`, `tool_risk_matrix.md §Risk 1–8`,
`recommended_mcp_architecture.md`, `mcp_migration_plan.md`.

---

## Phase 1 Goals (from requirements)

1. Split DB write, notification, and market data tools into separate MCP servers
2. Establish tool permission enforcement (auth tokens on write/notify tools)
3. Build `tool_audit_log` infrastructure
4. Remove or mark orphaned MCP tools
5. Migrate production workflow high-risk direct calls to MCP tool calls
6. Preserve low-risk local helper direct calls

---

## Scope Decision: Phase 1 vs. Phase 2

### In Scope — Phase 1

| Sub-phase | Description | Effort |
|-----------|-------------|--------|
| **1A — Security hardening** | Remove orphaned tools; env isolation; news sanitization | ~2 h |
| **1B — Server split** | Create `market_data_server`, `persistence_server`, `notification_server`; deprecate `finance_mcp_server` | ~4 h |
| **1C — Workflow integration** | Migrate `send_notification_node` + `save_to_db_node` to MCP via sync wrapper | ~3 h |
| **1D — Audit infrastructure** | `tool_audit_log` DDL + `@audit_tool` decorator wired into all three new servers | ~2 h |

**Total Phase 1 estimate: ~11 hours**

### Out of Scope — Deferred to Phase 2+

| Item | Reason |
|------|--------|
| Async workflow migration (T3-E) | Requires `graph.invoke → graph.ainvoke`; separate epic |
| `calculate_pnl()` → MCP | Per-holding concurrent fetching needs async; too risky without T3-E |
| `get_taiex_actuals()` → MCP | Only called by `backtest_agent.py`, not main production path; Phase 2 |
| TWSE `verify=False` fix | Custom CA bundle work is independent; parallelize outside Phase 1 |
| LLM function calling (`bind_tools`) | 8+ hours; `mcp_migration_plan.md §Phase 4` |
| Streamlit auth (T1-C) | Separate security initiative; not MCP-related |
| `notification_log` table | Phase 1 uses `tool_audit_log` as the audit mechanism; dedicated notification log is Phase 2 |

---

## Architectural Principles (from `recommended_mcp_architecture.md`)

1. **Least privilege per server**: each server receives only the credentials it needs.
   A server that reads market data gets no DB password; a notification server gets no TiDB credentials.
2. **Every side effect is a named tool**: DB writes and message delivery are `@mcp.tool()` calls — observable, auditable, replaceable.
3. **Write tools require internal tokens**: any tool that modifies state requires an `_api_key`
   parameter checked against an env variable. LLM-accessible tools never have write access by default.
4. **No orphaned tools**: every tool has at least one named consumer.
5. **Workflow stays synchronous until T3-E**: MCP calls use `asyncio.run()` sync wrappers during Phase 1.

---

## Sub-phase 1A: Security Hardening

**Files changed**: `mcp_servers/finance_mcp_server.py`, all `StdioServerParameters` call sites

### Task 1A-1: Remove orphaned tools

`save_brief_to_db` and `send_brief_to_user` are declared in `finance_mcp_server.py` with
zero consumers. They carry full DB write access and LINE/Telegram push capability reachable
by any MCP client (`tool_inventory.md §Tool 5–6`).

**Action**: Delete both `@mcp.tool()` blocks from `finance_mcp_server.py`.
Do NOT add auth guards here — the tools will be re-implemented properly in the new servers
(sub-phase 1B). Deleting is cleaner than guarding a dead tool.

Also remove the `finance://backtest/report` MCP resource — it has no consumers either.

### Task 1A-2: Explicit env whitelist for MCP subprocesses

All `StdioServerParameters` calls pass `env=None`, inheriting the full parent environment
including LINE tokens, Telegram tokens, and `ANTHROPIC_API_KEY` in every subprocess
(`tool_risk_matrix.md §Risk 1`).

**Action**: Create `utils/mcp_env.py` with one builder function per server:

```python
def market_data_env() -> dict:
    # Public APIs only; TIDB_* needed only for get_portfolio_pnl (Phase 2)
    return {k: os.environ[k] for k in ["PATH", "HOME", "VIRTUAL_ENV", "UV_CACHE_DIR"]
            if k in os.environ}

def notification_env() -> dict:
    # LINE + Telegram only; no TIDB, no ANTHROPIC
    required = ["LINE_CHANNEL_ACCESS_TOKEN", "LINE_USER_ID",
                "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "MCP_NOTIFY_TOKEN"]
    return {**_base_env(), **{k: os.environ.get(k, "") for k in required}}

def persistence_env() -> dict:
    # TIDB + write token; no LINE/Telegram
    required = ["TIDB_HOST", "TIDB_PORT", "TIDB_USER", "TIDB_PASSWORD",
                "TIDB_DB", "MCP_WRITE_TOKEN"]
    return {**_base_env(), **{k: os.environ.get(k, "") for k in required}}

def system_env() -> dict:
    return _base_env()   # no credentials
```

All existing `StdioServerParameters(env=None)` calls must be updated to use the appropriate builder.

### Task 1A-3: News title sanitization

Raw Anue headlines flow unsanitized into LLM prompts (`tool_risk_matrix.md §Risk 3`).
Attack path: Anue API → `market_snapshot.json` → `data_collector_node` → `chief_strategist` → LINE push.

**Action**: Add `_sanitize_title(title: str) -> str` to `finance_mcp_server.py` now,
and carry it forward into `market_data_server.py`:

```python
_INJECTION_KEYWORDS = [
    "ignore", "forget", "override", "system:", "human:", "assistant:", "<<", "]]"
]

def _sanitize_title(title: str) -> str:
    lower = title.lower()
    if any(kw in lower for kw in _INJECTION_KEYWORDS):
        return "[filtered]"
    return title[:200]
```

Apply inside `get_financial_news` before appending each headline to the result list.

---

## Sub-phase 1B: Server Split

**Files created**:
- `mcp_servers/market_data_server.py`
- `mcp_servers/persistence_server.py`
- `mcp_servers/notification_server.py`

**Files deprecated**: `mcp_servers/finance_mcp_server.py` (keep with deprecation header for one sprint)

**File renamed**: `mcp_servers/system_inspector.py` → `mcp_servers/system_server.py`

For the complete per-server tool specifications, credential lists, and placement rationale,
see **`mcp_server_boundary_design.md`**.

**Migration summary**:

```
finance_mcp_server.py (deprecated after 1 sprint)
  ├── get_tw_future_chips    → market_data_server.py (unchanged)
  ├── get_us_market_summary  → market_data_server.py → split into:
  │                              get_us_indices (DJIA, NDX, SOX)
  │                              get_tsm_adr (TSM ADR only)
  ├── get_financial_news     → market_data_server.py + _sanitize_title
  ├── save_brief_to_db       → persistence_server.py as save_brief (+ auth guard)
  └── send_brief_to_user     → notification_server.py as push_investment_brief (+ auth + dedup)

system_inspector.py
  └── get_system_stats       → system_server.py (rename only, no content change)
```

**Consumer update required**: `test_collection.py` must update its `StdioServerParameters`
to reference `mcp_servers/market_data_server.py` before `finance_mcp_server.py` is deleted.

---

## Sub-phase 1C: Workflow Integration

**Files changed**: `investment_workflow.py`  
**New utility**: `utils/mcp_call.py` (sync wrapper)

Two nodes in `investment_workflow.py` call high-risk direct functions that must move to MCP:

| Node | Current Direct Call | Risk | → MCP Target |
|------|--------------------|----- |--------------|
| `send_notification_node` | `messenger_tools.send_brief()` | 🔴 No audit, no dedup, sends to real users | `notification_server.push_investment_brief` |
| `save_to_db_node` | `database_tools.save_brief()` | 🟡 TiDB write, no call log | `persistence_server.save_brief` |

**Direct calls to preserve (not MCP-ified)**:

| Function | Reason to Keep Direct |
|----------|-----------------------|
| `database_tools.log_cost()` | Called 6× per run; MCP cold-start overhead would add ~12 s |
| All `database_tools.get_*()` | Read-only, no external side effects, no governance need |
| `portfolio_tools.calculate_pnl()` | Requires async per-holding concurrency; deferred to Phase 2 |
| `lesson_writer.write_lesson()` | Internal flywheel helper, no direct external side effects |
| `lesson_retriever.get_lesson_context()` | Pure read + string transform |
| `messenger_tools.format_brief()` | Pure function, no I/O |

**Sync wrapper pattern** (from `recommended_mcp_architecture.md`):

```python
# utils/mcp_call.py
def call_mcp_tool_sync(server_script, tool_name, arguments, timeout=30.0) -> dict:
    """Blocking MCP tool call safe for use in synchronous LangGraph nodes."""
    async def _inner():
        params = StdioServerParameters(
            command="uv", args=["run", server_script],
            env=_env_for_server(server_script),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                return json.loads(result.content[0].text)
    return asyncio.run(_inner())
```

Each MCP call adds ~2 s subprocess cold start. For Phase 1 (2 calls at workflow end),
total overhead is ~4 s on a 40–50 s workflow — acceptable until T3-E async migration.

For the exact node-level migration diffs, see **`backward_compatibility_plan.md §Workflow Nodes`**.

---

## Sub-phase 1D: Audit Infrastructure

**Files changed**: `migration.sql`, each new MCP server  
**New utility**: `utils/mcp_audit.py`

### migration.sql Step 12 — tool_audit_log

```sql
CREATE TABLE IF NOT EXISTS tool_audit_log (
    id          BIGINT        AUTO_INCREMENT PRIMARY KEY,
    called_at   TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
    server      VARCHAR(50)   NOT NULL,
    tool_name   VARCHAR(100)  NOT NULL,
    caller      VARCHAR(50)   NULL,
    status      VARCHAR(20)   NOT NULL,  -- ok | error | unauthorized | skipped
    latency_ms  INT           NULL,
    error_msg   TEXT          NULL,
    INDEX idx_tal_called_at (called_at),
    INDEX idx_tal_tool      (tool_name),
    INDEX idx_tal_status    (status)
);
```

### `utils/mcp_audit.py` — audit_tool context manager

```python
import contextlib, time, os
from typing import Optional

def audit_tool(server: str, tool_name: str, caller: str = ""):
    @contextlib.contextmanager
    def _ctx():
        t0 = time.monotonic()
        status, err = "ok", None
        try:
            yield
        except Exception as exc:
            status = "error"
            err = str(exc)[:500]
            raise
        finally:
            ms = int((time.monotonic() - t0) * 1000)
            try:
                _write_audit(server, tool_name, caller, status, ms, err)
            except Exception:
                pass   # audit failure must never block tool execution
    return _ctx()
```

`_write_audit()` uses a **dedicated minimal connection** (not the shared `_engine()` pool)
to avoid connection exhaustion (`tool_risk_matrix.md §Risk 8`).

### Wiring into servers

Each `@mcp.tool()` function wraps its body in `with audit_tool(server, tool_name):`.
Write tools additionally log status `"unauthorized"` when the token check fails before
returning the error dict (the `with` block is not entered for unauthorized calls,
so `status` defaults to what we set before returning).

---

## New Environment Variables Required

| Variable | Server | Purpose |
|----------|--------|---------|
| `MCP_WRITE_TOKEN` | `persistence_server` | Auth for all write tools |
| `MCP_NOTIFY_TOKEN` | `notification_server` | Auth for push tools |

Add to `.env` on both local dev and server:
```
MCP_WRITE_TOKEN=<generate: python -c "import secrets; print(secrets.token_hex(32))">
MCP_NOTIFY_TOKEN=<generate: python -c "import secrets; print(secrets.token_hex(32))">
```

---

## Implementation Order (within Phase 1)

Dependencies run top to bottom:

```
1A-1: Remove orphaned tools from finance_mcp_server   (no dependencies)
1A-2: Create utils/mcp_env.py                         (no dependencies)
1A-3: Add _sanitize_title to finance_mcp_server       (no dependencies)
  │
  ├── 1D:  migration.sql Step 12 + utils/mcp_audit.py (no dependencies)
  │
  ├── 1B-market:  Create market_data_server.py         (needs 1A-3 sanitization)
  ├── 1B-persist: Create persistence_server.py         (needs 1D audit)
  ├── 1B-notify:  Create notification_server.py        (needs 1D audit)
  │
  ├── Update test_collection.py → market_data_server   (needs 1B-market)
  │
  └── 1C: Create utils/mcp_call.py                     (needs 1A-2 env)
          Update investment_workflow.py                 (needs 1B-persist + 1B-notify + utils/mcp_call)
```

---

## Success Criteria

| Criterion | Verification method |
|-----------|---------------------|
| Zero orphaned tools in any MCP server | `grep "@mcp.tool"` in each server; confirm each has ≥1 named consumer |
| No MCP subprocess inherits LINE/Telegram tokens | Add env key dump to each server `__main__`; verify absence |
| News title sanitization active | Unit test `_sanitize_title` with injection patterns |
| `test_collection.py` works against `market_data_server` | Run it; compare JSON structure |
| `send_notification_node` calls MCP tool | Code inspection + integration run |
| `save_to_db_node` calls MCP tool | Code inspection + DB verification |
| Every Phase 1 tool invocation appears in `tool_audit_log` | Run `backtest_agent.py`; `SELECT * FROM tool_audit_log` |
| Full daily cron ≤50 s (MCP overhead ≤5 s) | Time `investment_workflow.py` end-to-end |
| No credential leakage between servers | Grep each server for forbidden env keys |

---

## Phase 2 Preview

| Item | Effort |
|------|--------|
| `get_taiex_actuals` → `market_data_server` | ~1 h |
| `calculate_pnl` → `market_data_server` (async per-holding) | ~2 h |
| `backtest_agent.py` → `persistence_server` for `save_actual` + `save_accuracy_report` | ~1 h |
| T3-E async migration: `asyncio.run()` wrappers → `await session.call_tool()` | ~3 h |
| TWSE custom CA bundle (replace `verify=False`) | ~1 h |
| Dedicated `notification_log` table (beyond `tool_audit_log`) | ~1 h |
