# Token Cost Analysis
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## Pricing Reference

```python
# market_analyst_agents.py:23–27
_PRICING = {  # USD per 1M tokens
    "claude-haiku-4-5-20251001": {"input": 1.00, "output":  5.00},
    "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00},
    "claude-opus-4-7":           {"input": 5.00, "output": 25.00},
}
```

Cost formula: `(input_tok × price_in + output_tok × price_out) / 1_000_000`

---

## Token Tracking Architecture

### Tracking path

```
LLM node invoke()
    └─► response.usage_metadata
            ├─ input_tokens  (int)
            └─ output_tokens (int)
                  │
                  ▼
         _record_usage(agent_name, model, response, latency_ms)
                  │
                  ▼
         _calc_cost(model, in_tok, out_tok)
                  │
                  ▼
         log_cost(agent, model, in_tok, out_tok, cost_usd, latency_ms)
                  │
                  ▼
         INSERT INTO cost_logs (TiDB)
```

**Tracking is implemented in: `market_analyst_agents.py:148–158` + `database_tools.py:114–133`**

### Tracking gaps

```
backtest_agent.py:evaluate_node
    llm = ChatAnthropic(model=MODEL_ID, ...)  ← direct instantiation
    response = llm.invoke(...)
    # _record_usage() is NEVER called
    # cost_logs receives NOTHING

agent_orchestrator.py:think_node
    llm = ChatAnthropic(model=MODEL_ID, ...)  ← direct instantiation
    response = llm.invoke(messages)
    # _record_usage() is NEVER called
    # cost_logs receives NOTHING
```

---

## Workflow Token Distribution (Investment Workflow)

Token estimates based on current single-holding portfolio and typical snapshot sizes.

### Per-Node Breakdown

| Node | Model | Input Tokens | Output Tokens | Input Cost | Output Cost | Node Total |
|------|-------|-------------|--------------|-----------|------------|-----------|
| data_collector | Haiku | ~1,260–2,560 | ~100–150 | $0.0013–$0.0026 | $0.0005–$0.0008 | **$0.0018–$0.0033** |
| chip_analyst | Sonnet | ~440–540 | ~150–250 | $0.0013–$0.0016 | $0.0023–$0.0038 | **$0.0036–$0.0054** |
| tech_analyst | Sonnet | ~470–570 | ~200–300 | $0.0014–$0.0017 | $0.0030–$0.0045 | **$0.0044–$0.0062** |
| chief_strategist | Opus | ~925–1,325 | ~600–900 text + **thinking** | $0.0046–$0.0066 | $0.0150–$0.0225 + thinking | **$0.024–$0.055+** |
| portfolio_manager | Sonnet | ~710–1,110 | ~300–600 | $0.0021–$0.0033 | $0.0045–$0.0090 | **$0.0066–$0.0123** |
| format_agent | Haiku | ~1,090–1,790 | ~500–800 | $0.0011–$0.0018 | $0.0025–$0.0040 | **$0.0036–$0.0058** |

### Per-Run Total

| Scenario | Total Cost | Notes |
|---------|-----------|-------|
| Minimal thinking | ~$0.044–$0.055 | Opus produces ~200 thinking tokens |
| Typical run | ~$0.08–$0.13 | Opus adaptive thinking ~1,000–3,000 tokens |
| Complex market day | ~$0.15–$0.25+ | Opus thinking could hit 5,000+ tokens |

**Monthly cost** (20 trading days): $1.60–$5.00/month

**Opus share of total**: 55–75% depending on thinking token count.

---

## Chief Strategist Token Deep Dive

The Opus node is the only node with extended thinking enabled. Its token profile has two layers:

### Text output tokens

```
System prompt (_CHIEF_SYSTEM): ~125 tokens
User input (chip_report + tech_report): ~800–1,200 tokens
─────────────────────────────────────────
Input total:                    ~925–1,325 tokens
Output text:                    ~600–900 tokens  (4-section brief)
```

### Thinking tokens (hidden billing)

With `thinking={"type": "adaptive"}` and `max_tokens=16000`:

```
Budget consumed (estimated):
  Simple market (clear direction):      ~500–1,500 thinking tokens
  Moderate market (mixed signals):    ~1,500–4,000 thinking tokens
  Complex market (contradictory data): ~4,000–10,000 thinking tokens

Billing rate: $25.00 per 1M output tokens (same as text output)

Cost at 4,000 thinking tokens:  4,000 × $25/M = $0.10
Cost at 10,000 thinking tokens: 10,000 × $25/M = $0.25
```

**The thinking tokens are the primary cost driver on volatile days.** There is no `budget_tokens` ceiling to prevent this.

### max_tokens=16000 reservation

Anthropic reserves `max_tokens` slots in the response. For Opus at current output sizes:

| Actual output | Reserved slots | Overreservation ratio |
|-------------|--------------|---------------------|
| ~700 text + ~2,000 thinking | 16,000 | 6× |
| ~700 text + ~5,000 thinking | 16,000 | 2.7× |
| ~700 text + ~10,000 thinking | 16,000 | 1.4× |

---

## Agent Token Usage (All Scripts)

### Investment Workflow (6 nodes, tracked)

Already covered above. All 6 nodes call `_record_usage()`.

### Backtest Agent (1 node, untracked)

**`backtest_agent.py:evaluate_node`**:

| Input component | Est. tokens |
|----------------|------------|
| `_EVAL_SYSTEM` prompt | ~100 tokens |
| trade_date + brief_record | ~800–1,500 tokens |
| actual_data JSON | ~50–100 tokens |
| **Total input** | **~950–1,700 tokens** |
| Output (accuracy report) | ~400–600 tokens |

**Estimated cost per backtest run: $0.003–$0.005 (Haiku)**
**Monthly cost (20 runs): ~$0.06–$0.10 — never appears in cost_logs**

### Maintenance Orchestrator (1 node, untracked)

**`agent_orchestrator.py:think_node`**:

| Input component | Est. tokens |
|----------------|------------|
| `SYSTEM_PROMPT` | ~100 tokens |
| `TASK` | ~100 tokens |
| system_stats JSON | ~200–400 tokens |
| **Total input** | **~400–600 tokens** |
| Output (analysis + STATUS) | ~200–400 tokens |

**Estimated cost per orchestrator run: $0.0014–$0.0026 (Haiku)**
**Monthly cost (20 runs): ~$0.03–$0.05 — never appears in cost_logs**

---

## Duplicated Token Usage

Two duplication patterns exist in the current workflow.

### Duplication 1: final_brief sent twice

`final_brief` (~1,500–2,000 chars, ~600–900 tokens) is:
1. **Consumed by `portfolio_manager_node`**: included in full in `user_content` alongside portfolio P&L lines
2. **Consumed again by `format_agent_node`**: included in full in `user_content`

This means the full Opus brief is re-tokenized and re-sent to two downstream models.

```
chief_strategist output:  ~600–900 tokens of text
                               │
                ┌──────────────┴──────────────┐
                ▼                             ▼
  portfolio_manager input    format_agent input
  (+600–900 duplicated)      (+600–900 duplicated)
```

**Duplicated tokens per run**: ~1,200–1,800 input tokens across two nodes
**Duplication cost per run**: ~$0.004–$0.007 (Sonnet + Haiku rates)

### Duplication 2: portfolio_advice echoed into format_agent

`portfolio_advice` (~400–800 chars, ~150–300 tokens) is:
1. **Produced by `portfolio_manager_node`**
2. **Passed verbatim into `format_agent_node`** as part of user_content

This is architecturally correct (format_agent must know the advice to format it), but the advice text was derived from `final_brief`, meaning the format_agent receives content that originated from Opus but was transformed through Sonnet — a two-hop token echo.

---

## Cost Log Coverage Matrix

| Script | Node | Model | Cost Logged? | TiDB agent_name |
|--------|------|-------|-------------|-----------------|
| `investment_workflow.py` | data_collector | Haiku | ✅ | `data_collector` |
| `investment_workflow.py` | chip_analyst | Sonnet | ✅ | `chip_analyst` |
| `investment_workflow.py` | tech_analyst | Sonnet | ✅ | `tech_analyst` |
| `investment_workflow.py` | chief_strategist | Opus | ✅ | `chief_strategist` |
| `investment_workflow.py` | portfolio_manager | Sonnet | ✅ | `portfolio_manager` |
| `investment_workflow.py` | format_agent | Haiku | ✅ | `format_agent` |
| `backtest_agent.py` | evaluate | Haiku | ❌ | — |
| `agent_orchestrator.py` | think | Haiku | ❌ | — |

**Tracked cost coverage: 6/8 nodes (75%)**
**Untracked monthly spend estimate: ~$0.09–$0.15**
