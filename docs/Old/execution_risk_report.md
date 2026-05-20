# Execution Risk Report
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Risk Severity Scale

| Symbol | Level | Definition |
|--------|-------|-----------|
| 🔴 | CRITICAL | Can cause data loss, silent failure, or runaway cost in current production |
| 🟡 | HIGH | Will cause problems under realistic conditions; needs remediation plan |
| 🟢 | LOW | Theoretical risk; acceptable for single-user deployment |

---

## 1. Infinite Loop Risk

**Verdict: 🟢 NOT PRESENT**

All three graphs are pure DAGs. Static edge analysis confirms:

| Graph | Cycle Check | Back-edges Found |
|-------|------------|-----------------|
| `investment_workflow` | ✅ Acyclic | 0 |
| `backtest_agent` | ✅ Acyclic | 0 |
| `maintenance_agent` | ✅ Acyclic | 0 |

No `add_conditional_edges` with a self-referencing route function exists. LangGraph's `StateGraph.compile()` performs its own cycle detection and would raise `ValueError` at compile time if a cycle were introduced.

**Future risk**: If recursive reflection (self-correction loops) is added — e.g., a `quality_gate_node` that routes back to `chief_strategist` if the brief score is low — a maximum iteration counter (`recursion_limit`) must be set. Default is 25; custom: `graph.compile(recursion_limit=5)`.

---

## 2. Retry Storm Risk

**Verdict: 🟡 HIGH (latent)**

### 2.1 Current Situation

No retry logic exists anywhere. All LLM calls are single-attempt. On failure, the entire graph aborts.

```python
# Every node does this — no retry wrapper:
response = llm.invoke([SystemMessage(...), HumanMessage(...)])
```

### 2.2 The Storm Scenario

If retry were naively added using `.with_retry()` to both `chip_analyst` and `tech_analyst` (parallel branches), and Claude enters a 529 storm:

1. Both branches issue their first call → both receive 529
2. Both branches wait `backoff(1)` seconds, then retry simultaneously → both receive 529 again
3. Both branches double backoff → retry simultaneously again
4. At `stop_after_attempt=3`, both branches are now creating 3× the API load simultaneously

**With `stop_after_attempt=3` and `wait_exponential(multiplier=1, min=2, max=60)`:**
- Worst case: 2 parallel branches × 3 attempts × 60s max wait = up to 360 API calls competing in the first 3 minutes of an outage window
- This amplifies pressure on a rate-limited API, worsening the overload that caused the 529

### 2.3 Safe Retry Configuration

```python
# CORRECT — jitter prevents synchronized retries across parallel branches:
from langchain_core.rate_limiters import InMemoryRateLimiter

llm = ChatAnthropic(...).with_retry(
    stop_after_attempt=3,
    wait_exponential_jitter=True,   # adds random jitter to break synchronization
    retry_if_exception_type=(anthropic.RateLimitError, anthropic.APITimeoutError),
)
```

---

## 3. Deadlock Risk

**Verdict: 🟡 HIGH (fan-in failure scenario)**

### 3.1 The Fan-in Failure Scenario

In `investment_workflow`, `chief_strategist` waits for BOTH `chip_analyst` and `tech_analyst` to complete. If one branch fails:

**Scenario A — chip_analyst raises, tech_analyst succeeds:**
- LangGraph propagates the exception from `chip_analyst`
- The graph runner detects a failed node
- `chief_strategist` is never scheduled — the join condition cannot be met
- All work done by `tech_analyst` is discarded (it ran successfully but its output is never used)
- The graph raises the chip_analyst exception to `graph.invoke()`'s caller

**Scenario B — tech_analyst raises after chip_analyst succeeds:**
- Same outcome — `chief_strategist` is blocked, tech_analyst exception propagates

**Result**: No true "deadlock" in the traditional sense (no thread is stuck waiting forever), but the semantic effect is equivalent: the join point is permanently unresolvable once a branch fails, and there is no recovery path.

### 3.2 Why This Is HIGH Risk

Claude API returns 529 Overloaded errors multiple times per month during peak hours. Since `chip_analyst` and `tech_analyst` run in parallel, only one needs to receive a 529 for the entire daily brief to be lost. At current API reliability, this represents a meaningful risk to daily brief generation.

### 3.3 Remediation

```python
# Option A: Per-node try/except + fallback state
def chip_analyst_node(state: WorkflowState) -> dict:
    try:
        response = llm.invoke(...)
        return {"chip_report": _extract_text(response)}
    except Exception as exc:
        logger.error(f"[ChipAnalyst] failed: {exc}")
        return {"chip_report": json.dumps({"sentiment": "unknown", "error": str(exc)})}

# Option B: LangGraph node-level error handling (LangGraph >= 0.2)
# graph.add_node("chip_analyst", chip_analyst_node, retry_policy=RetryPolicy(max_attempts=3))
```

---

## 4. Token Explosion Risk

**Verdict: 🔴 CRITICAL**

### 4.1 Source: `chief_strategist_node` — Unbounded Adaptive Thinking

```python
# market_analyst_agents.py:122-129
def _llm_opus() -> ChatAnthropic:
    return ChatAnthropic(
        model=_MODEL_OPUS,
        max_tokens=16000,                    # 🔴 8× actual output size
        thinking={"type": "adaptive"},        # 🔴 no budget_tokens cap
        output_config={"effort": "high"},     # ⚠ invalid param — silently ignored
    )
```

### 4.2 Blast Radius

| Scenario | Thinking Tokens | Chief Strategist Cost | Total Run Cost |
|----------|----------------|----------------------|----------------|
| Normal day | ~2 000 | ~$0.05 | ~$0.13 |
| Volatile day (e.g., TSMC ADR ±5%) | ~8 000 | ~$0.12 | ~$0.22 |
| Extreme event (Taiwan election, Fed decision) | ~12 000+ | ~$0.20 | ~$0.35 |
| **Worst case (max_tokens=16000 hit)** | ~14 000 | **~$0.43** | **~$0.60** |

Observed on 2026-05-14: actual run cost was $0.127 vs expected $0.075 — a 69% overage attributable to thinking tokens not captured in `usage_metadata["output_tokens"]`.

### 4.3 Immediate Fix

```python
# Replace adaptive thinking with bounded enabled thinking:
thinking={"type": "enabled", "budget_tokens": 5000},
max_tokens=2048,  # actual output is 600-1000 tokens; 2048 is sufficient headroom
# Remove: output_config={"effort": "high"}  # invalid parameter
```

**Result:** Chief strategist cost becomes deterministic at ~$0.027/run. Monthly cost drops from ~$2.54 (expected) to ~$1.60 (capped).

### 4.4 Secondary Risk: `max_tokens=16000` on Sonnet Nodes

No other node has an excessive `max_tokens`. `portfolio_manager` and `format_agent` both use 1024/2048 respectively, which are appropriate.

---

## 5. Additional Execution Risks

### 5.1 🟡 No Error Propagation Boundary

**Risk**: All nodes either raise exceptions (crashing the graph) or silently return degraded state. There is no middle ground — no node returns a "partial success" with an error flag that downstream nodes can check.

**Example**: `data_collector_node` returns `{}` on JSON parse failure (line 183-184):
```python
except Exception:
    logger.warning("[DataCollector] JSON 解析失敗，使用空 dict")
    raw_market_data = {}
```

`chip_analyst_node` then falls back to raw snapshot data (line 196-197). This fallback is intentional and well-implemented. However, `chief_strategist` receives no signal that the upstream compression step failed — it proceeds with potentially lower-quality chip data without knowing the data quality issue.

### 5.2 🟡 asyncio.run() Anti-Pattern in Synchronous Node

**File:** `agent_orchestrator.py:75`

```python
def act_node(state: AgentState) -> dict:
    stats = asyncio.run(_fetch_mcp_stats())  # ← blocks the thread; crashes in async context
```

`asyncio.run()` creates a **new** event loop and blocks the current thread until the coroutine completes. This is safe in a CLI context. It will raise `RuntimeError: This event loop is already running` if:
- Called from inside an existing event loop (Jupyter, FastAPI, async test)
- Called from `await graph.ainvoke()` (async LangGraph invocation)

### 5.3 🟡 `send_notification_node` Silent Failure

**File:** `market_analyst_agents.py:319`

```python
results = send_brief(report)
for channel, res in results.items():
    if status != "ok":
        logger.warning(...)
return {}  # always returns success — notification failure does not fail the graph
```

If LINE/Telegram push fails (network error, expired token), the graph completes with `status="success"` and `db_row_id` is set. The brief is persisted in TiDB but the user never receives the notification. This is by design (non-fatal), but there is no alert mechanism or retry for the push itself.

### 5.4 🟢 `save_to_db_node` Swallows DB Failure

**File:** `market_analyst_agents.py:342`

```python
except Exception as exc:
    logger.error(f"[SaveToDB] 寫入失敗: {exc}")
    return {"db_row_id": None}
```

If TiDB is unavailable, `save_to_db_node` returns `db_row_id=None` and the graph continues to `send_notification`. The LINE push succeeds but the brief is not persisted. Backtest for this day will find no record in `daily_briefs`.

### 5.5 🟢 No Concurrent Run Guard

`market_snapshot.json` is a single shared file. If a manual invocation of `investment_workflow.py` runs while the cron job is also running (both triggered around 08:20 CST), they will read the same snapshot file but create duplicate `daily_briefs` rows (since there is no UNIQUE constraint on `daily_briefs.trade_date`). The `get_brief()` function handles this with `ORDER BY id DESC LIMIT 1`, but duplicate rows accumulate.

---

## 6. Risk Summary Matrix

| Risk | Graph | Severity | Probability | Remediation |
|------|-------|----------|-------------|-------------|
| Token explosion (adaptive thinking) | investment_workflow | 🔴 CRITICAL | HIGH (every volatile day) | Set `budget_tokens: 5000` |
| Fan-in branch failure → no daily brief | investment_workflow | 🟡 HIGH | MEDIUM (monthly 529 events) | Add per-node try/except + fallback |
| No checkpoint → expensive work lost | investment_workflow | 🟡 HIGH | MEDIUM | Add MemorySaver checkpointer |
| Retry storm (if retry added naively) | investment_workflow | 🟡 HIGH | LOW (future) | Use `wait_exponential_jitter=True` |
| asyncio.run() in sync node | maintenance_agent | 🟡 HIGH | LOW (CLI only today) | Wrap in `asyncio.get_event_loop()` check |
| Silent notification failure | investment_workflow | 🟢 LOW | LOW | Alert log or fallback channel |
| DB write failure silent | investment_workflow | 🟢 LOW | LOW | Accept — non-fatal by design |
| Duplicate run → duplicate DB rows | investment_workflow | 🟢 LOW | LOW | Add UNIQUE on `daily_briefs.trade_date` |
