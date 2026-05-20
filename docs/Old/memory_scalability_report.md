# Memory Scalability Report
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Executive Summary

The current memory architecture is **safe for single-user, single-server deployment for the next 2–3 years** with no changes. However, three structural risks will surface as the system scales:

1. **Context explosion**: Not from database growth, but from portfolio size — `portfolio_manager_node` grows linearly with holdings
2. **Connection pool exhaustion**: `_engine()` creates a new connection pool on every DB call — the most dangerous pattern under concurrent load
3. **Retrieval blindness**: The LLM has zero historical memory, which limits prediction quality at any scale

None of these risks require urgent remediation for a single user. This document sizes them precisely and recommends thresholds for action.

---

## Risk 1: Context Explosion

### Source: portfolio_manager_node input growth

```python
pnl_lines = [
    f"股票代碼: {h['stock_id']} | 成本: {h['entry_price']} | 現價: {h['current_price']:.2f} | "
    f"持股數: {h['quantity']} 股 | 損益: {h['unrealized_pnl']:.2f} ({h['pnl_pct']:.2f}%) | "
    f"止損觸發: {'是' if h['stop_loss_triggered'] else '否'} | 策略: {h['strategy_type']}"
    for h in enriched
]
user_content = (
    f"今日市場展望：\n{state['final_brief']}\n\n"
    f"使用者持倉損益：\n" + "\n".join(pnl_lines)
)
```

Each holding generates approximately 80–100 Chinese characters (~110–140 tokens) in the user message.

| Holdings | Portfolio Text Tokens | final_brief Tokens | Total Input Tokens | Within Sonnet 200K Limit? |
|---------|---------------------|-------------------|-------------------|--------------------------|
| 1 (current) | ~130 | ~600 | ~730 | ✅ |
| 5 | ~650 | ~600 | ~1250 | ✅ |
| 20 | ~2600 | ~600 | ~3200 | ✅ |
| 100 | ~13000 | ~600 | ~13600 | ✅ |
| 500 | ~65000 | ~600 | ~65600 | ✅ |
| 1500 | ~195000 | ~600 | ~195600 | ⚠️ Approaching limit |

**Practical concern**: The context window is not the bottleneck — cost and latency are.

| Holdings | Sonnet Input Cost (est.) | Latency Impact |
|---------|--------------------------|---------------|
| 1 | $0.002/run | Baseline |
| 20 | $0.010/run | +$0.008/run |
| 100 | $0.041/run | Significant |

**Threshold for action**: At 20+ holdings, add a portfolio summarization step before `portfolio_manager_node` that groups holdings by strategy type and aggregates metrics rather than enumerating each row.

---

### Source: snapshot field in WorkflowState

The `snapshot` dict (3–8 KB raw JSON) remains in `WorkflowState` for the entire workflow run but is only consumed by `data_collector_node`. It occupies memory through 6 subsequent nodes.

For current payload sizes, this is irrelevant. If MCP tools return more data in the future (e.g., detailed TAIFEX option chain data), the snapshot could grow to 50–100 KB.

**Fix when needed**: Zero out the field after `data_collector_node`:
```python
def data_collector_node(state: WorkflowState) -> dict:
    ...
    return {"raw_market_data": raw_market_data, "snapshot": {}}  # release memory
```
This cannot be done today without breaking the chip/tech analyst fallback paths that read `state["snapshot"]["tools"]["..."]`.

---

## Risk 2: Connection Pool Exhaustion

### The Problem

```python
def _engine() -> Engine:
    return create_engine(url, pool_pre_ping=True)  # NEW pool on EVERY call
```

`create_engine()` creates a new `QueuePool` (default: `pool_size=5, max_overflow=10`) on every call. Each pool holds up to 15 TiDB connections.

**Connection count per workflow run**:

| Caller | `_engine()` calls | Connections created |
|--------|------------------|-------------------|
| `log_cost()` × 6 nodes | 6 | Up to 90 |
| `save_brief()` | 1 | Up to 15 |
| `get_portfolio()` | 1 | Up to 15 |
| Dashboard: `get_portfolio()` | 1 | Up to 15 |
| Dashboard: `get_cost_summary()` | 1 | Up to 15 |
| Dashboard: `get_cost_trend()` | 1 | Up to 15 |
| Dashboard: `get_recent_accuracy()` | 1 | Up to 15 |

**Under concurrent load** (cron 08:20 workflow + user has dashboard open):
- Total `_engine()` calls: ~12–14
- Maximum TiDB connections: ~180–210

**TiDB Cloud default connection limit**: 500 for Developer Tier, 2,500 for Dedicated. Current usage is well within limits, but each new pool:
1. Opens a TCP connection to TiDB
2. Spawns background threads for pool management
3. Keeps connections alive for up to 10 minutes (default `pool_recycle`)

**Thread accumulation over time**: If the dashboard runs for 8 hours with 3600s cache TTL, it triggers ~8 `_engine()` calls per tab. With 10 browser tabs: ~80 pools × potential 5 connections = 400 background threads.

**Fix** (from `docs/tool_risk_matrix.md`):
```python
from functools import lru_cache

@lru_cache(maxsize=1)
def _engine() -> Engine:
    return create_engine(
        url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 10, "read_timeout": 30},
    )
```

This is the **highest-impact single-line fix** in the entire memory architecture.

---

### Database Growth Projections

| Table | Current Rows | Growth Rate | 1 Year | 3 Years | 10 Years |
|-------|-------------|------------|--------|---------|---------|
| `daily_briefs` | ~4 | 250 rows/year | ~250 | ~750 | ~2,500 |
| `market_actuals` | ~0 | 250 rows/year | ~250 | ~750 | ~2,500 |
| `cost_logs` | ~24 | 1,200 rows/year* | ~1,200 | ~3,600 | ~12,000 |
| `user_portfolio` | ~1 | ~10–50 rows/year | ~50 | ~150 | ~500 |

_* 6 nodes × 20 trading days/month × 10 months = 1,200 rows/year (excludes weekends/holidays)_

**Storage estimates** (compressed TiDB storage, ~1:3 ratio):
| Table | Row Size | 10-Year Raw Size | 10-Year Compressed |
|-------|---------|-----------------|-------------------|
| `daily_briefs` | ~8 KB (brief_text dominates) | ~20 MB | ~7 MB |
| `market_actuals` | ~100 bytes | ~250 KB | ~83 KB |
| `cost_logs` | ~100 bytes | ~1.2 MB | ~400 KB |
| `user_portfolio` | ~200 bytes | ~100 KB | ~33 KB |

**Conclusion**: TiDB storage growth is negligible at current scale. There is no capacity risk from database growth for single-user operation over any realistic time horizon.

---

## Risk 3: Retrieval Latency

### Bottleneck 1: yfinance sequential per-holding calls

| # Holdings | calculate_pnl() latency | portfolio_manager_node total | User waits (LINE delivery) |
|-----------|------------------------|------------------------------|---------------------------|
| 1 | 1–3s | +3–5s | ~65–95s total workflow |
| 5 | 5–15s | +7–17s | ~75–105s total workflow |
| 20 | 20–60s | +22–62s | ~90–130s total workflow |

**Root cause**: yfinance `Ticker.history()` is a synchronous blocking HTTP call. There is no parallelism in `calculate_pnl()`. With 20 holdings, the portfolio enrichment step alone takes longer than the entire LLM pipeline.

**Fix**: Run yfinance calls concurrently using `asyncio.gather()` with `run_in_executor()`:
```python
async def calculate_pnl_async(holdings: list[dict]) -> list[dict]:
    loop = asyncio.get_event_loop()
    async def _fetch(h):
        df = await loop.run_in_executor(
            None, lambda: yf.Ticker(f"{h['stock_id']}.TW").history(period="1d")
        )
        return float(df["Close"].iloc[-1]) if not df.empty else None
    prices = await asyncio.gather(*[_fetch(h) for h in holdings])
    # enrich with prices[i]
```

This reduces 20-holding latency from ~20–60s to ~2–5s (single batch of concurrent calls).

---

### Bottleneck 2: `_engine()` connection overhead per DB call

Without the `@lru_cache` fix, each DB function call incurs:
1. `create_engine()`: ~5ms (Python object creation + pool initialization)
2. `pool.connect()`: ~20–50ms (new TCP connection to TiDB if pool is cold)
3. SQL execution: ~5–30ms (indexed query)
4. Pool cleanup: ~2ms

**With `@lru_cache(maxsize=1)`**:
1. `_engine()`: ~0.001ms (dict lookup)
2. `pool.connect()`: ~0.1ms (reuse pooled connection)
3. SQL execution: ~5–30ms (unchanged)

**Effective speedup per DB call**: ~25–50ms → ~5–30ms. For a workflow that makes 8 DB calls, this saves ~160–400ms per run.

---

### Bottleneck 3: Aggregation queries as cost_logs grows

`get_cost_summary(days=30)` aggregates `cost_logs` for the time window:
```sql
WHERE logged_at >= NOW() - INTERVAL 30 DAY
GROUP BY agent_name, model_name
```

With `idx_logged_at` index, the time filter is efficient. The GROUP BY forces a partial scan of the selected rows.

| Row count in 30-day window | Estimated query time |
|---------------------------|---------------------|
| 120 (current, 20 runs) | ~5–10ms |
| 600 (5 users, 1 year) | ~10–20ms |
| 6,000 (multi-tenant, 3 years) | ~30–60ms |

**Not a concern for single-user deployment**. Would need a composite index `(agent_name, model_name, logged_at)` if multi-tenant use is added.

---

## Risk 4: File System Growth

### collection_journal.jsonl

```
~300 bytes/entry × 20 entries/month × 12 months = ~72 KB/year
× 10 years = ~720 KB
```

No risk. However, the file is **never read** by any code — it is write-only. It grows indefinitely with zero benefit.

**Options**:
A. Delete it (accept the data loss)
B. Add a consumer (trend analysis on collection reliability)
C. Rotate at 10,000 lines with `logrotate` equivalent

---

### investment_brief_*.txt files

```
~2 KB/file × 20 files/month × 12 = ~480 KB/year
× 10 years = ~4.8 MB + ~2,400 files
```

No storage risk. Operational confusion risk (2,400 brief files in the project root).

**Recommended**: Either write to a `briefs/` subdirectory, or stop writing these files entirely (the brief is already in TiDB `daily_briefs.brief_text`).

---

## Risk 5: No Recovery from LLM Failure Mid-Workflow

**Current state**: `graph.compile()` with no checkpointer.

If any node raises an exception after `chief_strategist_node` completes (e.g., TiDB connection failure in `save_to_db_node`), the result is:
- Opus computation (~$0.048, ~30s) is **lost**
- `daily_briefs` row is **not written**
- LINE notification is **not sent**
- No recovery is possible — the workflow must re-run from scratch (spending $0.048 again)

**LangGraph checkpointer fix**:
```python
from langgraph.checkpoint.sqlite import SqliteSaver

memory = SqliteSaver.from_conn_string("checkpoints.db")
graph = build_graph().compile(checkpointer=memory)

# On retry, resumes from the last completed node:
result = graph.invoke(initial_state, config={"configurable": {"thread_id": trade_date}})
```

**Estimated effort**: 30 minutes. **Estimated value**: prevents $0.048 re-runs and avoids missed trading days.

---

## Summary: Risk Register

| Risk | Current Severity | Threshold for Urgent Action | Fix |
|------|----------------|----------------------------|-----|
| `_engine()` pool exhaustion | 🟡 HIGH (concurrent load) | 3+ concurrent DB callers | `@lru_cache(maxsize=1)` |
| Stale `market_snapshot.json` | 🔴 CRITICAL (correctness) | Already triggered by any failed cron | Freshness timestamp check |
| `calculate_pnl()` serial latency | 🟠 MEDIUM | ≥5 holdings | `asyncio.gather()` + executor |
| No workflow checkpointer | 🟡 HIGH (reliability) | Already present (happens on any DB failure) | `SqliteSaver` |
| `portfolio_manager_node` context growth | 🟢 LOW | ≥50 holdings | Portfolio aggregation step |
| `daily_briefs` duplicate rows | 🟠 MEDIUM | Run workflow twice on same day | UNIQUE KEY on `trade_date` |
| `collection_journal.jsonl` write-only | 🟢 LOW | Already wasted | Add consumer or delete |
| `investment_brief_*.txt` accumulation | 🟢 LOW | ~2400 files in 10 years | Move to `briefs/` subdir |
| TiDB storage growth | 🟢 NEGLIGIBLE | N/A | No action needed |
| Context window growth | 🟢 LOW | ≥50 holdings | Summarization step |
