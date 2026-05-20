# Optimization Opportunities
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Summary Table

| Opportunity | Estimated Saving | Effort | Priority |
|------------|-----------------|--------|---------|
| Cap Opus `max_tokens` to 4096 | 0–15% cost reduction | 1 min | **P0** |
| Add `budget_tokens` to Opus thinking | 30–60% Opus cost reduction on complex days | 5 min | **P0** |
| `@lru_cache` on `_engine()` | Eliminates pool thrashing, -25ms/DB call | 1 line | **P0** |
| `@lru_cache` on `_llm()` factory | Eliminates ChatAnthropic re-instantiation overhead | 3 lines | **P1** |
| Async `calculate_pnl()` | -80% yfinance latency at ≥5 holdings | 30 min | **P1** |
| Add `_record_usage()` to backtest + orchestrator | Closes cost visibility gap | 10 min | **P1** |
| Reduce `final_brief` duplication | ~$0.004–$0.007/run saving | 20 min | **P2** |
| Anthropic prompt caching for system prompts | ~20–30% Haiku/Sonnet input cost reduction | 2 hrs | **P2** |
| Portfolio aggregation before Opus | Required at ≥20 holdings | Triggered | **P3** |
| data_collector fallback error handling | Prevents large raw data injection into Sonnet | 15 min | **P2** |

---

## Caching Opportunities

### 1. `@lru_cache` on `_engine()` — CRITICAL

```python
# database_tools.py:16 — CURRENT (creates new pool every call)
def _engine() -> Engine:
    return create_engine(url, pool_pre_ping=True)

# FIX
from functools import lru_cache

@lru_cache(maxsize=1)
def _engine() -> Engine:
    return create_engine(
        url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 10, "read_timeout": 30},
    )
```

**Impact**: Eliminates 12–14 redundant `create_engine()` calls per daily cycle (each creating a new 5-connection pool). Saves ~25–50ms per DB call. Prevents potential thread accumulation under concurrent load (dashboard + workflow).

### 2. `@lru_cache` on `_llm()` factory

```python
# market_analyst_agents.py:114 — CURRENT (creates new ChatAnthropic every node invocation)
def _llm(model: str, max_tokens: int = 1024) -> ChatAnthropic:
    return ChatAnthropic(...)

# FIX
from functools import lru_cache

@lru_cache(maxsize=8)
def _llm(model: str, max_tokens: int = 1024) -> ChatAnthropic:
    return ChatAnthropic(model=model, api_key=os.getenv("ANTHROPIC_API_KEY"), max_tokens=max_tokens)
```

**Impact**: Each workflow run currently creates 5 `ChatAnthropic` objects (for 5 `_llm()` calls) + 1 `_llm_opus()`. With caching, the objects are reused across runs if the process stays alive (e.g., in a long-running cron daemon). Savings are minor for one-shot CLI runs but meaningful in server deployments.

### 3. Anthropic Prompt Caching (Beta)

The 6 system prompts in `market_analyst_agents.py` are static — they never change between runs. Anthropic's prompt caching caches the KV state of a prompt prefix, reducing input token costs by ~90% for the system prompt portion on subsequent calls within a 5-minute window.

```python
# Example for chip_analyst_node:
from anthropic import Anthropic

client = Anthropic()
response = client.messages.create(
    model=_MODEL_SONNET,
    max_tokens=1024,
    system=[{
        "type": "text",
        "text": _CHIP_SYSTEM,
        "cache_control": {"type": "ephemeral"},  # cache this prefix
    }],
    messages=[{"role": "user", "content": user_content}],
)
```

**Estimated saving** (if workflow runs multiple times per day, e.g., test runs):
- Sonnet system prompt: ~140 tokens × $3/M × 0.9 discount = $0.00038 saved per cached hit
- Haiku system prompt: ~90 tokens × $1/M × 0.9 discount = negligible

**Limitation**: Cache TTL is 5 minutes. Daily cron workflows running once per day get no cache benefit (cache is always cold). Only valuable for dev/test iteration where the same prompt is called multiple times in quick succession.

---

## Small Model Replacement

### Current routing assessment

| Node | Current Model | Could Downgrade? | Risk |
|------|-------------|-----------------|------|
| data_collector | Haiku ✅ | Already optimal | — |
| format_agent | Haiku ✅ | Already optimal | — |
| evaluate (backtest) | Haiku ✅ | Already optimal | — |
| think (orchestrator) | Haiku ✅ | Already optimal | — |
| chip_analyst | Sonnet | No — needs judgment | Output quality degrades without Sonnet's reasoning |
| tech_analyst | Sonnet | No — uses weighted formula logic | Output quality degrades |
| portfolio_manager | Sonnet | Borderline | Stop-loss decisions require reliable judgment |
| chief_strategist | Opus | No — primary quality driver | Core product value |

**Verdict**: The model assignments are already well-optimized. The lowest-risk downgrade candidate is `portfolio_manager_node` to Haiku for simple single-holding cases, but this risks incorrect buy/sell advice — the cost saving (~$0.007/run) does not justify the risk.

---

## Async Execution

### 1. Parallelize `calculate_pnl()` yfinance calls

**Current** (`portfolio_tools.py`):

```python
def calculate_pnl(holdings: list[dict]) -> list[dict]:
    for h in holdings:                                      # sequential
        df = yf.Ticker(f"{h['stock_id']}.TW").history(period="1d")
```

**Fix**:

```python
import asyncio

async def calculate_pnl_async(holdings: list[dict]) -> list[dict]:
    loop = asyncio.get_event_loop()

    async def _fetch(h: dict) -> float | None:
        try:
            df = await loop.run_in_executor(
                None, lambda: yf.Ticker(f"{h['stock_id']}.TW").history(period="1d")
            )
            return float(df["Close"].iloc[-1]) if not df.empty else None
        except Exception:
            return None

    prices = await asyncio.gather(*[_fetch(h) for h in holdings])
    # enrich holdings with prices[i]
    ...
```

**Latency reduction**:

| Holdings | Current (sequential) | After (concurrent) |
|---------|---------------------|-------------------|
| 1 | 1–3s | 1–3s (no change) |
| 5 | 5–15s | 1–4s |
| 20 | 20–60s | 2–6s |

**Note**: `portfolio_manager_node` is already called after the two parallel Sonnet nodes (`chip_analyst`, `tech_analyst`). Those two nodes run concurrently by LangGraph's graph topology — no changes needed there.

### 2. `save_to_db_node` and `send_notification_node` in parallel

These two nodes currently run sequentially (`save_to_db → send_notification`). They are independent — the notification does not depend on the DB row_id. They could run as a parallel fan-out:

```python
# investment_workflow.py — build_graph()
graph.add_edge("format_agent", "save_to_db")
graph.add_edge("format_agent", "send_notification")   # parallel fan-out
graph.add_edge("save_to_db", END)
graph.add_edge("send_notification", END)
```

**Saving**: ~20–50ms (DB write latency hidden behind LINE API call). Minor but free.

---

## Context Compression

### 1. Reduce Opus `max_tokens` from 16000 to 4096

```python
# market_analyst_agents.py:122–129 — CURRENT
def _llm_opus() -> ChatAnthropic:
    return ChatAnthropic(
        model=_MODEL_OPUS,
        max_tokens=16000,            # ← 8× actual output (~2000 tokens)
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},  # ← ignored silently
    )

# FIX
def _llm_opus() -> ChatAnthropic:
    return ChatAnthropic(
        model=_MODEL_OPUS,
        max_tokens=4096,
        thinking={"type": "adaptive", "budget_tokens": 2048},
    )
```

**Impact of `max_tokens=4096`**: The actual brief output is ~600–900 tokens text. 4096 provides 3× headroom. The hard ceiling prevents a runaway thinking-heavy response from consuming 10,000+ tokens.

**Impact of `budget_tokens=2048`**: Caps thinking to 2,048 tokens (~$0.051 max thinking cost per run). For complex market days that previously consumed 5,000+ thinking tokens (~$0.125), this reduces Opus thinking cost by 60%.

**Output quality tradeoff**: Capping at 2,048 thinking tokens may reduce reasoning depth on highly ambiguous signal days. The brief content has been qualitatively good at this scale in testing.

### 2. Remove `output_config={"effort": "high"}`

This parameter is not in `ChatAnthropic`'s documented parameter set. It is silently ignored. It should be removed to keep the code clean and avoid confusion.

### 3. Compress `final_brief` before `portfolio_manager_node`

Currently `portfolio_manager_node` receives the full 1,500–2,000 character Opus brief + portfolio P&L lines. The portfolio advice only needs the market direction summary (盤勢定調 + 操作策略 sections), not the full 4-section brief.

```python
# Extract only the relevant sections before sending to portfolio_manager
def _extract_strategy_section(brief: str) -> str:
    lines = brief.split("\n")
    relevant = []
    capture = False
    for line in lines:
        if "【盤勢定調】" in line or "【操作策略】" in line:
            capture = True
        elif "【關鍵防守點】" in line:
            capture = False
        if capture:
            relevant.append(line)
    return "\n".join(relevant) or brief  # fallback to full if parsing fails
```

**Estimated saving**: ~300–400 input tokens to `portfolio_manager_node` × $3/M Sonnet = ~$0.001/run. Small but reduces prompt token duplication.

---

## Retrieval Optimization

### 1. Add `trade_date` index to `daily_briefs`

`get_brief(trade_date)` runs `WHERE trade_date = :d` and `get_recent_accuracy()` runs `ORDER BY trade_date DESC`. Neither has an explicit index on `trade_date` in the `CREATE TABLE` DDL.

```sql
ALTER TABLE daily_briefs ADD INDEX idx_trade_date (trade_date);
```

At current scale (250 rows/year) this is a latency non-issue, but should be added before multi-year accumulation.

### 2. Add `UNIQUE KEY` to `daily_briefs.trade_date`

```sql
ALTER TABLE daily_briefs ADD UNIQUE KEY uq_trade_date (trade_date);
```

Prevents duplicate rows from double-runs. Eliminates the correctness risk in `get_recent_accuracy()` where duplicate dates inflate the accuracy count.

### 3. Historical context injection for `chief_strategist_node` (high effort, high value)

The single highest-value retrieval optimization is giving `chief_strategist_node` access to recent prediction history. This is addressed in detail in `hybrid_memory_architecture_roadmap.md`.

**Quick version** (no vector search): inject the last 5 session outcomes via SQL before the Opus call:

```python
from database_tools import get_recent_accuracy

def chief_strategist_node(state: WorkflowState) -> dict:
    history = get_recent_accuracy(5)
    history_text = "\n".join(
        f"- {r['trade_date']}: predicted {r['gap_direction']} {r['predicted_gap_pct']}%, "
        f"actual {r['actual_gap_pct']}%"
        for r in history if r["actual_gap_pct"] is not None
    )
    user_content = (
        f"近期預測記錄：\n{history_text}\n\n"
        f"籌碼面報告：\n{state['chip_report']}\n\n"
        f"技術面報告：\n{state['tech_report']}"
    )
    ...
```

**Token cost of injection**: ~100–200 additional input tokens × $5/M Opus = ~$0.001/run. The prediction quality improvement far outweighs this cost.
