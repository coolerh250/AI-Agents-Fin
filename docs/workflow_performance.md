# Workflow Performance Analysis
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## 1. Wall-Time Profile — investment_workflow

All timing based on observed latency from `cost_logs` and API benchmarks.

### 1.1 Node Execution Time Estimates

| Node | Model | Network | DB | Est. Duration | Notes |
|------|-------|---------|----|----|-------|
| `data_collector` | Haiku | ~800 ms | — | 0.8–1.5 s | Small prompt, fast Haiku |
| `chip_analyst` | Sonnet | ~1.5–2 s | — | 1.5–2.5 s | ← parallel |
| `tech_analyst` | Sonnet | ~1.5–2 s | — | 1.5–2.5 s | ← parallel |
| `chief_strategist` | Opus+Thinking | ~15–45 s | — | 15–45 s | DOMINANT bottleneck |
| `portfolio_manager` | Sonnet | ~1.5–2 s | yfinance ~0.5 s | 2–3 s | Includes live price fetch |
| `format_agent` | Haiku | ~0.8–1 s | — | 0.8–1.5 s | — |
| `save_to_db` | — | — | ~100 ms | 0.1–0.3 s | TiDB write |
| `send_notification` | — | LINE/TG ~500 ms | — | 0.3–1 s | HTTP POST |

### 1.2 Critical Path (Longest Execution Chain)

```
data_collector (1s)
    │
    ├── chip_analyst (2s) ─────── ← runs in parallel, BUT
    └── tech_analyst (2s) ─────── chief_strategist waits for BOTH
              │
              ▼
   chief_strategist (30s) ← ⚠️ DOMINANT — 41% of cost, ~70% of wall time
              │
   portfolio_manager (2.5s)
              │
      format_agent (1s)
              │
        save_to_db (0.2s)
              │
    send_notification (0.5s)
```

**Estimated total wall time:**
- Best case (simple market): ~38 seconds
- Typical: ~42–50 seconds
- Worst case (volatile market + slow Opus thinking): ~60–70 seconds

**Critical path formula:**
```
T_total = T_data_collector + max(T_chip, T_tech) + T_chief + T_portfolio + T_format + T_db + T_notify
        ≈ 1 + 2 + 30 + 2.5 + 1 + 0.2 + 0.5
        ≈ 37 seconds (typical)
```

`chief_strategist` alone accounts for **~80% of wall time**.

---

## 2. Parallelizable Nodes

### 2.1 Currently Parallel (Declared via Fan-out)

| Nodes | Parallel? | How | Verified? |
|-------|-----------|-----|-----------|
| `chip_analyst` + `tech_analyst` | ✅ YES | LangGraph spawns threads | ❌ Not instrumented |

LangGraph's synchronous graph runner uses Python threads for fan-out branches. Since both nodes issue I/O-bound HTTP calls to the Claude API, the GIL is released during socket blocking, and true concurrency is achieved at the OS network level.

**Theoretical savings**: ~1.5–2 seconds per run (one Sonnet call vs two sequential). Marginal relative to chief_strategist's 30-second dominance.

### 2.2 Currently NOT Parallel — Could Be

| Node Pair | Why Not Parallel Currently | Parallelizable? | Risk |
|-----------|--------------------------|-----------------|------|
| `save_to_db` + `send_notification` | Sequential edge only | ✅ YES | Low — they write to different systems |
| `portfolio_manager` (DB + yfinance) + `format_agent` setup | Depends on `portfolio_advice` | ❌ NO | `format_agent` needs `portfolio_advice` |
| `backtest_agent` nodes | All sequential | Partial | `load_brief` + `fetch_actual` could be parallel |

**Optimization opportunity**: `save_to_db` and `send_notification` could run in parallel since they use different I/O targets (TiDB vs LINE API). This would save ~0.2–0.5 seconds — negligible given chief_strategist's dominance.

### 2.3 Blocked by Data Dependency — Cannot Parallelize

```
chip_analyst ──needs──► raw_market_data ──produced by──► data_collector
chief_strategist ──needs──► chip_report + tech_report ──► both parallel branches
portfolio_manager ──needs──► final_brief ──produced by──► chief_strategist
format_agent ──needs──► portfolio_advice ──produced by──► portfolio_manager
```

The data dependency chain is the primary constraint on parallelism. The critical path through chief_strategist cannot be shortened by parallelism alone.

---

## 3. Blocking Nodes

All nodes are synchronous and block their executing thread:

| Node | Blocking Operation | Duration | Can Be Made Async? |
|------|-------------------|----------|--------------------|
| `data_collector` | `llm.invoke()` → HTTP | ~1 s | ✅ `llm.ainvoke()` |
| `chip_analyst` | `llm.invoke()` → HTTP | ~2 s | ✅ `llm.ainvoke()` |
| `tech_analyst` | `llm.invoke()` → HTTP | ~2 s | ✅ `llm.ainvoke()` |
| `chief_strategist` | `llm.invoke()` → HTTP | 15–45 s | ✅ `llm.ainvoke()` |
| `portfolio_manager` | `yfinance.download()` + `llm.invoke()` | ~2.5 s | ✅ both async-capable |
| `format_agent` | `llm.invoke()` → HTTP | ~1 s | ✅ `llm.ainvoke()` |
| `save_to_db` | SQLAlchemy `conn.execute()` | ~0.2 s | ✅ `asyncio` + async engine |
| `send_notification` | `httpx.post()` | ~0.5 s | ✅ `httpx.AsyncClient` |
| `act_node` (maintenance) | `asyncio.run()` | ~1 s | ⚠️ Already async internally but wrapped in sync |

**Performance impact of async migration**: Migrating all nodes to `async def` with `await llm.ainvoke()` would:
- Enable fan-out branches to use true `asyncio` concurrency (no GIL constraint)
- Allow `portfolio_manager`'s yfinance call and portfolio DB read to run concurrently with each other
- Reduce wall time by ~5–10% (mostly from eliminating thread switching overhead)
- Enable future multi-user support (concurrent workflow invocations without thread explosion)

---

## 4. Expensive Nodes

### 4.1 By Cost

| Node | Cost/Run | % of Total | Monthly (20 days) |
|------|----------|------------|-------------------|
| `chief_strategist` | ~$0.053 | 41% | ~$1.06 |
| `portfolio_manager` | ~$0.008 | 6.4% | ~$0.16 |
| `chip_analyst` | ~$0.005 | 3.5% | ~$0.09 |
| `tech_analyst` | ~$0.004 | 3.3% | ~$0.08 |
| `format_agent` | ~$0.004 | 3.4% | ~$0.09 |
| `data_collector` | ~$0.002 | 1.3% | ~$0.03 |
| **TOTAL** | **~$0.075** | | **~$1.51** |

> Actual observed cost on 2026-05-14: $0.127 (thinking tokens cause 69% overage)

### 4.2 By Latency (Wall Time)

| Node | Latency | % of Total Wall Time |
|------|---------|---------------------|
| `chief_strategist` | 15–45 s | ~74–79% |
| `portfolio_manager` | 2–3 s | ~5–7% |
| `chip_analyst` (parallel) | 1.5–2.5 s | N/A (overlaps tech_analyst) |
| `tech_analyst` (parallel) | 1.5–2.5 s | ~5–7% (critical path) |
| `data_collector` | 0.8–1.5 s | ~2–3% |
| `format_agent` | 0.8–1.5 s | ~2–3% |
| `send_notification` | 0.3–1 s | ~1–2% |
| `save_to_db` | 0.1–0.3 s | <1% |

### 4.3 Cost Reduction Levers

| Lever | Node | Estimated Saving | Quality Risk |
|-------|------|-----------------|-------------|
| Set `budget_tokens: 5000` on Opus | `chief_strategist` | ~$0.025/run (47% of Opus cost) | Low |
| Reduce `max_tokens` to 2048 | `chief_strategist` | Prevents runaway output | None |
| Remove invalid `output_config` | `chief_strategist` | Cleaner API call | None |
| Replace `format_agent` LLM with rule-based formatter | `format_agent` | ~$0.004/run | Medium (format quality) |
| Summarize `final_brief` before passing to `portfolio_manager` | `portfolio_manager` | ~$0.003/run | Low-Medium |
| Prompt caching on system prompts | All nodes | Up to 90% on cached tokens | None |

---

## 5. Backtest Agent — Performance Profile

| Node | Operation | Duration |
|------|-----------|---------|
| `load_brief` | TiDB SELECT | ~100 ms |
| `fetch_actual` | TWSE HTTP + TiDB UPSERT | ~2–5 s (TWSE varies) |
| `evaluate` | Haiku LLM | ~1–2 s |
| **TOTAL** | | **~3–7 s** |

**Bottleneck**: TWSE API response time (occasionally 3–5 seconds due to TW gov server load).
**Risk**: If TWSE times out (10 s default), `fetch_actual` blocks the thread for 10 seconds before failing.

---

## 6. Maintenance Agent — Performance Profile

| Node | Operation | Duration |
|------|-----------|---------|
| `act_node` | `asyncio.run(_fetch_mcp_stats())` → subprocess spawn + psutil | ~2–3 s |
| `think_node` | Haiku LLM | ~1–2 s |
| **TOTAL** | | **~3–5 s** |

**Bottleneck**: Subprocess spawn for `system_inspector.py` MCP server (~1–2 s cold start per invocation). There is no persistent MCP connection — the subprocess is spawned and torn down every run.

---

## 7. Performance Improvement Priority

Listed by impact:

| Priority | Change | Wall Time Reduction | Cost Reduction |
|----------|--------|--------------------|--------------| 
| **P0** | Cap Opus `budget_tokens: 5000` | Minor (reduces max thinking time) | **~$0.025/run** |
| **P1** | Add LangGraph `.with_retry()` + jitter | +0–5 s (retry overhead on success path) | None (prevents wasted reruns) |
| **P2** | Add `MemorySaver` checkpoint | None (overhead <100 ms) | Prevents rerun waste |
| **P3** | Replace `format_agent` with rule-based | ~1 s saved | ~$0.004/run |
| **P4** | Async migration (`async def` + `ainvoke`) | ~5–10% wall time | None direct |
| **P5** | Prompt caching for system prompts | None (API latency unchanged) | Up to 90% on system prompt tokens |
