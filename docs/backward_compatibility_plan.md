# Backward Compatibility Plan
**AI Agent Studio — Taiwan Stock Futures Analysis Team**  
_Plan date: 2026-05-16 | Status: DESIGN — pending implementation_

---

## Objective

Migrate from direct-import side effects to MCP tool calls without breaking any of the five
existing callers. No callers should need to be aware of Phase 1 changes at the same time
(changes are sequenced). Rollback for any step must be a one-line revert.

---

## Caller Inventory

| Caller | File | Affected by Phase 1 | Change Required |
|--------|------|--------------------|-----------------|
| `test_collection.py` | `test_collection.py` | Yes — server name changes | Update `StdioServerParameters` path |
| `investment_workflow.py` | `investment_workflow.py` | Yes — 2 nodes migrated | Replace 2 direct calls with `call_mcp_tool_sync` |
| `backtest_agent.py` | `backtest_agent.py` | No (Phase 2) | No change in Phase 1 |
| `agent_orchestrator.py` | `agent_orchestrator.py` | Yes — server renamed | Update `StdioServerParameters` path |
| `dashboard.py` | `dashboard.py` | No | No change |

---

## Change 1: `test_collection.py` — Server Path Update

### Current

```python
server_params = StdioServerParameters(
    command="uv",
    args=["run", "mcp_servers/finance_mcp_server.py"],
    env=None,
)
```

### After Phase 1

```python
from utils.mcp_env import market_data_env

server_params = StdioServerParameters(
    command="uv",
    args=["run", "mcp_servers/market_data_server.py"],
    env=market_data_env(),
)
```

### Tool name changes

`get_us_market_summary` is split into `get_us_indices` + `get_tsm_adr`.
`test_collection.py` currently calls `get_us_market_summary` as one tool.

**Migration options**:

**Option A (recommended)**: Update `test_collection.py` to call both new tools and merge results.
This is the right long-term fix and the snapshot format already separates the fields.

```python
# Before:
result = await session.call_tool("get_us_market_summary", {})
# After:
indices = await session.call_tool("get_us_indices", {})
tsm_adr = await session.call_tool("get_tsm_adr", {})
# Merge into snapshot as before; field names unchanged in snapshot.json
```

**Option B (shim, if Option A creates risk)**: Add a `get_us_market_summary` shim in
`market_data_server.py` that calls `get_us_indices` + `get_tsm_adr` internally and returns
the old combined format. Remove the shim in Phase 2.

```python
@mcp.tool()
async def get_us_market_summary() -> dict:
    """Compatibility shim for test_collection.py. Deprecated — use get_us_indices + get_tsm_adr."""
    indices = await _fetch_us_indices()
    tsm = await _fetch_tsm_adr()
    return {**indices, **tsm}
```

Recommendation: **Option A** — `test_collection.py` is a short script, the change is 3 lines,
and the shim adds dead code. However, if `market_snapshot.json` consumers depend on the
combined key structure, verify field names before choosing.

### Rollback

Revert the `args` path back to `finance_mcp_server.py` — `finance_mcp_server.py` is kept
(with deprecation header) for one full sprint before deletion.

---

## Change 2: `investment_workflow.py` — `save_to_db_node`

### Current (direct call)

```python
def save_to_db_node(state: WorkflowState) -> dict:
    from database_tools import save_brief
    row_id = save_brief(
        trade_date=state["trade_date"],
        brief_text=state["final_brief"],
        predicted_gap_pct=state.get("predicted_gap_pct"),
        gap_direction=state.get("gap_direction"),
    )
    return {"db_row_id": row_id}
```

### After Phase 1 (MCP call)

```python
def save_to_db_node(state: WorkflowState) -> dict:
    from utils.mcp_call import call_mcp_tool_sync
    result = call_mcp_tool_sync(
        server_script="mcp_servers/persistence_server.py",
        tool_name="save_brief",
        arguments={
            "trade_date":         str(state["trade_date"]),
            "brief_text":         state.get("final_brief", ""),
            "predicted_gap_pct":  state.get("predicted_gap_pct"),
            "gap_direction":      state.get("gap_direction"),
            "_api_key":           os.getenv("MCP_WRITE_TOKEN", ""),
        },
    )
    if not result.get("success"):
        logger.warning(f"[SaveToDB] persistence_server returned error: {result.get('error')}")
    return {"db_row_id": result.get("row_id", 0)}
```

### Failure mode differences

| Scenario | Current (direct) | After (MCP) |
|----------|-----------------|-------------|
| TiDB down | `OperationalError` propagates up; node raises | `call_mcp_tool_sync` catches subprocess error; returns `{"success": false, "error": "..."}` |
| `MCP_WRITE_TOKEN` missing | N/A | Returns `{"success": false, "error": "unauthorized"}` — logged as warning, workflow continues |
| Subprocess cold start fails | N/A | `RuntimeError` from `asyncio.run()` — will surface as node error; needs try/except |

**Add outer try/except to `save_to_db_node`**:

```python
def save_to_db_node(state: WorkflowState) -> dict:
    try:
        ...  # MCP call as above
    except Exception as exc:
        logger.warning(f"[SaveToDB] MCP call failed, falling back to direct: {exc}")
        from database_tools import save_brief as _save_brief
        row_id = _save_brief(
            trade_date=state["trade_date"],
            brief_text=state.get("final_brief", ""),
            predicted_gap_pct=state.get("predicted_gap_pct"),
            gap_direction=state.get("gap_direction"),
        )
        return {"db_row_id": row_id}
```

The fallback to direct call preserves the pre-Phase-1 behaviour exactly.
This pattern means Phase 1 is **additive only** — the rollback is "remove the try/except wrapper
and restore the direct call body".

### Rollback

Replace `save_to_db_node` body with the "Current (direct call)" version above.

---

## Change 3: `investment_workflow.py` — `send_notification_node`

### Current (direct call)

```python
def send_notification_node(state: WorkflowState) -> dict:
    from messenger_tools import send_brief
    result = send_brief(state.get("final_brief", ""))
    return {"notification_result": result}
```

### After Phase 1 (MCP call)

```python
def send_notification_node(state: WorkflowState) -> dict:
    try:
        from utils.mcp_call import call_mcp_tool_sync
        result = call_mcp_tool_sync(
            server_script="mcp_servers/notification_server.py",
            tool_name="push_investment_brief",
            arguments={
                "brief_text": state.get("final_brief", ""),
                "_api_key":   os.getenv("MCP_NOTIFY_TOKEN", ""),
            },
        )
        if result.get("dedup_skipped"):
            logger.info("[Notification] Dedup skipped — already sent today")
        return {"notification_result": result}
    except Exception as exc:
        logger.warning(f"[Notification] MCP call failed, falling back to direct: {exc}")
        from messenger_tools import send_brief as _send_brief
        result = _send_brief(state.get("final_brief", ""))
        return {"notification_result": result}
```

### New behaviours introduced by MCP migration

| Behaviour | Before | After |
|-----------|--------|-------|
| Duplicate push prevention | None (sends every run) | Dedup by trade date via `tool_audit_log` |
| Message length truncation | None (LINE API may reject > 5000 chars) | Hard truncated to 4000 chars in `notification_server` |
| Auth requirement | None | `MCP_NOTIFY_TOKEN` must be set; missing token → fallback |
| Audit trail | stdout log only | Persisted to `tool_audit_log` (server name, status, latency) |

### Rollback

Replace `send_notification_node` body with the "Current (direct call)" version above.
The `notification_server.py` can remain deployed — no consumers = no effect.

---

## Change 4: `agent_orchestrator.py` — Server Rename

### Current

```python
server_params = StdioServerParameters(
    command="uv",
    args=["run", "mcp_servers/system_inspector.py"],
    env=None,
)
```

### After Phase 1

```python
from utils.mcp_env import system_env

server_params = StdioServerParameters(
    command="uv",
    args=["run", "mcp_servers/system_server.py"],
    env=system_env(),
)
```

Tool name `get_system_stats` is unchanged. This is a path-only change.

### Rollback

Revert to `system_inspector.py` path. Keep `system_inspector.py` for one sprint.

---

## New Utility: `utils/mcp_call.py`

This file does not exist today. It is the sync wrapper that lets synchronous LangGraph nodes
call MCP tools without T3-E async migration.

```python
"""
utils/mcp_call.py
Synchronous wrapper for MCP tool calls. Safe for use in synchronous LangGraph nodes.
One asyncio.run() per call — ~2 s cold start per subprocess. Acceptable for Phase 1.
Replace with native await in T3-E async migration.
"""
import asyncio, json, os
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from utils.mcp_env import _env_for_server


def call_mcp_tool_sync(
    server_script: str,
    tool_name: str,
    arguments: dict,
    timeout: float = 30.0,
) -> dict:
    """Synchronously call a single MCP tool from a stdio server.
    Raises RuntimeError if already inside a running event loop (use ainvoke instead).
    """
    async def _inner():
        params = StdioServerParameters(
            command="uv",
            args=["run", server_script],
            env=_env_for_server(server_script),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                return json.loads(result.content[0].text)

    return asyncio.run(_inner())
```

`_env_for_server()` in `utils/mcp_env.py` maps a server script path to its env builder:

```python
def _env_for_server(server_script: str) -> dict:
    if "market_data" in server_script:
        return market_data_env()
    if "persistence" in server_script:
        return persistence_env()
    if "notification" in server_script:
        return notification_env()
    if "system" in server_script:
        return system_env()
    return {}   # unknown server: pass empty env (safest default)
```

---

## Parallel Operation Period

During Phase 1 implementation, both old and new paths may coexist briefly:

```
Day 1: Phase 1A complete
  finance_mcp_server.py: orphaned tools removed + sanitization added
  test_collection.py: still uses finance_mcp_server.py (unchanged — NOT yet broken)

Day 2-3: Phase 1B complete
  market_data_server.py: deployed
  persistence_server.py: deployed
  notification_server.py: deployed
  test_collection.py: updated to market_data_server.py
  finance_mcp_server.py: deprecated header added, kept for rollback

Day 4: Phase 1C complete
  investment_workflow.py: save_to_db_node + send_notification_node → MCP calls
  finance_mcp_server.py: still present (not deleted)

Day 5: Phase 1D complete
  tool_audit_log: table created, wired into all three new servers

After next sprint: finance_mcp_server.py deleted
```

During this window, any rollback to `finance_mcp_server.py` is a one-line path change.

---

## Rollback Decision Matrix

| What breaks | Rollback action | Time to recover |
|-------------|----------------|-----------------|
| `market_data_server.py` tool failure | Change `test_collection.py` `args` back to `finance_mcp_server.py` | 2 min |
| `persistence_server.py` down | `save_to_db_node` fallback path (direct call) activates automatically | 0 min |
| `notification_server.py` down | `send_notification_node` fallback path (direct call) activates automatically | 0 min |
| `MCP_WRITE_TOKEN` not set | `save_to_db_node` fallback path activates; logs warning | 0 min |
| `MCP_NOTIFY_TOKEN` not set | `send_notification_node` fallback path activates; logs warning | 0 min |
| `system_server.py` not found | Change `agent_orchestrator.py` `args` back to `system_inspector.py` | 2 min |
| `tool_audit_log` table missing | `_write_audit()` is fail-silent; `push_investment_brief` dedup falls back to "always send" | 0 min |

All fallbacks are passive (no operator action required except the two that need a path change).
The `finance_mcp_server.py` is kept for the entire Phase 1 sprint as a safety net.

---

## Environment Variable Change Summary

| Variable | Before Phase 1 | After Phase 1 | Action |
|----------|---------------|---------------|--------|
| `MCP_WRITE_TOKEN` | Not defined | Required by `persistence_server` | **Add to `.env`** |
| `MCP_NOTIFY_TOKEN` | Not defined | Required by `notification_server` | **Add to `.env`** |
| All existing vars | Set in `.env` | Unchanged | No action |

Generate tokens before deploying:
```bash
python -c "import secrets; print('MCP_WRITE_TOKEN=' + secrets.token_hex(32))"
python -c "import secrets; print('MCP_NOTIFY_TOKEN=' + secrets.token_hex(32))"
```

Add both lines to `.env` on the production server before starting Phase 1C.

---

## Testing Checklist

Run these checks at the end of each sub-phase:

**After 1A (Security)**:
- [ ] `test_collection.py` still runs successfully against `finance_mcp_server` (pre-1B)
- [ ] `get_financial_news` output shows `"[filtered]"` for a test title with "ignore" keyword
- [ ] `env` dict printed in each MCP server `__main__` shows no LINE/Telegram keys

**After 1B (Server split)**:
- [ ] `test_collection.py` runs against `market_data_server.py`; snapshot JSON structure unchanged
- [ ] `agent_orchestrator.py` runs against `system_server.py`; `get_system_stats` returns CPU/mem/disk
- [ ] `persistence_server.py` starts; `save_brief` with wrong token returns `{"success": false, "error": "unauthorized"}`
- [ ] `notification_server.py` starts; `push_investment_brief` with wrong token returns `{"error": "unauthorized"}`

**After 1C (Workflow integration)**:
- [ ] `investment_workflow.py` completes full run; brief saved to `daily_briefs` via MCP
- [ ] `investment_workflow.py` completes full run; LINE notification sent via MCP
- [ ] Re-run same day → `push_investment_brief` returns `dedup_skipped: true`
- [ ] Kill `persistence_server.py` mid-run → `save_to_db_node` falls back to direct call silently

**After 1D (Audit)**:
- [ ] `SELECT COUNT(*) FROM tool_audit_log` > 0 after a workflow run
- [ ] Unauthorized call logged with `status = 'unauthorized'`
- [ ] `tool_audit_log` has entries for `save_brief` and `push_investment_brief`
