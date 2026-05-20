# Architecture Summary — Executive Report
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Overall Assessment

The system is a **well-structured, functionally complete single-user investment analysis pipeline** that delivers real value at low cost (~$2.54 USD/month). The architecture is clean and the code is readable. However, it carries **a cluster of risks that compound each other** in a production deployment: no workflow checkpointing means a single API timeout loses all upstream computation; no dashboard authentication exposes financial data on the LAN; and Opus adaptive thinking has an unbounded cost ceiling.

---

## 1. Biggest Risks

### 🔴 RISK-1: Streamlit Dashboard Has No Authentication
**File:** `dashboard.py`
**Impact:** Portfolio holdings (stock, quantity, entry price, P&L), API costs, and prediction accuracy are visible to any host on the local network. The manual `market_actuals` write form can corrupt backtest data with no audit trail beyond a `notes` field.

**Why this is the top risk:** Financial data confidentiality + data integrity both affected. Anyone on the `10.0.1.20` subnet can add, delete, or modify portfolio holdings.

---

### 🔴 RISK-2: No LangGraph Checkpointing — Workflow Failures Lose All Computation
**File:** `investment_workflow.py` → `build_graph()`
**Impact:** If `chief_strategist` (the slowest node, 15–45 seconds) raises a transient error (Claude 529, network timeout, SQLAlchemy connection reset), the entire 8-node pipeline aborts. The $0.05–0.10 spent on preceding nodes is wasted; the daily brief is not generated; LINE/Telegram push does not occur; `market_actuals` for backtest remains empty.

**Frequency estimate:** Claude API returns 529 Overloaded errors during peak hours several times per month.

---

### 🔴 RISK-3: Opus Adaptive Thinking Has No Cost Ceiling
**File:** `market_analyst_agents.py:127`
```python
thinking={"type": "adaptive"},   # no budget_tokens cap
max_tokens=16000,                  # 8× actual usage
```
**Impact:** On a high-volatility day, the adaptive thinking chain could reach 12 000+ tokens, pushing a single run cost to $0.25–0.35. Over 20 trading days, this could yield a $5–7 monthly bill instead of the expected $2.54.

---

### 🟡 RISK-4: TWSE SSL Verification Disabled
**File:** `twse_fetcher.py:24`
**Impact:** On a compromised network, a MITM attack could inject false TAIEX closing prices, corrupting backtest data and potentially triggering false trading signals from the portfolio_manager.

---

### 🟡 RISK-5: `daily_run.sh` Local Copy Is Stale
**File:** `daily_run.sh` (local repo)
**Impact:** The local file still contains the old 3-step logic (with duplicate push in Step 3). If someone deploys from the local repo to a new server, they will get duplicate LINE messages every day. The authoritative copy is on the remote server only — not version-controlled.

---

## 2. Biggest Technical Debts

### TD-1: `main.py` Is a Placeholder
The entry point declared in `pyproject.toml` as the project's `main.py` prints "Hello from ai-agent-studio!" — it serves no functional purpose. New team members will be confused about the project's entry point.

### TD-2: `save_brief_to_db` and `send_brief_to_user` MCP Tools Are Dead Code
Two MCP tools in `finance_mcp_server.py` are registered but never called by any agent node (bypassed by direct Python imports). They add maintenance surface (tests to write, docs to keep updated) with zero runtime benefit.

### TD-3: `finance://backtest/report` MCP Resource Is Never Read
The MCP resource is implemented and functional but no client ever calls it. The dashboard renders the same data via direct DB queries.

### TD-4: `_MODEL_HAIKU` and `_PRICING` Are Protected-Name Exports
`investment_workflow.py` imports `_MODEL_HAIKU` and `_PRICING` directly from `market_analyst_agents.py`. Underscore-prefixed names signal "module-private" by convention. Pylance raises warnings. These belong in a separate `config.py` module.

### TD-5: `_engine()` Creates a New SQLAlchemy Engine Per Call
Every database function calls `_engine()` which calls `create_engine()` — a heavyweight object that opens a connection pool. In a single workflow run, this happens 6+ times (log_cost × 6 agents + get_portfolio + save_brief). Each engine is GC'd after the `with` block. There is no shared singleton engine, risking connection exhaustion under load.

### TD-6: `output_config={"effort": "high"}` Is Not a Valid `ChatAnthropic` Parameter
This parameter is silently ignored by the LangChain Anthropic client. The developer's intent (high-effort thinking) is not actually being communicated to the API. The correct parameter is `budget_tokens` within the `thinking` dict.

### TD-7: `backtest_accuracy_report` Is Never Persisted
Claude Haiku generates a detailed accuracy evaluation (with 0–100 score, failure analysis, and improvement suggestions) but this output only goes to `stdout`. There is no `accuracy_logs` table. Historical accuracy trend is not queryable; the Streamlit KPI tab derives accuracy from raw prediction vs actual data rather than Claude's nuanced evaluation.

### TD-8: No `uv.lock` Committed
Builds are not reproducible. A new server deployment may resolve different minor versions of `langchain-anthropic`, `langgraph`, or `yfinance`, any of which could introduce breaking changes.

### TD-9: Local `daily_run.sh` Diverges from Remote
The version-controlled script is stale. Infrastructure-as-code principle is violated — the authoritative configuration exists only on the production server.

---

## 3. Biggest Cost Problems

### CP-1: Opus Adaptive Thinking Without Budget Cap
As documented in RISK-3 and `cost_analysis.md`, the absence of `budget_tokens` in the thinking config means cost per run is non-deterministic. A single outlier run could cost 3–4× the average.

**Fix:** `thinking={"type": "enabled", "budget_tokens": 5000}` — caps thinking at ~$0.025 per run with predictable cost.

### CP-2: `backtest_agent` and `agent_orchestrator` Cost Not Tracked
Two workflows make LLM calls but never write to `cost_logs`. Total cost visibility is 80%, not 100%.

### CP-3: `format_agent` Receives Redundant Context
`format_agent` receives the full `final_brief` prose (~800 tokens) plus `portfolio_advice` (~300 tokens) to generate a LINE message that could be templated. A rule-based formatter (no LLM) could handle the structural reformatting while preserving the content, saving ~$0.004/run ($0.08/month). This is a micro-optimisation but illustrates the "LLM-for-everything" pattern.

---

## 4. Biggest Scalability Bottlenecks

### SB-1: `market_snapshot.json` Is a Single Shared File
If two workflow invocations run simultaneously (manual + cron race), the second invocation's `test_collection.py` will overwrite the file while `investment_workflow.py` from the first run is reading it. No file locking.

### SB-2: `_engine()` Per-Call Pattern Cannot Scale
If dashboard + workflow run concurrently (likely — dashboard is a long-running process), each DB call competes for connections without a shared pool. SQLAlchemy's `pool_pre_ping=True` helps detect stale connections but does not cap the total connection count.

### SB-3: Synchronous Claude API Calls in LangGraph Parallel Branches
`chip_analyst` and `tech_analyst` are declared as a parallel fan-out, but both use synchronous `llm.invoke()`. LangGraph runs them in threads, but the GIL limits true parallelism for CPU-bound work. Since Claude API calls are I/O-bound, Python threads do release the GIL and these should run in parallel — **however, this has not been verified with instrumentation**.

### SB-4: No Async Support End-to-End
The entire workflow stack (LangGraph nodes, database_tools, portfolio_tools, twse_fetcher) is synchronous. Migrating to `async def` nodes and `asyncio` would enable true concurrent API calls, reduce wall time by ~30–40%, and is required for any multi-user extension.

### SB-5: Single `user_portfolio` Table Without Multi-User Support
The portfolio table has no `user_id` column. Adding a second user's portfolio requires architectural changes to the DB schema, portfolio_tools, and all LangGraph nodes that query or format portfolio data.

---

## 5. Priority Improvement Backlog

Listed by impact-to-effort ratio:

| Priority | Item | Impact | Effort | Addresses |
|----------|------|--------|--------|---------|
| **P0** | Add LangGraph `MemorySaver` checkpointer to `investment_workflow` | High | Low | RISK-2 |
| **P0** | Cap Opus thinking: `budget_tokens: 5000` | High | Low | RISK-3, CP-1 |
| **P1** | Add Streamlit authentication (`streamlit-authenticator`) | High | Low | RISK-1 |
| **P1** | Fix `daily_run.sh` in repo to match remote; commit | Medium | Low | TD-9, RISK-5 |
| **P1** | Add `_record_usage` to `backtest_agent.evaluate_node` | Low | Low | CP-2 |
| **P2** | Fix `output_config` → `budget_tokens` in `_llm_opus()` | Medium | Low | TD-6, CP-1 |
| **P2** | Create `config.py` for `MODEL_*` and `PRICING` constants | Low | Low | TD-4 |
| **P2** | Create shared `_get_engine()` singleton in `database_tools` | Medium | Low | SB-2, TD-5 |
| **P2** | Commit `uv.lock` | Medium | Low | TD-8 |
| **P3** | Add `accuracy_logs` table + persist `accuracy_report` | Medium | Medium | TD-7 |
| **P3** | Add `ADD UNIQUE INDEX` on `daily_briefs.trade_date` | Low | Low | Memory gap |
| **P3** | Replace TWSE `verify=False` with proper CA install | Medium | Medium | RISK-4 |
| **P3** | Add `.with_retry(stop_after_attempt=3)` to all LLM calls | High | Medium | RISK-2 partial |
| **P4** | Delete dead MCP tools `save_brief_to_db`, `send_brief_to_user` | Low | Low | TD-2, TD-3 |
| **P4** | Replace `main.py` placeholder with CLI entry point | Low | Low | TD-1 |
| **P4** | Add `user_id` to `user_portfolio` for multi-user readiness | Medium | High | SB-5 |
| **P5** | Migrate all nodes to `async def` + async DB calls | High | High | SB-3, SB-4 |
| **P5** | Add prompt caching via `cache_control` | Medium | Medium | cost reduction |
