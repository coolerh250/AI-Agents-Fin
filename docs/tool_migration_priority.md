# Tool Migration Priority
**AI Agent Studio — Taiwan Stock Futures Analysis Team**  
_Plan date: 2026-05-16 | Status: DESIGN — pending implementation_

---

## Reading This Document

- **Current state**: where the tool lives today
- **Target state**: where it should live after Phase 1 (or later phase)
- **Priority**: P0 = security/blocking · P1 = Phase 1 · P2 = Phase 2 · KEEP = stays direct forever
- **Effort**: estimated implementation time for this specific tool's migration
- **Risk if deferred**: what happens if this item is not done in Phase 1

---

## Priority Matrix

### P0 — Security (must be done before any new MCP work)

| Tool | Current State | Target State | Effort | Risk If Deferred |
|------|--------------|--------------|--------|------------------|
| `save_brief_to_db` | Orphaned in `finance_mcp_server`; any MCP client can call → INSERT to `daily_briefs` | **Remove** from `finance_mcp_server`; re-implement as `save_brief` in `persistence_server` with auth | 15 min (remove) + 30 min (new) | MCP client can corrupt `daily_briefs` silently |
| `send_brief_to_user` | Orphaned in `finance_mcp_server`; any MCP client can call → push arbitrary text to user's LINE | **Remove** from `finance_mcp_server`; re-implement as `push_investment_brief` in `notification_server` with auth | 15 min (remove) + 45 min (new) | MCP client can spam user's LINE/Telegram |
| `finance://backtest/report` (resource) | Orphaned MCP resource in `finance_mcp_server`; no consumers | **Remove** from `finance_mcp_server` | 5 min | Minor: read-only but dead code |
| `StdioServerParameters(env=None)` | All subprocess spawns inherit full parent env including LINE/Telegram tokens | Replace with per-server env dict via `utils/mcp_env.py` | 20 min | Credential exfiltration via compromised MCP subprocess dependency |
| `get_financial_news` title output | Raw Anue headlines passed unsanitized to LLM prompts | Add `_sanitize_title()` in `finance_mcp_server.py` now; carry to `market_data_server` | 20 min | Prompt injection: adversarial headline → LLM output → user's LINE |

---

### P1 — Phase 1 Server Split

| Tool | Current State | Target State | Effort | Notes |
|------|--------------|--------------|--------|-------|
| `get_tw_future_chips` | `finance_mcp_server.py` | `market_data_server.py` (same logic, add `_aretry` async) | 30 min | Consumer: `test_collection.py` |
| `get_us_market_summary` | `finance_mcp_server.py` | Split into `get_us_indices` + `get_tsm_adr` in `market_data_server.py` | 45 min | Enables downstream disaggregation of TSM ADR signal |
| `get_financial_news` | `finance_mcp_server.py` | `market_data_server.py` (with sanitization from P0) | 20 min | P0 sanitization must be done first |
| `save_brief_to_db` | Removed in P0 | New: `persistence_server.save_brief` with `MCP_WRITE_TOKEN` | 30 min | Consumer: `investment_workflow.save_to_db_node` (Phase 1C) |
| `send_brief_to_user` | Removed in P0 | New: `notification_server.push_investment_brief` with `MCP_NOTIFY_TOKEN` + dedup | 45 min | Consumer: `investment_workflow.send_notification_node` (Phase 1C) |
| `get_system_stats` | `system_inspector.py` | `system_server.py` (rename only) | 10 min | Update `agent_orchestrator.py` reference |
| `tool_audit_log` DDL | Not implemented | `migration.sql` Step 12 + `utils/mcp_audit.py` | 45 min | Cross-cutting; needed before server wiring |
| `push_raw` | Not implemented | New tool in `notification_server` for manual testing | 20 min | Prevents notification_server from having zero test surface |
| `save_actual` (stub) | Direct call in `backtest_agent.py` | Declare as stub in `persistence_server` (Phase 2 wiring) | 15 min | Stub now so Phase 2 doesn't require server restart |
| `get_brief` (MCP) | Direct call in `backtest_agent.py` | Declare in `persistence_server` (Phase 2 wiring) | 15 min | Same; Phase 1 declares, Phase 2 wires |

---

### P1 — Workflow Integration (Phase 1C)

| Node | Current | Target | Effort | Dependency |
|------|---------|--------|--------|------------|
| `send_notification_node` in `investment_workflow.py` | `from messenger_tools import send_brief; send_brief(report)` | `call_mcp_tool_sync("notification_server.py", "push_investment_brief", {...})` | 45 min | notification_server must exist (1B) |
| `save_to_db_node` in `investment_workflow.py` | `from database_tools import save_brief; save_brief(...)` | `call_mcp_tool_sync("persistence_server.py", "save_brief", {...})` | 30 min | persistence_server must exist (1B) |
| `utils/mcp_call.py` | Does not exist | New sync wrapper; used by both migrated nodes | 30 min | Precondition for both node migrations |
| `test_collection.py` server reference | Points to `finance_mcp_server.py` | Update to `market_data_server.py` | 15 min | market_data_server must exist (1B) |
| `agent_orchestrator.py` server reference | Points to `system_inspector.py` | Update to `system_server.py` | 5 min | system_server must exist (1B) |

---

### P2 — Phase 2 Tool Promotions

| Tool | Current State | Target State | Effort | Reason for P2 |
|------|--------------|--------------|--------|---------------|
| `twse_fetcher.get_taiex_actuals()` | Direct call in `backtest_agent.py`; `verify=False` TLS risk | `market_data_server.get_taiex_actuals` with custom CA bundle | 1 h | Only called by `backtest_agent`, not main workflow; lower urgency |
| `portfolio_tools.calculate_pnl()` | Direct call in `portfolio_manager_node`; no P&L stale flag | `market_data_server.get_portfolio_pnl` with `price_stale` flag | 2 h | Requires async per-holding fetch (`asyncio.gather`) — needs T3-E async migration |
| `backtest_agent` → `persistence_server` | `save_actual()` direct call | `call_mcp_tool_sync("persistence_server.py", "save_actual", {...})` | 30 min | Lower priority than main workflow migration |
| `asyncio.run()` wrappers → `await session.call_tool()` | Phase 1 uses `asyncio.run()` per call | Native async after T3-E migration | 3 h | T3-E must be done first |
| `notification_log` table | Phase 1 uses `tool_audit_log` for dedup | Dedicated `notification_log` with per-message metadata | 1 h | Nice-to-have; `tool_audit_log` is sufficient for Phase 1 |

---

### KEEP — Direct Call Forever

These functions should never be wrapped in MCP. The overhead, complexity, or nature of the call
makes MCP counter-productive.

| Function | File | Reason to Keep Direct |
|----------|------|-----------------------|
| `database_tools.log_cost()` | `database_tools.py` | Called 6× per workflow run; MCP cold-start would add ~12 s |
| `database_tools.get_brief()` | `database_tools.py` | Internal read within workflow process; no external I/O |
| `database_tools.get_portfolio()` | `database_tools.py` | Internal read; used by both workflow and dashboard |
| `database_tools.save_actual()` | `database_tools.py` | Called by `backtest_agent` directly; MCP migration is Phase 2 |
| `database_tools.log_session_episode()` | `database_tools.py` | Internal telemetry; no governance need |
| `database_tools.create_eval_run()` | `database_tools.py` | Internal evaluation pipeline |
| `database_tools.save_eval_result()` | `database_tools.py` | Internal evaluation pipeline |
| `database_tools.save_strategy_lesson()` | `database_tools.py` | Flywheel internal; no external side effect |
| `database_tools.get_relevant_lessons()` | `database_tools.py` | Internal read for context injection |
| `messenger_tools.format_brief()` | `messenger_tools.py` | Pure function; no I/O; never needs governance |
| `lesson_writer.write_lesson()` | `lesson_writer.py` | Internal flywheel helper |
| `lesson_retriever.get_lesson_context()` | `lesson_retriever.py` | Pure read + string transform |
| `_llm()` / `_llm_opus()` factories | `market_analyst_agents.py` | LLM calls are the reasoning layer, not tool calls |
| All `evaluation_*` functions | `evaluation_*.py` | Internal quality pipeline; no external side effects |
| `twse_fetcher.get_taiex_actuals()` | `twse_fetcher.py` | Keep direct for now; Phase 2 MCP promotion |

---

## Risk-Priority Heatmap

```
         Impact
          HIGH    │  [P0] save_brief_to_db orphan    [P0] env inheritance
                  │  [P0] send_brief_to_user orphan   [P0] news injection
                  │  [P1] send_notification → MCP     [P2] calculate_pnl
                  │
         MEDIUM   │  [P1] save_to_db → MCP            [P2] taiex_actuals
                  │  [P1] server split                 [P2] async migration
                  │
          LOW     │  [P1] audit log                   [KEEP] DB reads
                  │  [P1] get_system_stats rename      [KEEP] format_brief
                  │
                  └──────────────────────────────────────────────────────
                         HIGH              LOW
                         Probability of exploitation
```

---

## Effort Summary by Priority

| Priority | Total Estimated Effort |
|----------|----------------------|
| P0 — Security (5 tasks) | ~75 min |
| P1 — Server split (10 tasks) | ~280 min (~4.5 h) |
| P1 — Workflow integration (5 tasks) | ~125 min (~2 h) |
| P1 — Total | ~6.5 h |
| P2 | ~7.5 h |
| **Phase 1 Grand Total** | **~8 h** _(P0 + P1)_ |

---

## Migration Decision Log

Decisions made in this document that differ from `mcp_migration_plan.md`:

| Decision | mcp_migration_plan.md said | This document says | Reason |
|----------|---------------------------|--------------------|--------|
| `calculate_pnl` timing | P2 "Week 1 alongside hardening" | Phase 2 proper | Requires async; unsafe to rush |
| `save_actual` (backtest) | "Week 2 persistence layer" | P2, not P1 | Focus Phase 1 on main workflow only |
| `notification_log` table | Create in Phase 1 | Phase 2; use `tool_audit_log` for dedup | Reduces Phase 1 scope; tool_audit_log is sufficient |
| `get_us_market_summary` split | P2 "Category D1" | P1 during server split | Natural moment; negligible extra cost during move |
| `get_recent_accuracy` as MCP tool | Not specified | Declare in persistence_server | Low-cost stub; enables Phase 2 wiring without restart |
