# Agent Evaluation Framework — Design Document
**AI Agent Studio | 2026-05-16**

---

## Overview

Rule-based, no-external-platform evaluation framework for the Taiwan Stock Futures 8-node LangGraph workflow. Evaluates per-agent output quality every trading day and persists structured results to TiDB for trending and dashboard visibility.

---

## Architecture

```
                    ┌─────────────────────────────────┐
                    │   evaluation_runner.py (CLI)    │
                    └──────────────┬──────────────────┘
                                   │ reads
              ┌────────────────────┼────────────────────┐
              ▼                    ▼                    ▼
        llm_traces           daily_briefs         session_episodes
    (per-agent output)    (brief_text, gap_dir) (market context)
              │                    │                    │
              └────────────────────┼────────────────────┘
                                   │
                                   ▼
                    ┌──────────────────────────────┐
                    │   evaluation_metrics.py      │
                    │   (pure rule-based, no LLM)  │
                    │                              │
                    │  eval_data_collector()       │
                    │  eval_chip_analyst()         │
                    │  eval_tech_analyst()         │
                    │  eval_chief_strategist()     │
                    │  eval_format_agent()         │
                    │  compute_brief_quality_score()│
                    └──────────────┬───────────────┘
                                   │ writes
              ┌────────────────────┼────────────────────┐
              ▼                    ▼                    ▼
         eval_runs           eval_results          dashboard
     (one per trade_date)   (one per agent)      (📝 評估 tab)
```

---

## Data Sources Per Agent

| Agent | Primary Source | Fallback |
|-------|---------------|---------|
| data_collector | session_episodes columns | empty dict (score=0) |
| chip_analyst | llm_traces.raw_response | "" (score=0) |
| tech_analyst | llm_traces.raw_response | "" (score=0) |
| chief_strategist | llm_traces.raw_response | daily_briefs.brief_text |
| format_agent | llm_traces.raw_response | "" (score=0) |
| backtest_evaluator | LLM report (backtest_agent.py) | — |

---

## DB Schema

### eval_runs
One row per trade_date (UNIQUE KEY uq_er_trade_date).

| Column | Type | Description |
|--------|------|-------------|
| id | BIGINT PK | Auto-increment |
| trade_date | DATE UNIQUE | The trading day evaluated |
| run_id_ref | VARCHAR(36) | FK to workflow_runs.id (nullable) |
| triggered_by | VARCHAR(30) | 'manual' / 'cron' / 'backtest' |
| status | VARCHAR(20) | 'success' / 'failed' |
| brief_quality_score | DECIMAL(5,2) | 0–100 weighted composite |
| direction_correct | TINYINT | 1=correct 0=wrong NULL=no actuals |
| predicted_direction | VARCHAR(10) | 'up' / 'flat' / 'down' |
| actual_direction | VARCHAR(10) | Derived from market_actuals |
| completed_at | TIMESTAMP | When evaluation finished |
| created_at | TIMESTAMP | Row creation time |

### eval_results
One row per agent per eval_run.

| Column | Type | Description |
|--------|------|-------------|
| id | BIGINT PK | Auto-increment |
| eval_run_id | BIGINT | FK to eval_runs.id |
| trade_date | DATE | Denormalized for query convenience |
| agent_name | VARCHAR(50) | Agent identifier |
| quality_score | DECIMAL(5,2) | 0–100 |
| schema_valid | TINYINT | 1=pass 0=fail NULL=unchecked |
| missing_fields | JSON | list[str] of absent fields |
| hallucination_flags | JSON | list[str] of detected anomalies |
| extra_metrics | JSON | Agent-specific supplementary data |
| created_at | TIMESTAMP | Row creation time |

---

## Scoring Weights (Brief Quality Composite)

| Agent | Weight | Rationale |
|-------|--------|-----------|
| data_collector | 10% | Foundation; low if missing but rarely fails |
| chip_analyst | 20% | Chip analysis; JSON parse issues common |
| tech_analyst | 25% | Highest operational risk; gap_direction NULL breaks daily_briefs |
| chief_strategist | 35% | Final product quality; most visible to end user |
| format_agent | 10% | Formatting; rarely fails if upstream is good |

---

## Trigger Modes

| Mode | triggered_by | How |
|------|-------------|-----|
| Daily cron | 'cron' | Add to daily_run.sh after investment_workflow |
| Backtest | 'backtest' | Automatic via save_accuracy_node in backtest_agent.py |
| Manual | 'manual' | `uv run python evaluation_runner.py --date YYYY-MM-DD` |

---

## Direction Accuracy Thresholds

Mirrors `_calc_accuracy_kpi()` in dashboard.py to ensure consistency:

| actual_gap_pct | Assigned direction |
|---------------|-------------------|
| > +0.3% | "up" |
| < -0.3% | "down" |
| [-0.3%, +0.3%] | "flat" |

---

## Idempotency

- `eval_runs`: `ON DUPLICATE KEY UPDATE` on `trade_date` — safe to re-run same day
- `eval_results`: New rows inserted on each run — provides version history; add `UNIQUE KEY (eval_run_id, agent_name)` later if strict idempotency needed
- `backtest_agent.py` persistence: `create_eval_run(triggered_by='backtest')` also uses ON DUPLICATE KEY UPDATE

---

## Phase 2 Roadmap (Not Implemented)

| Enhancement | Value | Effort |
|-------------|-------|--------|
| LLM-based content quality check (no number hallucination) | High | 2h |
| Cross-agent consistency check (chief_strategist references tech_analyst numbers) | Medium | 3h |
| Trend alert: if avg quality_score drops >20% week-over-week, fire A-010 | High | 1h |
| session_episodes.direction_correct backfill from eval_results | Medium | 1h |
| evaluation_runner added to cron daily_run.sh | Low | 15 min |
