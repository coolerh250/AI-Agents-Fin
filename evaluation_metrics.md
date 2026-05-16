# Agent Evaluation Metrics — Reference Guide
**AI Agent Studio | 2026-05-16**

All checks are rule-based (no LLM). Each agent returns a score 0–100.

---

## data_collector (Haiku)

**What it checks**: Did the agent successfully extract all required market fields from the snapshot?

| Check | Points | Pass Condition |
|-------|--------|----------------|
| foreign_oi_net present & numeric | 10 | Not None, is int/float |
| trust_oi_net present & numeric | 10 | Not None, is int/float |
| dealer_oi_net present & numeric | 10 | Not None, is int/float |
| djia_chg_pct present & numeric | 10 | Not None, is int/float |
| ndx_chg_pct present & numeric | 10 | Not None, is int/float |
| sox_chg_pct present & numeric | 10 | Not None, is int/float |
| tsm_adr_chg_pct present & numeric | 10 | Not None, is int/float |
| data_ok flag present | 10 | Field exists in session_episodes |
| All 7 keys present (bonus) | 20 | Zero missing fields |

**schema_valid**: True if all 7 required keys have numeric values.

---

## chip_analyst (Sonnet)

**What it checks**: Is the output JSON-parseable? Does it contain the required `divergence_signal` key and Chinese section keywords?

| Check | Points | Pass Condition |
|-------|--------|----------------|
| Output length 100–4000 chars | 20 | len(text) in range |
| Contains 外資, 投信, 自營商 | ~10 | Each keyword ~3.3 pts |
| JSON parseable | 40 | json.loads() succeeds |
| divergence_signal key exists | 30 | Key present in parsed JSON |

**schema_valid**: JSON parseable AND divergence_signal key present.

**Hallucination flags**:
- `divergence_conflict`: divergence_signal=True but foreign_oi_net > 0 (OI is actually bullish, no divergence)

---

## tech_analyst (Sonnet)

**What it checks**: Is the output valid JSON with correct gap_direction and numeric estimated_gap_pct?

> **Critical**: `save_to_db_node` parses tech_report as JSON to extract `gap_direction` for `daily_briefs`. If this fails, `gap_direction` is stored as NULL and backtest cannot evaluate direction accuracy.

| Check | Points | Pass Condition |
|-------|--------|----------------|
| JSON parseable | 30 | json.loads() succeeds |
| gap_direction valid | 30 | Value in {"up", "flat", "down"} |
| estimated_gap_pct is numeric | 20 | isinstance(v, (int, float)) |
| abs(estimated_gap_pct) ≤ 5.0% | 20 | Within realistic range for TAIEX |

**schema_valid**: Parseable AND gap_direction valid AND no hallucination flags.

**Hallucination flags**:
- `json_parse_failed`: Cannot parse as JSON at all
- `gap_pct_out_of_range`: abs(estimated_gap_pct) > 5% (TAIEX rarely gaps this far)
- `direction_pct_mismatch`: gap_direction="up" but estimated_gap_pct < 0 (contradiction)

---

## chief_strategist (Opus + Extended Thinking)

**What it checks**: Does the final_brief contain all 4 required Chinese section markers?

| Check | Points | Pass Condition |
|-------|--------|----------------|
| Contains 盤勢定調 | 25 | Substring found |
| Contains 操作策略 | 25 | Substring found |
| Contains 關鍵防守點 | 25 | Substring found |
| Contains 風險提示 | 25 | Substring found |

**schema_valid**: All 4 sections present.

**Hallucination flags**:
- `brief_too_short`: len < 200 (likely truncated or failure message)
- `brief_too_long`: len > 3000 (possible raw data leakage into prompt)

---

## format_agent (Haiku)

**What it checks**: Does the LINE message contain all 4 emoji section markers and fit within LINE's character limit?

| Check | Points | Pass Condition |
|-------|--------|----------------|
| Contains 📊 | 20 | Substring found |
| Contains ⚔️ | 20 | Substring found |
| Contains 🛡️ | 20 | Substring found |
| Contains ⚠️ | 20 | Substring found |
| Total length ≤ 2000 chars | 20 | LINE message size limit |

**schema_valid**: All 4 emojis present AND length ≤ 2000.

**Hallucination flags**:
- `exceeds_line_limit`: len > 2000 (LINE will truncate or reject)

---

## backtest_evaluator (Haiku — persisted by backtest_agent.py)

**What it checks**: Compares the LLM-generated accuracy report against the structured format.

| Field | Source | How Parsed |
|-------|--------|-----------|
| direction_correct | Report text | regex: `方向準確[：:]\s*(是|否)` |
| quality_score | Report text | regex: `綜合評分[：:]\s*(\d+)` |
| actual_direction | market_actuals.actual_gap_pct | Same ±0.3% threshold as dashboard |

**Note**: This is the only agent where the score is LLM-generated (0–100 from the Haiku evaluation). All other agents use purely rule-based scoring.

---

## Brief Quality Composite Score

```
quality_score = (
    data_collector_score    × 0.10 +
    chip_analyst_score      × 0.20 +
    tech_analyst_score      × 0.25 +
    chief_strategist_score  × 0.35 +
    format_agent_score      × 0.10
)
```

A missing agent's score defaults to 0.0 with its full weight applied (not skipped). This penalizes runs where llm_traces data is unavailable.

---

## Interpreting Scores

| Score Range | Interpretation |
|-------------|----------------|
| 90–100 | Excellent — all agents output correct schemas |
| 70–89 | Good — minor issues (e.g., chip_analyst missing divergence_signal) |
| 50–69 | Warning — at least one agent has schema issues; check missing_fields |
| 0–49 | Critical — tech_analyst likely returned non-JSON (gap_direction=NULL in DB) |

---

## Common Failure Patterns

| Pattern | Score Impact | Root Cause |
|---------|-------------|-----------|
| tech_analyst returns explanation instead of JSON | −75 pts | Prompt not followed; LLM context too long |
| chief_strategist missing 關鍵防守點 | −25 pts | Token truncation at max_tokens limit |
| format_agent exceeds 2000 chars | −20 pts | portfolio_advice block too large |
| chip_analyst non-JSON output | −70 pts | LLM prompt ambiguity |
| data_collector missing US market fields | −30–70 pts | yfinance timeout or TWSE fetch failure |
