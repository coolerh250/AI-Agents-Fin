# Scalability Analysis
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Executive Summary

The current architecture is a **hardwired single-user, single-instance pipeline with no horizontal scaling capability and no queue system**. It is correctly sized for its current purpose: one user, one cron run per day, ~15 seconds wall time. However, three structural constraints — a shared file as the data bus, synchronous LLM calls, and per-call DB engine creation — mean that scaling beyond the current single-run-per-day pattern requires architectural changes before the code breaks under load.

---

## 1. Current Throughput Baseline

| Metric | Current Value |
|--------|--------------|
| Workflows per day | 1 (cron, 08:20 CST) |
| Concurrent users | 1 |
| LLM calls per run | 6 (Haiku×2, Sonnet×3, Opus×1) |
| Wall time per run | ~15–45 seconds |
| DB writes per run | 6 cost_log rows + 1 daily_brief row |
| API cost per run | ~$0.13 USD (expected) |
| Monthly runs | ~20 (trading days) |

---

## 2. Horizontal Scaling

### 2.1 Current: Not Possible

The architecture has no horizontal scaling capability:

- A single `market_snapshot.json` file is the data bus between `test_collection.py` and `investment_workflow.py`. Two simultaneous runs would race to overwrite it.
- The TiDB `daily_briefs` table has no `UNIQUE` constraint on `trade_date`. Two simultaneous runs would insert duplicate rows.
- The `user_portfolio` table has no `user_id` column. It cannot serve multiple users.
- The Streamlit dashboard is a single-process, single-session app with no multi-user session isolation.
- All Python processes run as the same `itadmin` OS user; no process-level isolation.

### 2.2 Bottlenecks for Multi-Instance Deployment

| Bottleneck | Why It Blocks Horizontal Scale |
|-----------|-------------------------------|
| `market_snapshot.json` (shared file) | No file locking; concurrent writes corrupt data |
| `daily_briefs` (no unique constraint on trade_date) | Two instances insert duplicate rows; `get_brief()` returns non-deterministic result |
| `_engine()` per-call pattern | Each process creates its own connection pool; N instances = N×7 connection pools open simultaneously |
| `itadmin` OS user | No process isolation; compromise of one workflow = access to all |
| Streamlit single-instance | Cannot serve multiple users with session-isolated state |
| `ANTHROPIC_API_KEY` (single key, no rate-limiting by workflow) | Concurrent workflows share one key; rate limit applied globally |

### 2.3 Minimum Changes for Dual-Instance Support

If a second user were added today (e.g., for a team), the minimum changes required:

1. Add `user_id` to `user_portfolio`, `daily_briefs`, `cost_logs`
2. Move `market_snapshot.json` to `market_snapshot_{user_id}.json` or to TiDB
3. Add `UNIQUE (trade_date, user_id)` on `daily_briefs`
4. Separate `.env` per user (different Anthropic key per user)
5. Separate cron entries per user
6. Streamlit multi-user session handling (Streamlit does not natively support this; requires a separate instance per user or a proper web framework)

---

## 3. Workflow Scaling

### 3.1 Fan-out Parallelism (Current)

The investment workflow contains one parallel fan-out:

```
data_collector
    ├── chip_analyst    (Sonnet, thread A)
    └── tech_analyst    (Sonnet, thread B)
              └── chief_strategist (waits for both)
```

LangGraph executes these branches in **separate Python threads** when `graph.invoke()` is used (synchronous mode). Since both nodes make outbound HTTPS calls to the Anthropic API, the Python GIL releases on I/O operations. Actual parallelism is achieved at the OS socket level.

**Observed benefit**: ~2–3 seconds saved vs sequential Sonnet calls (unverified; no instrumentation).

**Limitation**: `asyncio.run()` in `agent_orchestrator.py`'s `act_node` creates a new event loop inside a synchronous thread. This pattern is incompatible with running the maintenance agent from within an existing async context (Jupyter, FastAPI, or `await graph.ainvoke()`).

### 3.2 Missing Parallelism Opportunities

| Opportunity | Where | Potential Saving |
|-------------|-------|-----------------|
| `portfolio_tools.calculate_pnl` fetches yfinance prices sequentially | `portfolio_tools.py` | ~1–3s for multi-stock portfolios |
| `save_to_db` and `send_notification` could run in parallel after `format_agent` | `investment_workflow.py` | ~0.5s |
| `test_collection.py` already uses `asyncio.gather` for 3 MCP tool calls | ✅ Implemented | — |

### 3.3 Async Migration Status

| Component | Sync/Async | Migration Risk |
|-----------|-----------|----------------|
| `test_collection.py` | ✅ Fully async | — |
| `agent_orchestrator.py` | Sync wrapper around async (`asyncio.run`) | Medium |
| `market_analyst_agents.py` (all nodes) | ❌ Synchronous `llm.invoke()` | High (all 6 nodes) |
| `database_tools.py` | ❌ Synchronous SQLAlchemy | Medium |
| `portfolio_tools.py` | ❌ Synchronous yfinance | Low |
| `messenger_tools.py` | ❌ Synchronous httpx | Low |
| `investment_workflow.py` (graph) | `graph.invoke()` (sync) | Requires `await graph.ainvoke()` |

**Full async migration** would reduce wall time by 30–40% (estimated) and enable future multi-user concurrent workflow execution. Migration order recommended in `production_architecture_recommendation.md:T3-E`.

---

## 4. Queue System

### 4.1 Current State: None

There is no task queue. All work is either:
- Cron-triggered (fire-and-forget, no retry, no acknowledgement)
- Manual CLI (interactive, no queue)

If the Anthropic API is rate-limited or down at 08:20 CST, the workflow fails with no retry mechanism. The cron job does not attempt a re-run. The brief is not generated that day.

### 4.2 What a Queue Would Enable

| Feature | Without Queue | With Queue |
|---------|--------------|------------|
| Retry on 529 error | ❌ Silent failure | ✅ Auto-retry with backoff |
| Delayed execution (API comes back at 09:00) | ❌ Skip day | ✅ Re-queue at 09:00 |
| Multiple users | ❌ Race condition on files | ✅ One task per user, serialized or parallel |
| Dead-letter queue for investigation | ❌ Logs only | ✅ Failed tasks inspectable |
| Priority runs (manual override) | ❌ Must wait for next cron | ✅ High-priority queue entry |

### 4.3 Queue Options (Ascending Complexity)

**Option A: Python `tenacity` retry (no queue, but addresses transient failures)**

```python
# market_analyst_agents.py — add to all LLM calls
from tenacity import retry, stop_after_attempt, wait_exponential_jitter
import anthropic

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=1, max=30),
    retry=retry_if_exception_type((
        anthropic.RateLimitError,
        anthropic.APITimeoutError,
        anthropic.APIConnectionError,
    ))
)
def _invoke_with_retry(llm, messages):
    return llm.invoke(messages)
```

**Effort**: 30 min. Covers transient 529 errors. Does not handle full process failure.

**Option B: LangGraph `MemorySaver` + cron retry**

Add LangGraph checkpointing (already recommended in `production_architecture_recommendation.md:T2-A`). Modify cron to retry once on failure:

```bash
# daily_run.sh — replace set -euo pipefail with per-step retry
uv run investment_workflow.py || {
    sleep 300  # wait 5 min, then retry once
    uv run investment_workflow.py
}
```

With checkpointing, the retry resumes from the last completed node, not from scratch.

**Effort**: 1 hr. Handles node-level failures and full process restart.

**Option C: Redis + Celery (for future multi-user)**

Requires installing Redis (Docker container) and adding `celery` to dependencies. Each workflow run becomes a Celery task. Supports:
- Multiple users (separate task per user)
- Retry with exponential backoff
- Dead-letter queue
- Task progress visibility

**Effort**: 4–8 hrs. Overkill for single-user system; appropriate if adding 3+ users.

---

## 5. Database Scaling

### 5.1 Current State

TiDB is deployed as a single-node Docker container. For the current workload (20 rows/day, 4 tables), performance is not a concern.

| Metric | Current | Practical Limit (single node) |
|--------|---------|------------------------------|
| Writes per day | ~7 rows | ~10,000/s |
| Reads (dashboard) | Manual, occasional | ~5,000/s |
| Table sizes after 1 year | ~5,000 rows each | Trivial |
| Concurrent connections | ~3 (workflow + dashboard + backtest) | ~100 |

**No scaling concern for the current workload.** However:

**Connection Pool Problem**: `_engine()` creates a new `create_engine()` call on every database function invocation. In a single workflow run, this happens 7–10 times (6 `log_cost` calls + `save_brief` + `get_portfolio`). Each engine instantiation creates a connection pool. When dashboard and workflow run concurrently, connection pools multiply.

```python
# Current pattern (database_tools.py:16-23):
def _engine() -> Engine:
    return create_engine(url, pool_pre_ping=True)
    # New engine created on EVERY call — no pool reuse

# Fix (from production_architecture_recommendation.md:T3-C):
from functools import lru_cache

@lru_cache(maxsize=1)
def _get_engine() -> Engine:
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=2)
    # One engine per process — pool shared across all calls
```

### 5.2 Multi-User DB Scaling Path

If the platform scales to 5–10 users:
- Separate schemas per user (`agent_memory_user1`, `agent_memory_user2`)
- Or: add `user_id` column to all tables + row-level access policies
- TiDB supports horizontal scaling via TiKV sharding if row counts grow to millions

---

## 6. Scalability Bottleneck Map

```
BOTTLENECK SEVERITY (current workload: 1 user, 1 run/day)

CRITICAL for multi-user:
├── market_snapshot.json → shared file, no locking
├── user_portfolio → no user_id
└── daily_briefs → no unique(trade_date, user_id)

HIGH for reliability:
├── No queue → no retry on API failure
├── No LangGraph checkpointer → full restart on any node failure
└── _engine() per-call → connection pool churn

MEDIUM for performance:
├── Synchronous LLM calls → cannot true-parallel with async
├── yfinance sequential per stock → grows with portfolio size
└── format_agent receives full brief → LLM for structural formatting

LOW (not a problem today):
├── TiDB single node → adequate for years at current write rate
├── Disk usage → trivial growth, no rotation needed urgently
└── Anthropic rate limits → well within limits for 1 user/day
```

---

## 7. Scalability Roadmap Summary

| Improvement | Effort | Addresses | Priority |
|-------------|--------|-----------|----------|
| LangGraph `MemorySaver` checkpointer | 15 min | No-checkpoint failure cost | 🔴 P0 |
| Per-LLM retry with jitter (`tenacity`) | 30 min | No queue / API 529 | 🔴 P0 |
| Shared DB engine singleton (`lru_cache`) | 20 min | Connection pool churn | 🟠 P1 |
| `set -euo pipefail` → per-step error handling | 30 min | Data collection failure stops workflow | 🟠 P1 |
| `uv.lock` committed | 10 min | Non-reproducible builds | 🟠 P1 |
| `user_id` in all tables | 4 hrs | Multi-user database isolation | 🟡 P2 (future) |
| `market_snapshot.json` → DB or per-user file | 2 hrs | Multi-instance race condition | 🟡 P2 (future) |
| Full async migration (all nodes) | 8–12 hrs | Parallel I/O, 30–40% faster | 🟡 P2 (future) |
| Redis + Celery task queue | 8 hrs | Multi-user queuing | 🟢 P3 (3+ users) |
