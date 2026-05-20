# Cost Risk Report
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Executive Summary

Three cost risks exist in the current architecture, two of which are active today:

| Risk | Severity | Status | Financial Exposure |
|------|---------|--------|-------------------|
| Unbounded Opus thinking tokens | 🔴 HIGH | **Active** | $0.10–$0.25+ per run on volatile market days |
| Untracked Haiku spend (backtest + orchestrator) | 🟡 MEDIUM | **Active** | ~$0.09–$0.15/month invisible to dashboard |
| data_collector fallback injects raw snapshot into Sonnet | 🟠 MEDIUM | **Latent** | ~5× token spike per affected run |

None of these risks will cause runaway costs at single-user scale (~$2.60/month nominal). However, the first risk has already produced runs in the $0.13 range, and could reach $0.25+ on days with highly contradictory market signals.

---

## Risk 1: Runaway Opus Thinking Tokens

### Root cause

```python
# market_analyst_agents.py:122–129
def _llm_opus() -> ChatAnthropic:
    return ChatAnthropic(
        model=_MODEL_OPUS,
        max_tokens=16000,                  # ← ceiling for BOTH text + thinking tokens
        thinking={"type": "adaptive"},      # ← no budget_tokens cap
        output_config={"effort": "high"},   # ← ignored; silently discarded
    )
```

`thinking={"type": "adaptive"}` means Claude decides at runtime how many thinking tokens to use, up to `max_tokens`. With `max_tokens=16000`, up to ~15,300 thinking tokens could be generated before the 900 text tokens are produced (the combined ceiling is 16,000).

### Billing mechanism

Thinking tokens are billed as **output tokens** at $25.00/M (Opus output rate):

| Thinking token count | Thinking cost | Text output cost (~750 tokens) | Run total |
|---------------------|-------------|-------------------------------|---------|
| 500 (simple market) | $0.0125 | $0.01875 | ~$0.031 |
| 2,000 (typical) | $0.0500 | $0.01875 | ~$0.069 |
| 5,000 (complex signals) | $0.1250 | $0.01875 | ~$0.144 |
| 12,000 (worst case) | $0.3000 | $0.01875 | ~$0.319 |

**At worst case (12,000 thinking tokens × 20 trading days): $6.38/month for Opus alone**

### Detection gap

`_record_usage()` reads `response.usage_metadata.get("output_tokens")`. In Anthropic's API, thinking tokens are included in the `output_tokens` count when using `ChatAnthropic`. This means the `cost_logs.estimated_cost_usd` value correctly captures thinking token costs — but there is no separate `thinking_tokens` column.

**You cannot distinguish thinking cost from text output cost in the current dashboard.** If thinking tokens spike, the `chief_strategist` row in cost_logs shows higher total cost but no breakdown.

### Fix

```python
def _llm_opus() -> ChatAnthropic:
    return ChatAnthropic(
        model=_MODEL_OPUS,
        max_tokens=4096,
        thinking={"type": "adaptive", "budget_tokens": 2048},
    )
```

`budget_tokens=2048` caps thinking at $0.051 per run regardless of market complexity. Maximum Opus cost per run with this fix: `(1,325 × $5/M) + (2,048 + 900) × $25/M = $0.0066 + $0.0737 = ~$0.080`. Hard ceiling.

---

## Risk 2: Untracked Haiku Spend

### Root cause

`backtest_agent.py` and `agent_orchestrator.py` instantiate `ChatAnthropic` directly and never call `_record_usage()`:

```python
# backtest_agent.py:128–133 — cost invisible to dashboard
llm = ChatAnthropic(
    model=MODEL_ID,           # claude-haiku-4-5-20251001
    api_key=os.getenv("ANTHROPIC_API_KEY"),
    max_tokens=1024,
)
response = llm.invoke([...])
# no _record_usage() call
```

```python
# agent_orchestrator.py:95–100 — cost invisible to dashboard
llm = ChatAnthropic(
    model=MODEL_ID,           # claude-haiku-4-5-20251001
    api_key=os.getenv("ANTHROPIC_API_KEY"),
    max_tokens=512,
)
response = llm.invoke(messages)
# no _record_usage() call
```

### Financial exposure

| Script | Runs/month | Cost/run | Monthly cost | Dashboard visibility |
|--------|-----------|---------|-------------|---------------------|
| `backtest_agent.py` | ~20 | $0.003–$0.005 | ~$0.06–$0.10 | ❌ None |
| `agent_orchestrator.py` | ~20 | $0.0014–$0.0026 | ~$0.03–$0.05 | ❌ None |
| **Total untracked** | — | — | **~$0.09–$0.15** | — |

**Secondary risk**: If these scripts are ever called in a loop (automated backtest series, repeated orchestrator health checks), the cost accumulates without any visibility until the Anthropic billing alert fires.

### Fix

Both scripts should use the shared `_record_usage()` function:

```python
# backtest_agent.py:evaluate_node — add after llm.invoke():
import time
from market_analyst_agents import _record_usage

start = time.monotonic()
response = llm.invoke([...])
latency_ms = int((time.monotonic() - start) * 1000)
_record_usage("backtest_evaluate", MODEL_ID, response, latency_ms)
```

---

## Risk 3: data_collector Fallback Token Spike

### Root cause

When `data_collector_node` fails to parse the Haiku JSON response, it returns `raw_market_data = {}`. Downstream nodes detect the empty dict and fall back to the full raw snapshot:

```python
# market_analyst_agents.py:196–197 — chip_analyst_node fallback
if not chip_data:
    chip_data = state["snapshot"]["tools"]["get_tw_future_chips"]["data"]
```

```python
# market_analyst_agents.py:221–222 — tech_analyst_node fallback
if not us_data:
    us_data = state["snapshot"]["tools"]["get_us_market_summary"]["data"]["markets"]
```

The `snapshot["tools"]` data is the raw MCP output: potentially 3–8 KB of JSON per tool. In the fallback path, `chip_analyst_node` and `tech_analyst_node` both receive this uncompressed data instead of the 200-byte compact dict.

### Token comparison

| Path | chip_analyst input | tech_analyst input |
|------|------------------|------------------|
| Normal (via data_collector) | ~300–400 tokens | ~300–400 tokens |
| Fallback (raw snapshot) | ~1,000–2,500 tokens | ~800–2,000 tokens |

**Cost spike per affected run**: ~1,400–4,200 additional input tokens × $3/M (Sonnet) = **~$0.004–$0.013 extra per run**

More importantly, injecting raw TAIFEX HTML scrape data into a Sonnet prompt that was designed for 3 clean numeric fields may produce hallucinated or unreliable chip analysis.

### Trigger probability

The fallback is triggered when:
1. Haiku produces a response that starts with triple-backtick markdown fencing (handled, but fragile)
2. The MCP snapshot structure changes (e.g., new field names from TAIFEX scraper update)
3. Haiku hallucinates non-JSON text in a degraded API state

### Fix

Add explicit error handling before fallback injection:

```python
# chip_analyst_node — replace implicit fallback with explicit error state
if not chip_data:
    logger.error("[ChipAnalyst] data_collector returned empty — raw snapshot fallback active")
    # Option A: Abort with clear error
    return {"chip_report": '{"error": "data_collector_failed", "sentiment": "unknown"}'}
    # Option B: Use raw but log the token spike
    chip_data = state["snapshot"]["tools"]["get_tw_future_chips"]["data"]
    logger.warning(f"[ChipAnalyst] Fallback: sending {len(json.dumps(chip_data))} byte raw data to Sonnet")
```

---

## Risk 4: Expensive Prompt Patterns

### Pattern 1: Full brief duplication across nodes

The 1,500–2,000 character Opus brief (`final_brief`) is sent to both `portfolio_manager_node` and `format_agent_node` in full. See `optimization_opportunities.md` for the compression fix.

**Cost per run**: ~$0.004–$0.007 wasted on token duplication. Low severity.

### Pattern 2: `_llm()` factory called inside node, not cached

```python
# chip_analyst_node:201
response = _llm(_MODEL_SONNET).invoke([...])
```

A new `ChatAnthropic` object is created for every node invocation. This does not affect token cost but adds 2–5ms of Python object initialization overhead per call and prevents the reuse of any connection-level state.

### Pattern 3: No retry on transient API failure

A single 429 (rate limit) or 500 (API error) response causes the entire workflow to fail, including the already-executed Opus call (~$0.048–$0.13 that cannot be recovered without a checkpointer). See `memory_scalability_report.md` Risk 5 for the `SqliteSaver` fix.

---

## Cost Monitoring Gap

The Streamlit dashboard's cost tab calls `get_cost_summary(30)` which aggregates `cost_logs` by `agent_name`. This shows per-node token and cost totals but has three visibility gaps:

1. **No thinking token breakdown**: `estimated_cost_usd` includes thinking costs in the `chief_strategist` row but cannot separate them from text output costs
2. **No cross-script aggregation**: backtest and orchestrator costs are absent
3. **No per-day Opus cost alert**: There is no threshold-based notification when a single Opus run exceeds a cost ceiling (e.g., $0.15)

**Recommended addition to `cost_logs` table**:
```sql
ALTER TABLE cost_logs ADD COLUMN thinking_tokens INT DEFAULT 0;
```

And in `_record_usage()`:
```python
thinking_tok = getattr(response.usage_metadata, "thinking_tokens", 0)
```
