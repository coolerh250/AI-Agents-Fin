# Cost Analysis
**AI Agent Studio — Taiwan Stock Futures Analysis Team**
_Scan date: 2026-05-15 | Analyst: Principal AI Systems Architect_

---

## 1. Pricing Reference

| Model | Input ($/1M tok) | Output ($/1M tok) |
|-------|-----------------|------------------|
| `claude-haiku-4-5-20251001` | $1.00 | $5.00 |
| `claude-sonnet-4-6` | $3.00 | $15.00 |
| `claude-opus-4-7` | $5.00 | $25.00 |

---

## 2. Per-Node Cost Breakdown (Observed, 2026-05-14)

| Node | Model | Input tok | Output tok | Cost (USD) | % of Total |
|------|-------|-----------|-----------|-----------|-----------|
| data_collector | Haiku 4.5 | ~800 | ~150 | ~$0.0016 | ~1.3% |
| chip_analyst | Sonnet 4.6 | ~500 | ~200 | ~$0.0045 | ~3.5% |
| tech_analyst | Sonnet 4.6 | ~400 | ~200 | ~$0.0042 | ~3.3% |
| **chief_strategist** | **Opus 4.7 + Thinking** | **~1 500** | **~1 800** | **~$0.0525** | **~41%** |
| portfolio_manager | Sonnet 4.6 | ~1 200 | ~300 | ~$0.0081 | ~6.4% |
| format_agent | Haiku 4.5 | ~1 800 | ~500 | ~$0.0043 | ~3.4% |
| **TOTAL (6 LLM nodes)** | | | | **~$0.0752** | 100% |

> Actual run on 2026-05-14: **$0.127 USD** (includes Opus thinking tokens not reflected in standard output count)

**Monthly estimate (20 trading days): ~$2.54 USD**

---

## 3. Chief Strategist: The Cost Spike

`chief_strategist` consumes **~41% of total cost per run** with two compounding factors:

### 3.1 Extended Thinking Overhead

```python
thinking={"type": "adaptive"},
output_config={"effort": "high"},  # ⚠ unrecognised param — likely ignored
max_tokens=16000,
```

With `thinking={"type": "adaptive"}`, Anthropic's API may generate a thinking chain of several thousand tokens **before** the visible output. Thinking tokens are billed at input rates. Observed actual cost ($0.0525) is ~50% higher than what standard token counts would predict — the excess is likely thinking tokens not captured in `usage_metadata["output_tokens"]`.

> **Risk**: On a complex market day, adaptive thinking could generate 8 000–12 000 thinking tokens, pushing `chief_strategist` cost to $0.10–0.15 per run (2x the entire current total).

### 3.2 `max_tokens=16000` Ceiling

The 16 000 token ceiling is 8× the actual output size (~600–1 000 tokens of brief text). This ceiling has no practical benefit but signals to the model that long responses are acceptable, potentially encouraging verbose output.

---

## 4. Token Flow Analysis

### 4.1 Context Amplification Chain

```
market_snapshot.json (~3 KB / ~2 000 raw tokens)
    ↓ data_collector extracts compact JSON (~150 output tokens)
    ↓ chip_analyst: 150 tokens in → 200 tokens out (chip_report)
    ↓ tech_analyst: 150 tokens in → 200 tokens out (tech_report)
    ↓ chief_strategist: (200 + 200 + ~400 system) = ~800 input
                         → ~800 output (final_brief)
    ↓ portfolio_manager: (800 brief + 300 portfolio data + ~300 system) = ~1 400 input
                          → ~300 output (portfolio_advice)
    ↓ format_agent: (800 brief + 300 advice + ~200 system) = ~1 300 input
                     → ~500 output (final_report)
```

**Total cross-node token miles: ~7 200 tokens across 6 LLM calls**

### 4.2 Context Duplication

| Duplication | Where | Wasted Tokens/Run |
|-------------|-------|------------------|
| `final_brief` passed to BOTH `portfolio_manager` AND `format_agent` | Lines 277, 302 | ~800 tokens × 2 = ~1 600 tok |
| `chip_report` + `tech_report` passed as raw JSON strings (not summarised) to `chief_strategist` | All prose formatting preserved | ~100 tokens of JSON structure overhead |
| `market_snapshot.json` full content (~2 000 tokens) passes through `data_collector` which outputs only ~150 tokens | Lines 166, 182 | Unavoidable — this is the compression step; ratio 13:1 |

---

## 5. Duplicated / Unused LLM Paths

### 5.1 format_agent Re-Processes Already-Formatted Data

`send_notification_node` calls `messenger_tools.send_brief(final_report)` where `final_report` is already LINE-formatted by `format_agent`.

`messenger_tools.send_brief()` calls `format_brief()` which **applies formatting rules again** to the input. Since `final_report` already contains `【盤勢定調】`, `【操作策略】`, `【關鍵防守點】` sections (formatted by format_agent), `format_brief()`'s regex will successfully extract and re-wrap them — effectively a no-op transformation but still consumes CPU on string processing.

**Impact:** No LLM cost, but logical inconsistency — `format_agent`'s work is partially redundant.

### 5.2 MCP Tools `save_brief_to_db` and `send_brief_to_user` Are Never Called

These two MCP tools in `finance_mcp_server.py` were built but are bypassed by direct Python imports in `save_to_db_node` and `send_notification_node`. Each MCP server launch via stdio incurs subprocess spawn overhead (~200ms). These tools are dead code that adds maintenance surface with no benefit.

---

## 6. Workflows Not Tracked in cost_logs

| Workflow | Tracked? | Missing Agents |
|----------|---------|----------------|
| `investment_workflow.py` | ✅ 6/6 LLM nodes | — |
| `backtest_agent.py` | ❌ 0/1 LLM nodes | `evaluate` (Haiku) |
| `agent_orchestrator.py` | ❌ 0/1 LLM nodes | `think` (Haiku) |

**Monthly estimate of untracked cost:**
- `backtest_agent.evaluate`: Haiku, ~1 500 input + ~800 output = ~$0.0055/run × 20 days = ~$0.11/month
- `agent_orchestrator.think`: Haiku, ~600 input + ~300 output = ~$0.0021/run (manual only) ≈ negligible

---

## 7. Cost Optimisation Opportunities

| Opportunity | Estimated Saving | Effort | Risk |
|-------------|-----------------|--------|------|
| **Set explicit thinking budget** on Opus (`budget_tokens: 4000` instead of adaptive) | ~30–50% of Opus cost = ~$0.015/run | Low | Possible quality degradation for complex days |
| **Reduce Opus `max_tokens` to 2 048** | Reduces model verbosity ceiling; no direct cost saving but prevents runaway outputs | Low | None |
| **Cache `chip_report`/`tech_report` if identical to prior day** | ~5% of runs (when chips data unchanged) | Medium | Low |
| **Sonnet for chief_strategist on non-critical days** (e.g. Friday before holiday) | ~$0.045/run saved | Medium | Significant quality loss |
| **Add `_record_usage` to `backtest_agent` and `agent_orchestrator`** | Zero cost saving, but enables full visibility | Low | None — pure gain |
| **Deduplicate `final_brief` context** — pass only a 200-token summary to `portfolio_manager` instead of full prose | ~$0.003/run | Medium | Possible advice quality reduction |
| **Prompt caching** via `cache_control` headers | Up to 90% discount on repeated system prompt tokens (billed at 10% after cache hit) | Medium | Requires LangChain prompt-level control |

---

## 8. Cost Scalability

| Scenario | Cost Impact |
|---------|------------|
| Adding 5 more stocks to portfolio | +5 yfinance calls + ~300 extra tokens to portfolio_manager → ~$0.001/run |
| Running workflow twice per day (pre-market + closing) | 2× total cost = ~$0.15/day |
| Scaling to 10 concurrent users/portfolios | Not supported architecturally (single workflow, single portfolio table) |
| Opus pricing increase of 2× | Total cost ~$0.17/run — still <$3.40/month |
| Thinking budget overflow (adaptive, busy day) | Could hit $0.20–0.30/run without budget cap |
