# Evaluation Framework — Change Report
**AI Agent Studio | 2026-05-16**

---

## Summary

5 code files changed + 3 new files + 3 document files. Adds a complete rule-based agent evaluation pipeline with TiDB persistence and dashboard visibility. Zero breaking changes to the daily workflow or output format.

---

## New Tables (migration.sql Steps 7/8)

### `eval_runs`
One row per trading day evaluation session. UNIQUE on `trade_date` — re-running the same day uses ON DUPLICATE KEY UPDATE.

Key columns: `brief_quality_score` (0–100 composite), `direction_correct` (1/0/NULL), `triggered_by` ('manual'/'cron'/'backtest').

### `eval_results`
One row per agent per eval_run. Stores per-agent scores, schema validity, missing field lists, and hallucination flag lists.

Both tables created automatically by `ensure_eval_tables()`, which is now called from `ensure_observability_tables()`.

---

## New Files

### `evaluation_metrics.py`
Pure scoring functions — no LLM calls, no DB access, no exceptions raised.

| Function | What it checks |
|----------|---------------|
| `eval_data_collector(raw_dict)` | 7 numeric market field completeness |
| `eval_chip_analyst(text)` | JSON parseable + divergence_signal + Chinese keywords |
| `eval_tech_analyst(text)` | JSON + valid gap_direction + numeric range |
| `eval_chief_strategist(text)` | 4 Chinese section markers |
| `eval_format_agent(text)` | 4 emoji sections + LINE 2000-char limit |
| `compute_brief_quality_score(scores)` | Weighted composite (10/20/25/35/10%) |

### `evaluation_runner.py`
CLI script: reads DB → evaluates → persists.

```bash
uv run python evaluation_runner.py                  # today
uv run python evaluation_runner.py --date 2026-05-14
```

Data flow:
1. Resolve `run_id_ref` from `workflow_runs` for the trade_date
2. Load per-agent `raw_response` from `llm_traces`
3. Load `session_episodes` for data_collector context
4. Load `daily_briefs` + `market_actuals` for direction check
5. Run all 5 evaluation functions
6. Compute composite score
7. Write to `eval_runs` (upsert) + `eval_results` (insert)

---

## Modified Files

### `database_tools.py` (+9 functions)

| Function | Description |
|----------|-------------|
| `ensure_eval_tables()` | CREATE TABLE IF NOT EXISTS for eval_runs + eval_results |
| `create_eval_run(...)` | Upsert into eval_runs, returns row id |
| `save_eval_result(...)` | Insert one eval_results row (fail-silent) |
| `get_eval_runs(days=30)` | SELECT eval_runs for dashboard |
| `get_eval_results(ids)` | SELECT eval_results by eval_run_id list |
| `get_eval_dashboard_kpis(days=30)` | Aggregated KPI dict for dashboard cards |
| `get_eval_agent_avg_scores(days=20)` | Per-agent AVG(quality_score) for bar chart |
| `get_session_episode(trade_date)` | SELECT one session_episodes row |

Also: `ensure_eval_tables()` added to `ensure_observability_tables()` call chain.

### `backtest_agent.py` (+1 node, +4 graph edges modified)

New node `save_accuracy_node` inserted after `evaluate_node`:
- Parses `方向準確：是/否` and `綜合評分：N` from accuracy_report text via regex
- Calls `create_eval_run(triggered_by='backtest')` + `save_eval_result(agent_name='backtest_evaluator')`
- Completely fail-silent — never crashes the backtest pipeline
- `evaluate_node` itself is unchanged

Graph: `evaluate → save_accuracy → END` (previously `evaluate → END`).

### `dashboard.py` (+1 tab, +4 imports)

New imports: `get_eval_agent_avg_scores`, `get_eval_dashboard_kpis`, `get_eval_results`, `get_eval_runs`.

New 6th tab "📝 評估":
- 4 KPI cards: eval count, avg quality score, direction accuracy %, schema pass rate
- Bar chart: per-agent avg quality score (last 20 evals)
- Table: last 20 eval runs with all per-agent scores + direction correct flag

### `migration.sql` (+Step 7/8)

DDL reference for `eval_runs` and `eval_results`. Tables are actually created via Python `ensure_eval_tables()` which handles IF NOT EXISTS.

---

## Deferred Items

| Item | Reason |
|------|--------|
| Add evaluation_runner.py to daily_run.sh cron | Deployment step; not needed for functionality |
| A-010 alert: quality trend drop >20% week-over-week | Phase 2 |
| LLM-based content consistency check | Phase 2; would add LLM cost |
| Cross-agent number consistency (chief vs tech) | Phase 2 |

---

## Context Budget Impact

evaluation_runner.py adds zero tokens to any LLM prompt — it reads existing outputs and scores them rule-based only. The dashboard tab adds 3 DB queries on page load; all are indexed by `trade_date` and `eval_run_id`.

---

## Files Changed

| File | Type | Lines |
|------|------|-------|
| `evaluation_metrics.py` | New | ~170 |
| `evaluation_runner.py` | New | ~150 |
| `evaluation_framework_design.md` | New | ~100 |
| `evaluation_metrics.md` | New | ~130 |
| `evaluation_change_report.md` | New | this file |
| `database_tools.py` | Modified | +220 |
| `backtest_agent.py` | Modified | +60 |
| `dashboard.py` | Modified | +60 |
| `migration.sql` | Modified | +30 |
