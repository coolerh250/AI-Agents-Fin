# Model Usage Report
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Model Inventory

Three Claude model tiers are used across four scripts. Every model assignment is **hardcoded** — there is no runtime routing decision.

```python
# market_analyst_agents.py:19–21
_MODEL_HAIKU  = "claude-haiku-4-5-20251001"
_MODEL_SONNET = "claude-sonnet-4-6"
_MODEL_OPUS   = "claude-opus-4-7"
```

---

## Claude Haiku 4.5 Usage

**Invocation count per workflow run: 2 (investment) + 1 (backtest) + 1 (orchestrator) = 4 total**

| Script | Node | Factory | max_tokens | Purpose |
|--------|------|---------|-----------|---------|
| `market_analyst_agents.py` | `data_collector_node` | `_llm(_MODEL_HAIKU)` | 1024 (default) | Extract 8 numeric fields from raw MCP snapshot |
| `market_analyst_agents.py` | `format_agent_node` | `_llm(_MODEL_HAIKU, max_tokens=2048)` | 2048 | Format Opus brief as LINE message ≤2000 chars |
| `backtest_agent.py` | `evaluate_node` | Direct `ChatAnthropic(model=MODEL_ID, max_tokens=1024)` | 1024 | Compare prediction vs actual TAIEX data |
| `agent_orchestrator.py` | `think_node` | Direct `ChatAnthropic(model=MODEL_ID, max_tokens=512)` | 512 | Interpret system health stats → STATUS label |

**Why Haiku**: deterministic extraction (data_collector), fixed-format output (format_agent), simple binary classification (orchestrator STATUS), structured scoring (backtest evaluate).

---

## Claude Sonnet 4.6 Usage

**Invocation count per workflow run: 3**

| Script | Node | Factory | max_tokens | Purpose |
|--------|------|---------|-----------|---------|
| `market_analyst_agents.py` | `chip_analyst_node` | `_llm(_MODEL_SONNET)` | 1024 (default) | Interpret 3-field OI data → sentiment + divergence JSON |
| `market_analyst_agents.py` | `tech_analyst_node` | `_llm(_MODEL_SONNET)` | 1024 (default) | Compute weighted gap prediction from 4 US market inputs |
| `market_analyst_agents.py` | `portfolio_manager_node` | `_llm(_MODEL_SONNET, max_tokens=1024)` | 1024 | Generate per-holding buy/sell/hold advice |

**Why Sonnet**: Single-domain domain analysis requiring judgment (not just extraction), multi-condition decision trees (stop-loss logic), Chinese-language reasoning.

---

## Claude Opus 4.7 Usage

**Invocation count per workflow run: 1**

| Script | Node | Factory | max_tokens | Purpose |
|--------|------|---------|-----------|---------|
| `market_analyst_agents.py` | `chief_strategist_node` | `_llm_opus()` | **16000** | Synthesize chip + tech reports into 4-section investment brief |

**`_llm_opus()` configuration** (`market_analyst_agents.py:122–129`):

```python
def _llm_opus() -> ChatAnthropic:
    return ChatAnthropic(
        model=_MODEL_OPUS,
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        max_tokens=16000,                   # ← 8× actual output size (~2000 tokens)
        thinking={"type": "adaptive"},       # ← no budget_tokens cap
        output_config={"effort": "high"},    # ← invalid param, silently ignored
    )
```

**Why Opus**: Multi-source synthesis requiring strategic reasoning across two independent analysis streams. Chief strategist output directly determines the trading decision narrative.

**Hidden costs**:
1. `max_tokens=16000` reserves the maximum billing slot regardless of actual output
2. `thinking={"type": "adaptive"}` enables extended thinking with no token budget ceiling — thinking tokens are billed at Opus output rates ($25/M)
3. `output_config={"effort": "high"}` is not a recognized `ChatAnthropic` parameter and has no effect — it is silently discarded

---

## Model Routing Logic

There is no runtime routing. The routing is a static design decision encoded at node definition time:

```
Complexity tier → Model assignment:
  Extraction / classification    →  Haiku 4.5   (fast, cheap, deterministic)
  Single-domain analysis         →  Sonnet 4.6  (balanced reasoning + cost)
  Multi-source synthesis         →  Opus 4.7    (highest reasoning depth)
```

The routing never changes based on:
- Market volatility or data complexity
- Time of day or cost budget
- Previous node output quality
- API error rates or latency

---

## Model Fallback

**There is no fallback mechanism anywhere in the codebase.**

If an API call fails:
1. The exception propagates up through the node function
2. LangGraph catches it and terminates the graph
3. No retry, no cheaper model substitution, no graceful degradation

The only exception-swallowing is in `_record_usage()`:

```python
# market_analyst_agents.py:148–158
def _record_usage(agent_name, model, response, latency_ms):
    try:
        ...
        log_cost(...)
    except Exception as exc:
        logger.warning(f"[{agent_name}] cost logging failed: {exc}")
```

This prevents a DB connection failure from killing the workflow, but it does NOT protect against LLM API failures — those still crash the workflow.

---

## Model Usage by Agent Type Summary

| Agent/Script | Models Used | LLM Nodes | Cost Tracked? |
|-------------|------------|----------|--------------|
| `investment_workflow.py` | Haiku × 2, Sonnet × 3, Opus × 1 | 6 | ✅ All 6 via `_record_usage()` |
| `backtest_agent.py` | Haiku × 1 | 1 | ❌ Direct instantiation, no logging |
| `agent_orchestrator.py` | Haiku × 1 | 1 | ❌ Direct instantiation, no logging |

**Total Claude API calls per full daily cycle** (all scripts): **8 LLM invocations**

Of these, **6 are cost-tracked** and **2 are invisible** to the dashboard.
