-- =============================================================================
-- Observability & Cost Governance Migration
-- AI Agent Studio — agent_memory database
-- Run order: Step 1 first (cost_logs extensions), then Steps 2-5 (new tables)
-- All statements are idempotent (IF NOT EXISTS / try-ignore approach in Python)
-- =============================================================================

-- Step 1: Extend cost_logs with run_id and thinking_tokens
-- -------------------------------------------------------
-- These ALTER TABLE statements are applied via ensure_observability_tables()
-- in database_tools.py with individual try/except blocks (TiDB requires
-- separate ALTER TABLE statements per column).

ALTER TABLE cost_logs ADD COLUMN thinking_tokens INT NOT NULL DEFAULT 0;
ALTER TABLE cost_logs ADD COLUMN run_id VARCHAR(36) DEFAULT NULL;
ALTER TABLE cost_logs ADD INDEX idx_cost_run_id (run_id);

-- Step 2: workflow_runs — one row per workflow execution
-- -------------------------------------------------------
CREATE TABLE IF NOT EXISTS workflow_runs (
    id                  VARCHAR(36)    NOT NULL,
    run_type            VARCHAR(20)    NOT NULL DEFAULT 'investment',
    status              VARCHAR(20)    NOT NULL DEFAULT 'running',
    started_at          TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at            TIMESTAMP      NULL,
    snapshot_ts         VARCHAR(40)    NULL,
    snapshot_age_seconds INT           NULL,
    total_cost_usd      DECIMAL(10,6)  NOT NULL DEFAULT 0.000000,
    error_message       TEXT           NULL,
    PRIMARY KEY (id),
    INDEX idx_wr_status (status),
    INDEX idx_wr_started (started_at),
    INDEX idx_wr_type_date (run_type, started_at)
);

-- Step 3: workflow_events — structured event log per run
-- -------------------------------------------------------
CREATE TABLE IF NOT EXISTS workflow_events (
    id          BIGINT         AUTO_INCREMENT PRIMARY KEY,
    run_id      VARCHAR(36)    NOT NULL,
    event_type  VARCHAR(50)    NOT NULL,
    node_name   VARCHAR(50)    NULL,
    detail      JSON           NULL,
    severity    VARCHAR(10)    NOT NULL DEFAULT 'info',
    created_at  TIMESTAMP      DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_we_run (run_id),
    INDEX idx_we_type (event_type),
    INDEX idx_we_severity (severity),
    INDEX idx_we_created (created_at)
);

-- Step 4: llm_traces — full prompt/response per LLM call
-- -------------------------------------------------------
CREATE TABLE IF NOT EXISTS llm_traces (
    id              BIGINT         AUTO_INCREMENT PRIMARY KEY,
    run_id          VARCHAR(36)    NOT NULL,
    agent_name      VARCHAR(50)    NOT NULL,
    model_name      VARCHAR(100)   NOT NULL,
    system_prompt   TEXT           NULL,
    user_content    TEXT           NULL,
    raw_response    TEXT           NULL,
    finish_reason   VARCHAR(30)    NULL,
    input_tokens    INT            NOT NULL DEFAULT 0,
    output_tokens   INT            NOT NULL DEFAULT 0,
    thinking_tokens INT            NOT NULL DEFAULT 0,
    latency_ms      INT            NULL,
    created_at      TIMESTAMP      DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_lt_run_agent (run_id, agent_name),
    INDEX idx_lt_created (created_at)
);

-- Step 5: audit_log — before/after for every DB mutation
-- -------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGINT         AUTO_INCREMENT PRIMARY KEY,
    table_name  VARCHAR(50)    NOT NULL,
    operation   VARCHAR(10)    NOT NULL,
    record_id   BIGINT         NULL,
    actor       VARCHAR(50)    NOT NULL DEFAULT 'system',
    before_json JSON           NULL,
    after_json  JSON           NULL,
    created_at  TIMESTAMP      DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_al_table_op (table_name, operation),
    INDEX idx_al_created (created_at)
);

-- Step 6: UNIQUE guard on daily_briefs.trade_date
-- -------------------------------------------------------
-- Prevents duplicate rows from double-runs.
-- Applied with try/except in Python (may already exist).
ALTER TABLE daily_briefs ADD UNIQUE KEY uq_trade_date (trade_date);

-- Step 7: eval_runs — one row per evaluation session (per trade_date)
-- -------------------------------------------------------
-- Created automatically by ensure_eval_tables() in database_tools.py
-- UNIQUE on trade_date: re-run same day → ON DUPLICATE KEY UPDATE
CREATE TABLE IF NOT EXISTS eval_runs (
    id                  BIGINT        AUTO_INCREMENT PRIMARY KEY,
    trade_date          DATE          NOT NULL,
    run_id_ref          VARCHAR(36)   NULL,
    triggered_by        VARCHAR(30)   NOT NULL DEFAULT 'manual',
    status              VARCHAR(20)   NOT NULL DEFAULT 'success',
    brief_quality_score DECIMAL(5,2)  NULL,
    direction_correct   TINYINT       NULL,
    predicted_direction VARCHAR(10)   NULL,
    actual_direction    VARCHAR(10)   NULL,
    completed_at        TIMESTAMP     NULL,
    created_at          TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_er_trade_date (trade_date),
    INDEX idx_er_run_ref (run_id_ref)
);

-- Step 8: eval_results — one row per agent per eval_run
-- -------------------------------------------------------
CREATE TABLE IF NOT EXISTS eval_results (
    id                  BIGINT        AUTO_INCREMENT PRIMARY KEY,
    eval_run_id         BIGINT        NOT NULL,
    trade_date          DATE          NOT NULL,
    agent_name          VARCHAR(50)   NOT NULL,
    quality_score       DECIMAL(5,2)  NULL,
    schema_valid        TINYINT       NULL,
    missing_fields      JSON          NULL,
    hallucination_flags JSON          NULL,
    extra_metrics       JSON          NULL,
    created_at          TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_evr_eval_run   (eval_run_id),
    INDEX idx_evr_agent_date (agent_name, trade_date)
);

-- Step 9: strategy_lessons — adaptive flywheel learning store
-- -------------------------------------------------------
-- One row per trade_date (UNIQUE). Lessons expire after 90 days via expires_at.
-- Created automatically by ensure_strategy_lessons_table() in database_tools.py
CREATE TABLE IF NOT EXISTS strategy_lessons (
    id                  BIGINT        AUTO_INCREMENT PRIMARY KEY,
    trade_date          DATE          NOT NULL,
    eval_run_id         BIGINT        NULL,
    error_type          VARCHAR(30)   NOT NULL,
    lesson_text         TEXT          NOT NULL,
    direction_correct   TINYINT       NOT NULL DEFAULT 0,
    predicted_direction VARCHAR(10)   NULL,
    actual_direction    VARCHAR(10)   NULL,
    predicted_gap_pct   DECIMAL(6,3)  NULL,
    actual_gap_pct      DECIMAL(6,3)  NULL,
    gap_error_abs       DECIMAL(6,3)  NULL,
    composite_score     DECIMAL(5,2)  NULL,
    regime_sox          VARCHAR(10)   NULL,
    regime_foreign_oi   VARCHAR(10)   NULL,
    divergence_signal   TINYINT       NULL,
    is_active           TINYINT       NOT NULL DEFAULT 1,
    expires_at          DATE          NULL,
    created_at          TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_sl_trade_date  (trade_date),
    INDEX idx_sl_error_type      (error_type),
    INDEX idx_sl_regime          (regime_sox, regime_foreign_oi),
    INDEX idx_sl_active_date     (is_active, trade_date DESC)
);

-- Step 10: Add lesson_quality_score to strategy_lessons (Flywheel Phase 2)
ALTER TABLE strategy_lessons ADD COLUMN IF NOT EXISTS lesson_quality_score DECIMAL(3,1) NULL;

-- Step 11: Fix daily_briefs unique key (Step 6 was non-unique; replace it)
-- Idempotent: DROP ignores error if not exists; ADD UNIQUE uses IF NOT EXISTS
-- Run manually if needed; ensure_observability_tables() handles this automatically.
ALTER TABLE daily_briefs DROP INDEX IF EXISTS idx_trade_date;
ALTER TABLE daily_briefs ADD UNIQUE KEY IF NOT EXISTS uq_db_trade_date (trade_date);
