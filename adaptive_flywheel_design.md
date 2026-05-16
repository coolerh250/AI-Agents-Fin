# Adaptive Data Flywheel Phase 1 — Design Document
**AI Agent Studio | 2026-05-16**

---

## Overview

Closes the feedback loop from `backtest_evaluator` output back into `chief_strategist` prompts. Every trading day the backtest agent writes an accuracy report; this pipeline extracts the 【反省與學習】 lesson, classifies the failure type, persists it to TiDB, and retrieves regime-matched past lessons to inject into the next chief_strategist prompt.

Expected outcome: +5–8% direction accuracy improvement over 30–60 days of lesson accumulation.

---

## Architecture

```
backtest_agent.py
  └─ save_accuracy_node
       ├─ [existing] write to eval_runs / eval_results
       └─ lesson_writer.write_lesson()
            ├─ extract_lesson_text()      ← regex on 【反省與學習】
            ├─ _get_snapshot_age()        ← query workflow_runs
            ├─ classify_error()           ← rule-based priority tree
            ├─ session_episodes regime    ← query session_episodes
            └─ database_tools.save_strategy_lesson()
                    │
                    ▼
              strategy_lessons (TiDB)
                    │
                    ▼
         lesson_retriever.get_lesson_context()
              ├─ _compute_regime()        ← same thresholds as log_session_episode
              └─ database_tools.get_relevant_lessons()  ← SQL relevance score
                    │
                    ▼
         market_analyst_agents.chief_strategist_node
              └─ user_content += lessons  ← ≤600 chars, before Opus call
```

---

## New Files

### `lesson_writer.py`
| Function | Description |
|----------|-------------|
| `extract_lesson_text(report)` | Regex-extracts 【反省與學習】; fallback = full report |
| `_get_snapshot_age(trade_date)` | Queries `workflow_runs.snapshot_age_seconds` |
| `classify_error(...)` | Returns one of 6 error_type strings (priority tree) |
| `write_lesson(...)` | Main entry: extract → classify → lookup regime → persist. Fail-silent, returns bool |

### `lesson_retriever.py`
| Function | Description |
|----------|-------------|
| `_compute_regime(raw_market_data)` | Derives regime_sox / regime_foreign_oi / divergence_signal |
| `get_lesson_context(raw_market_data, limit, max_chars)` | Returns formatted string ≤ max_chars, or "" |

---

## DB Schema — `strategy_lessons` (Step 9)

One row per trade_date (UNIQUE KEY `uq_sl_trade_date`).

| Column | Type | Description |
|--------|------|-------------|
| trade_date | DATE UNIQUE | The evaluated trading day |
| eval_run_id | BIGINT NULL | FK to eval_runs.id |
| error_type | VARCHAR(30) | Classification result |
| lesson_text | TEXT | Extracted 【反省與學習】 text |
| direction_correct | TINYINT | 0 / 1 |
| predicted / actual _direction | VARCHAR(10) | up / flat / down |
| predicted / actual _gap_pct | DECIMAL(6,3) | Percentage |
| gap_error_abs | DECIMAL(6,3) | |abs(predicted - actual)| |
| composite_score | DECIMAL(5,2) | From eval_runs |
| regime_sox | VARCHAR(10) | strong / neutral / weak |
| regime_foreign_oi | VARCHAR(10) | bullish / neutral / bearish |
| divergence_signal | TINYINT | From session_episodes |
| is_active | TINYINT | 1 = active; 0 = expired |
| expires_at | DATE | trade_date + 90 days |

Growth control: UNIQUE guarantees ≤ 1 row per day; 90-day expiry caps active lessons at ~90.

---

## Error Classification Priority

| Priority | error_type | Condition |
|----------|-----------|-----------|
| 1 | `stale_snapshot_error` | snapshot_age_seconds > 21600 (6h) |
| 2 | `missing_data_error` | ≥2 of {foreign_oi_net, sox_chg_pct, djia_chg_pct} are None |
| 3 | `overconfidence_error` | direction_correct=0 AND composite_score > 75 |
| 4 | `direction_error` | direction_correct=0 (general) |
| 5 | `magnitude_error` | direction_correct=1 AND gap_error_abs > 1.0% |
| 6 | `correct_prediction` | direction correct; skipped if gap_error_abs ≤ 0.5% |

---

## Relevance Scoring SQL

```sql
(CASE WHEN regime_sox       = :rsox THEN 20 ELSE 0 END +
 CASE WHEN regime_foreign_oi = :rfoi THEN 15 ELSE 0 END +
 CASE WHEN divergence_signal = :div  THEN 10 ELSE 0 END +
 CASE WHEN DATEDIFF(CURDATE(), trade_date) <= 7  THEN 25 ELSE 0 END +
 CASE WHEN DATEDIFF(CURDATE(), trade_date) <= 30 THEN 15 ELSE 0 END +
 CASE WHEN error_type IN ('direction_error','overconfidence_error') THEN 10 ELSE 0 END
) AS relevance
```

Max possible score: 80 (recent direction_error in matching regime with divergence_signal).

---

## Context Budget Impact

| Component | Tokens (approx) |
|-----------|----------------|
| chip_report | ~300 |
| tech_report | ~300 |
| accuracy history (≤800 chars) | ~200 |
| strategy lessons (≤600 chars) | **+150** |
| **Total delta** | **+150 tok** |

Opus 200K context window; +150 tokens is negligible (<0.1%).

---

## Idempotency & Growth Control

| Mechanism | Implementation |
|-----------|---------------|
| One lesson per day | UNIQUE KEY uq_sl_trade_date + ON DUPLICATE KEY UPDATE |
| Auto-expiry | expires_at = trade_date + INTERVAL 90 DAY |
| Query filter | WHERE is_active=1 AND expires_at >= CURDATE() |
| Manual cleanup | `cleanup_expired_lessons()` → sets is_active=0 |
| Perfect-prediction skip | write_lesson skips if error_type=correct_prediction AND gap_error_abs ≤ 0.5% |

---

## Verification

| Test | Command |
|------|---------|
| Table creation | `ensure_observability_tables()` → strategy_lessons appears |
| lesson_writer smoke | `python -c "from lesson_writer import extract_lesson_text; print(extract_lesson_text('【反省與學習】測試教訓【END】'))"` |
| lesson_retriever smoke | `python -c "from lesson_retriever import _compute_regime; print(_compute_regime({'sox_chg_pct': 1.5, 'foreign_oi_net': -20000}))"` |
| End-to-end | `uv run python backtest_agent.py 2026-05-15` → `SELECT * FROM strategy_lessons` |
| Chief context | `uv run python investment_workflow.py` → logs show "strategy lessons 載入" or "載入失敗（略過）" |
| Idempotency | Run backtest twice same date → strategy_lessons has 1 row |
| Expiry | `SELECT COUNT(*) FROM strategy_lessons WHERE expires_at < CURDATE()` → 0 (new data) |

---

## Phase 2 Roadmap

| Enhancement | Effort |
|-------------|--------|
| Add `cleanup_expired_lessons()` to daily_run.sh | 15 min |
| Dashboard Tab 6 "Flywheel" showing lesson counts by error_type | 2h |
| LLM-powered lesson quality scoring | 2h |
| Multi-lesson synthesis across similar regime days | 3h |
