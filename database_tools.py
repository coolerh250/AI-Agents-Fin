"""
database_tools.py
SQLAlchemy/PyMySQL helpers for agent_memory on TiDB.
"""
import json as _json
import os
from datetime import date
from functools import lru_cache
from typing import Optional

from dotenv import load_dotenv
from loguru import logger
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

load_dotenv()


@lru_cache(maxsize=1)
def _engine() -> Engine:
    host = os.getenv("TIDB_HOST", "127.0.0.1")
    port = os.getenv("TIDB_PORT", "4000")
    user = os.getenv("TIDB_USER", "root")
    password = os.getenv("TIDB_PASSWORD", "")
    db = os.getenv("TIDB_DB", "agent_memory")
    url = f"mysql+pymysql://{user}:{password}@{host}:{port}/{db}?charset=utf8mb4"
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=2,
        connect_args={"connect_timeout": 10, "read_timeout": 30},
    )


def save_brief(
    trade_date: date,
    brief_text: str,
    predicted_gap_pct: Optional[float],
    gap_direction: Optional[str],
    line_report: Optional[str] = None,
) -> int:
    """Upsert daily brief (one per trade_date). Returns the row id."""
    with _engine().begin() as conn:
        result = conn.execute(
            text("""
                INSERT INTO daily_briefs
                    (trade_date, brief_text, predicted_gap_pct, gap_direction, line_report)
                VALUES (:d, :brief, :gap_pct, :direction, :line_report)
                ON DUPLICATE KEY UPDATE
                    brief_text        = VALUES(brief_text),
                    predicted_gap_pct = VALUES(predicted_gap_pct),
                    gap_direction     = VALUES(gap_direction),
                    line_report       = VALUES(line_report)
            """),
            {"d": trade_date, "brief": brief_text,
             "gap_pct": predicted_gap_pct, "direction": gap_direction,
             "line_report": line_report},
        )
        row_id = result.lastrowid
        if row_id == 0:
            # ON DUPLICATE KEY UPDATE path — fetch existing row id
            existing = conn.execute(
                text("SELECT id FROM daily_briefs WHERE trade_date = :d LIMIT 1"),
                {"d": trade_date},
            ).fetchone()
            row_id = int(existing[0]) if existing else 0
        return row_id


def get_brief(trade_date: date) -> Optional[dict]:
    """Fetch the most recent brief for a given trade date."""
    with _engine().connect() as conn:
        row = conn.execute(
            text("""
                SELECT id, trade_date, brief_text, predicted_gap_pct, gap_direction,
                       line_report, created_at
                FROM daily_briefs
                WHERE trade_date = :d
                ORDER BY id DESC LIMIT 1
            """),
            {"d": trade_date},
        ).fetchone()
    return dict(row._mapping) if row else None


def save_actual(
    trade_date: date,
    open_price: float,
    close_price: float,
    actual_gap_pct: float,
    notes: str = "",
) -> None:
    """Upsert actual market data for a trade date."""
    with _engine().begin() as conn:
        conn.execute(
            text("""
                INSERT INTO market_actuals
                    (trade_date, open_price, close_price, actual_gap_pct, notes)
                VALUES (:d, :open, :close, :gap, :notes)
                ON DUPLICATE KEY UPDATE
                    open_price     = VALUES(open_price),
                    close_price    = VALUES(close_price),
                    actual_gap_pct = VALUES(actual_gap_pct),
                    notes          = VALUES(notes)
            """),
            {"d": trade_date, "open": open_price, "close": close_price,
             "gap": actual_gap_pct, "notes": notes},
        )


def get_actual(trade_date: date) -> Optional[dict]:
    """Fetch actual market data for a given trade date."""
    with _engine().connect() as conn:
        row = conn.execute(
            text("SELECT * FROM market_actuals WHERE trade_date = :d"),
            {"d": trade_date},
        ).fetchone()
    return dict(row._mapping) if row else None


def ensure_cost_logs_table() -> None:
    with _engine().begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS cost_logs (
                id                 BIGINT AUTO_INCREMENT PRIMARY KEY,
                agent_name         VARCHAR(50)    NOT NULL,
                model_name         VARCHAR(100)   NOT NULL,
                input_tokens       INT            NOT NULL DEFAULT 0,
                output_tokens      INT            NOT NULL DEFAULT 0,
                thinking_tokens    INT            NOT NULL DEFAULT 0,
                estimated_cost_usd DECIMAL(10,6)  NOT NULL DEFAULT 0.000000,
                latency_ms         INT,
                run_id             VARCHAR(36)    DEFAULT NULL,
                logged_at          TIMESTAMP      DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_agent (agent_name),
                INDEX idx_logged_at (logged_at),
                INDEX idx_run_id (run_id)
            )
        """))


def log_cost(
    agent_name: str,
    model_name: str,
    input_tokens: int,
    output_tokens: int,
    thinking_tokens: int = 0,
    estimated_cost_usd: float = 0.0,
    latency_ms: Optional[int] = None,
    run_id: Optional[str] = None,
) -> None:
    with _engine().begin() as conn:
        conn.execute(
            text("""
                INSERT INTO cost_logs
                    (agent_name, model_name, input_tokens, output_tokens,
                     thinking_tokens, estimated_cost_usd, latency_ms, run_id)
                VALUES (:agent, :model, :in_tok, :out_tok, :think_tok, :cost, :lat, :run_id)
            """),
            {"agent": agent_name, "model": model_name,
             "in_tok": input_tokens, "out_tok": output_tokens,
             "think_tok": thinking_tokens, "cost": estimated_cost_usd,
             "lat": latency_ms, "run_id": run_id},
        )


def get_cost_summary(days: int = 30) -> list[dict]:
    sql = """
        SELECT agent_name, model_name,
               SUM(input_tokens)       AS total_input,
               SUM(output_tokens)      AS total_output,
               SUM(thinking_tokens)    AS total_thinking,
               SUM(estimated_cost_usd) AS total_cost_usd,
               AVG(latency_ms)         AS avg_latency_ms,
               COUNT(*)                AS runs
        FROM cost_logs
        WHERE logged_at >= NOW() - INTERVAL :days DAY
        GROUP BY agent_name, model_name
        ORDER BY total_cost_usd DESC
    """
    with _engine().connect() as conn:
        rows = conn.execute(text(sql), {"days": days}).fetchall()
    return [dict(r._mapping) for r in rows]


def get_cost_trend(days: int = 30) -> list[dict]:
    sql = """
        SELECT DATE(logged_at) AS day,
               SUM(estimated_cost_usd) AS daily_cost_usd
        FROM cost_logs
        WHERE logged_at >= NOW() - INTERVAL :days DAY
        GROUP BY DATE(logged_at)
        ORDER BY day
    """
    with _engine().connect() as conn:
        rows = conn.execute(text(sql), {"days": days}).fetchall()
    return [dict(r._mapping) for r in rows]


def ensure_portfolio_table() -> None:
    with _engine().begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS user_portfolio (
                id               BIGINT AUTO_INCREMENT PRIMARY KEY,
                stock_id         VARCHAR(20)   NOT NULL,
                entry_price      DECIMAL(10,2) NOT NULL,
                quantity         INT           NOT NULL,
                stop_loss_level  DECIMAL(5,2)  NOT NULL DEFAULT 5.00,
                strategy_type    VARCHAR(20)   NOT NULL DEFAULT '波段',
                line_user_id     VARCHAR(50)   NULL DEFAULT NULL,
                created_at       TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uq_user_stock (line_user_id, stock_id),
                INDEX idx_line_user (line_user_id)
            )
        """))
        # Migrate existing table: add line_user_id column if missing
        for stmt, label in [
            (
                "ALTER TABLE user_portfolio "
                "ADD COLUMN line_user_id VARCHAR(50) NULL DEFAULT NULL",
                "add line_user_id",
            ),
            (
                "ALTER TABLE user_portfolio ADD INDEX idx_line_user (line_user_id)",
                "add idx_line_user",
            ),
            (
                "ALTER TABLE user_portfolio DROP INDEX uq_stock_entry",
                "drop uq_stock_entry",
            ),
            (
                "ALTER TABLE user_portfolio "
                "ADD UNIQUE KEY uq_user_stock (line_user_id, stock_id)",
                "add uq_user_stock",
            ),
        ]:
            try:
                conn.execute(text(stmt))
            except Exception:
                pass  # Already applied — expected on subsequent runs


def get_portfolio(user_id: Optional[str] = None) -> list[dict]:
    """Return holdings for a user. user_id=None returns legacy NULL-user records."""
    with _engine().connect() as conn:
        rows = conn.execute(
            text("""
                SELECT * FROM user_portfolio
                WHERE (line_user_id = :uid OR (:uid IS NULL AND line_user_id IS NULL))
                ORDER BY created_at
            """),
            {"uid": user_id},
        ).fetchall()
    return [dict(r._mapping) for r in rows]


def add_portfolio_item(
    stock_id: str,
    entry_price: float,
    quantity: int,
    stop_loss_level: float = 5.0,
    strategy_type: str = "波段",
    user_id: Optional[str] = None,
) -> bool:
    """Insert a new holding. Returns False if (line_user_id, stock_id) already exists."""
    try:
        with _engine().begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO user_portfolio
                        (stock_id, entry_price, quantity, stop_loss_level, strategy_type, line_user_id)
                    VALUES (:sid, :entry, :qty, :sl, :strat, :uid)
                """),
                {"sid": stock_id, "entry": entry_price, "qty": quantity,
                 "sl": stop_loss_level, "strat": strategy_type, "uid": user_id},
            )
        return True
    except Exception:
        return False


def delete_portfolio_item(item_id: int, user_id: Optional[str] = None) -> None:
    with _engine().begin() as conn:
        before_row = conn.execute(
            text("SELECT * FROM user_portfolio WHERE id = :id"), {"id": item_id}
        ).fetchone()
        before = dict(before_row._mapping) if before_row else {}
        conn.execute(
            text("""
                DELETE FROM user_portfolio
                WHERE id = :id
                  AND (line_user_id = :uid OR (:uid IS NULL AND line_user_id IS NULL))
            """),
            {"id": item_id, "uid": user_id},
        )
    log_audit("user_portfolio", "DELETE", item_id, "dashboard", before=before)


def delete_portfolio_by_stock(stock_id: str, user_id: Optional[str] = None) -> bool:
    """Delete a holding by stock_id for a user. Returns True if a row was deleted."""
    with _engine().begin() as conn:
        result = conn.execute(
            text("""
                DELETE FROM user_portfolio
                WHERE stock_id = :sid
                  AND (line_user_id = :uid OR (:uid IS NULL AND line_user_id IS NULL))
            """),
            {"sid": stock_id, "uid": user_id},
        )
    return result.rowcount > 0


def update_portfolio_item(
    item_id: int,
    quantity: int,
    stop_loss_level: float,
    strategy_type: str,
    entry_price: Optional[float] = None,
    user_id: Optional[str] = None,
) -> None:
    with _engine().begin() as conn:
        before_row = conn.execute(
            text("SELECT * FROM user_portfolio WHERE id = :id"), {"id": item_id}
        ).fetchone()
        before = dict(before_row._mapping) if before_row else {}
        if entry_price is not None:
            conn.execute(
                text("""
                    UPDATE user_portfolio
                    SET quantity = :qty, stop_loss_level = :sl, strategy_type = :strat,
                        entry_price = :entry
                    WHERE id = :id
                      AND (line_user_id = :uid OR (:uid IS NULL AND line_user_id IS NULL))
                """),
                {"qty": quantity, "sl": stop_loss_level, "strat": strategy_type,
                 "entry": entry_price, "id": item_id, "uid": user_id},
            )
        else:
            conn.execute(
                text("""
                    UPDATE user_portfolio
                    SET quantity = :qty, stop_loss_level = :sl, strategy_type = :strat
                    WHERE id = :id
                      AND (line_user_id = :uid OR (:uid IS NULL AND line_user_id IS NULL))
                """),
                {"qty": quantity, "sl": stop_loss_level, "strat": strategy_type,
                 "id": item_id, "uid": user_id},
            )
        after_row = conn.execute(
            text("SELECT * FROM user_portfolio WHERE id = :id"), {"id": item_id}
        ).fetchone()
        after = dict(after_row._mapping) if after_row else {}
    log_audit("user_portfolio", "UPDATE", item_id, "dashboard", before=before, after=after)


def update_portfolio_entry_price(
    stock_id: str,
    entry_price: float,
    user_id: Optional[str] = None,
) -> bool:
    """Update entry_price for a stock. Returns True if updated."""
    with _engine().begin() as conn:
        result = conn.execute(
            text("""
                UPDATE user_portfolio
                SET entry_price = :entry
                WHERE stock_id = :sid
                  AND (line_user_id = :uid OR (:uid IS NULL AND line_user_id IS NULL))
            """),
            {"entry": entry_price, "sid": stock_id, "uid": user_id},
        )
    return result.rowcount > 0


def seed_test_portfolio() -> None:
    owner_id = os.getenv("LINE_USER_ID") or None
    with _engine().begin() as conn:
        # Migrate existing NULL records to the owner's LINE user ID
        if owner_id:
            # Remove duplicate NULL rows: keep the highest-id row per stock_id
            conn.execute(text("""
                DELETE p1 FROM user_portfolio p1
                INNER JOIN user_portfolio p2
                    ON p1.stock_id = p2.stock_id
                    AND p1.line_user_id IS NULL
                    AND p2.line_user_id IS NULL
                    AND p1.id < p2.id
            """))
            # Remove NULL records that would conflict with owner's existing holdings
            conn.execute(
                text("""
                    DELETE p FROM user_portfolio p
                    INNER JOIN user_portfolio p2
                        ON p.stock_id = p2.stock_id AND p2.line_user_id = :uid
                    WHERE p.line_user_id IS NULL
                """),
                {"uid": owner_id},
            )
            # Migrate remaining NULL records to the owner
            conn.execute(
                text("UPDATE user_portfolio SET line_user_id = :uid WHERE line_user_id IS NULL"),
                {"uid": owner_id},
            )
        conn.execute(
            text("""
                INSERT IGNORE INTO user_portfolio
                    (stock_id, entry_price, quantity, stop_loss_level, strategy_type, line_user_id)
                VALUES (:sid, :entry, :qty, :sl, :strat, :uid)
            """),
            {"sid": "2330", "entry": 1000.00, "qty": 1000, "sl": 5.00, "strat": "波段",
             "uid": owner_id},
        )


def get_recent_accuracy(days: int = 5) -> list[dict]:
    """Return last N trade days joining daily_briefs and market_actuals."""
    sql = """
        SELECT b.trade_date, b.gap_direction, b.predicted_gap_pct,
               a.actual_gap_pct, a.open_price, a.close_price
        FROM daily_briefs b
        LEFT JOIN market_actuals a ON b.trade_date = a.trade_date
        ORDER BY b.trade_date DESC
        LIMIT :days
    """
    with _engine().connect() as conn:
        rows = conn.execute(text(sql), {"days": days}).fetchall()
    return [dict(r._mapping) for r in rows]


# ── Observability tables ───────────────────────────────────────────────────────

def ensure_observability_tables() -> None:
    """Create observability tables and migrate cost_logs if needed. Idempotent."""
    with _engine().begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS workflow_runs (
                id                   VARCHAR(36)   NOT NULL,
                run_type             VARCHAR(20)   NOT NULL DEFAULT 'investment',
                status               VARCHAR(20)   NOT NULL DEFAULT 'running',
                started_at           TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
                ended_at             TIMESTAMP     NULL,
                snapshot_ts          VARCHAR(40)   NULL,
                snapshot_age_seconds INT           NULL,
                total_cost_usd       DECIMAL(10,6) NOT NULL DEFAULT 0.000000,
                error_message        TEXT          NULL,
                PRIMARY KEY (id),
                INDEX idx_wr_status (status),
                INDEX idx_wr_started (started_at),
                INDEX idx_wr_type_date (run_type, started_at)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS workflow_events (
                id         BIGINT        AUTO_INCREMENT PRIMARY KEY,
                run_id     VARCHAR(36)   NOT NULL,
                event_type VARCHAR(50)   NOT NULL,
                node_name  VARCHAR(50)   NULL,
                detail     JSON          NULL,
                severity   VARCHAR(10)   NOT NULL DEFAULT 'info',
                created_at TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_we_run (run_id),
                INDEX idx_we_type (event_type),
                INDEX idx_we_severity (severity),
                INDEX idx_we_created (created_at)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS llm_traces (
                id              BIGINT        AUTO_INCREMENT PRIMARY KEY,
                run_id          VARCHAR(36)   NOT NULL,
                agent_name      VARCHAR(50)   NOT NULL,
                model_name      VARCHAR(100)  NOT NULL,
                system_prompt   TEXT          NULL,
                user_content    TEXT          NULL,
                raw_response    TEXT          NULL,
                finish_reason   VARCHAR(30)   NULL,
                input_tokens    INT           NOT NULL DEFAULT 0,
                output_tokens   INT           NOT NULL DEFAULT 0,
                thinking_tokens INT           NOT NULL DEFAULT 0,
                latency_ms      INT           NULL,
                created_at      TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_lt_run_agent (run_id, agent_name),
                INDEX idx_lt_created (created_at)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id          BIGINT        AUTO_INCREMENT PRIMARY KEY,
                table_name  VARCHAR(50)   NOT NULL,
                operation   VARCHAR(10)   NOT NULL,
                record_id   BIGINT        NULL,
                actor       VARCHAR(50)   NOT NULL DEFAULT 'system',
                before_json JSON          NULL,
                after_json  JSON          NULL,
                created_at  TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_al_table_op (table_name, operation),
                INDEX idx_al_created (created_at)
            )
        """))

    ensure_tool_audit_log_table()       # Phase 2
    ensure_session_episodes_table()     # Phase 4
    ensure_eval_tables()                # Evaluation Framework
    ensure_strategy_lessons_table()     # Adaptive Flywheel Phase 1
    ensure_stock_info_table()           # Stock code → company name mapping

    # Migrate cost_logs: add thinking_tokens, run_id if missing
    for stmt, label in [
        ("ALTER TABLE cost_logs ADD COLUMN thinking_tokens INT NOT NULL DEFAULT 0", "add thinking_tokens"),
        ("ALTER TABLE cost_logs ADD COLUMN run_id VARCHAR(36) DEFAULT NULL", "add run_id"),
        ("ALTER TABLE cost_logs ADD INDEX idx_cost_run_id (run_id)", "add idx_cost_run_id"),
        ("ALTER TABLE daily_briefs ADD UNIQUE KEY uq_trade_date (trade_date)", "add uq_trade_date"),
        ("ALTER TABLE daily_briefs ADD COLUMN line_report TEXT NULL", "add line_report"),
    ]:
        try:
            with _engine().begin() as conn:
                conn.execute(text(stmt))
        except Exception:
            pass  # Already applied


# ── Workflow run lifecycle ─────────────────────────────────────────────────────

def create_workflow_run(
    run_id: str,
    run_type: str = "investment",
    snapshot_ts: Optional[str] = None,
    snapshot_age_seconds: Optional[int] = None,
) -> None:
    try:
        with _engine().begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO workflow_runs (id, run_type, status, snapshot_ts, snapshot_age_seconds)
                    VALUES (:id, :rtype, 'running', :snap_ts, :snap_age)
                """),
                {"id": run_id, "rtype": run_type,
                 "snap_ts": snapshot_ts, "snap_age": snapshot_age_seconds},
            )
    except Exception as exc:
        logger.warning(f"[telemetry] create_workflow_run failed: {exc}")


def finish_workflow_run(
    run_id: str,
    status: str,
    error_message: Optional[str] = None,
) -> None:
    try:
        with _engine().begin() as conn:
            total = conn.execute(
                text("SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM cost_logs WHERE run_id = :rid"),
                {"rid": run_id},
            ).scalar()
            conn.execute(
                text("""
                    UPDATE workflow_runs
                    SET status = :status, ended_at = NOW(),
                        total_cost_usd = :cost, error_message = :err
                    WHERE id = :id
                """),
                {"status": status, "cost": float(total or 0),
                 "err": error_message, "id": run_id},
            )
    except Exception as exc:
        logger.warning(f"[telemetry] finish_workflow_run failed: {exc}")


def get_run_status(run_date: date) -> Optional[dict]:
    with _engine().connect() as conn:
        row = conn.execute(
            text("""
                SELECT id, run_type, status, started_at, ended_at,
                       snapshot_age_seconds, total_cost_usd, error_message
                FROM workflow_runs
                WHERE DATE(started_at) = :d AND run_type = 'investment'
                ORDER BY started_at DESC LIMIT 1
            """),
            {"d": run_date},
        ).fetchone()
    return dict(row._mapping) if row else None


def get_workflow_runs(days: int = 30) -> list[dict]:
    with _engine().connect() as conn:
        rows = conn.execute(
            text("""
                SELECT id, run_type, status, started_at, ended_at,
                       snapshot_age_seconds, total_cost_usd, error_message
                FROM workflow_runs
                WHERE started_at >= NOW() - INTERVAL :days DAY
                ORDER BY started_at DESC
            """),
            {"days": days},
        ).fetchall()
    return [dict(r._mapping) for r in rows]


# ── Event log ─────────────────────────────────────────────────────────────────

def log_event(
    run_id: str,
    event_type: str,
    node_name: Optional[str] = None,
    detail: Optional[dict] = None,
    severity: str = "info",
) -> None:
    try:
        with _engine().begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO workflow_events
                        (run_id, event_type, node_name, detail, severity)
                    VALUES (:run_id, :etype, :node, :detail, :sev)
                """),
                {"run_id": run_id, "etype": event_type, "node": node_name,
                 "detail": _json.dumps(detail or {}, ensure_ascii=False),
                 "sev": severity},
            )
    except Exception as exc:
        logger.warning(f"[telemetry] log_event failed: {exc}")


def get_run_events(run_id: str) -> list[dict]:
    with _engine().connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM workflow_events WHERE run_id = :rid ORDER BY created_at"),
            {"rid": run_id},
        ).fetchall()
    return [dict(r._mapping) for r in rows]


def get_recent_events(
    days: int = 7,
    severity_filter: Optional[list] = None,
) -> list[dict]:
    base = "SELECT * FROM workflow_events WHERE created_at >= NOW() - INTERVAL :days DAY"
    params: dict = {"days": days}
    if severity_filter:
        placeholders = ", ".join(f":sev{i}" for i in range(len(severity_filter)))
        base += f" AND severity IN ({placeholders})"
        for i, s in enumerate(severity_filter):
            params[f"sev{i}"] = s
    base += " ORDER BY created_at DESC LIMIT 500"
    with _engine().connect() as conn:
        rows = conn.execute(text(base), params).fetchall()
    return [dict(r._mapping) for r in rows]


# ── LLM trace ─────────────────────────────────────────────────────────────────

def log_llm_trace(
    run_id: str,
    agent_name: str,
    model_name: str,
    system_prompt: str,
    user_content: str,
    raw_response: str,
    finish_reason: Optional[str],
    input_tokens: int,
    output_tokens: int,
    thinking_tokens: int,
    latency_ms: int,
) -> None:
    try:
        with _engine().begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO llm_traces
                        (run_id, agent_name, model_name, system_prompt, user_content,
                         raw_response, finish_reason, input_tokens, output_tokens,
                         thinking_tokens, latency_ms)
                    VALUES (:run_id, :agent, :model, :sys, :usr, :resp,
                            :fin, :in_tok, :out_tok, :think_tok, :lat)
                """),
                {"run_id": run_id, "agent": agent_name, "model": model_name,
                 "sys": (system_prompt or "")[:4000],
                 "usr": (user_content or "")[:4000],
                 "resp": (raw_response or "")[:8000],
                 "fin": finish_reason,
                 "in_tok": input_tokens, "out_tok": output_tokens,
                 "think_tok": thinking_tokens, "lat": latency_ms},
            )
    except Exception as exc:
        logger.warning(f"[telemetry] log_llm_trace failed: {exc}")


# ── Audit log ─────────────────────────────────────────────────────────────────

def log_audit(
    table_name: str,
    operation: str,
    record_id: Optional[int],
    actor: str = "system",
    before: Optional[dict] = None,
    after: Optional[dict] = None,
) -> None:
    try:
        with _engine().begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO audit_log
                        (table_name, operation, record_id, actor, before_json, after_json)
                    VALUES (:tbl, :op, :rid, :actor, :before, :after)
                """),
                {"tbl": table_name, "op": operation, "rid": record_id, "actor": actor,
                 "before": _json.dumps(before, default=str) if before else None,
                 "after":  _json.dumps(after,  default=str) if after  else None},
            )
    except Exception as exc:
        logger.warning(f"[telemetry] log_audit failed: {exc}")


# ── Cost queries (extended) ────────────────────────────────────────────────────

def get_run_cost(run_id: str) -> float:
    with _engine().connect() as conn:
        val = conn.execute(
            text("SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM cost_logs WHERE run_id = :rid"),
            {"rid": run_id},
        ).scalar()
    return float(val or 0)


def get_per_run_cost_summary(days: int = 30) -> list[dict]:
    sql = """
        SELECT wr.id AS run_id,
               DATE(wr.started_at)        AS trade_date,
               wr.status,
               wr.total_cost_usd,
               wr.snapshot_age_seconds,
               COALESCE(SUM(cl.thinking_tokens), 0)  AS total_thinking_tokens,
               COALESCE(MAX(CASE WHEN cl.agent_name = 'chief_strategist'
                           THEN cl.thinking_tokens END), 0) AS opus_thinking_tokens,
               TIMESTAMPDIFF(SECOND, wr.started_at, wr.ended_at) AS duration_seconds
        FROM workflow_runs wr
        LEFT JOIN cost_logs cl ON wr.id = cl.run_id
        WHERE wr.run_type = 'investment'
          AND wr.started_at >= NOW() - INTERVAL :days DAY
        GROUP BY wr.id, wr.started_at, wr.status, wr.total_cost_usd,
                 wr.snapshot_age_seconds, wr.ended_at
        ORDER BY wr.started_at DESC
    """
    with _engine().connect() as conn:
        rows = conn.execute(text(sql), {"days": days}).fetchall()
    return [dict(r._mapping) for r in rows]


# ── Phase 2: Tool audit log ────────────────────────────────────────────────────

def ensure_tool_audit_log_table() -> None:
    """Create tool_audit_log table. Called by ensure_observability_tables()."""
    try:
        with _engine().begin() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS tool_audit_log (
                    id            BIGINT       AUTO_INCREMENT PRIMARY KEY,
                    tool_id       VARCHAR(100) NOT NULL,
                    tool_type     VARCHAR(20)  NOT NULL DEFAULT 'direct',
                    caller        VARCHAR(50)  NULL,
                    run_id        VARCHAR(36)  NULL,
                    status        VARCHAR(20)  NOT NULL DEFAULT 'ok',
                    latency_ms    INT          NULL,
                    error_message TEXT         NULL,
                    detail        JSON         NULL,
                    created_at    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_tal_tool    (tool_id),
                    INDEX idx_tal_run     (run_id),
                    INDEX idx_tal_created (created_at)
                )
            """))
    except Exception as exc:
        logger.warning(f"[migration] ensure_tool_audit_log_table failed: {exc}")


def log_tool_call(
    tool_id: str,
    tool_type: str = "direct",
    caller: Optional[str] = None,
    run_id: Optional[str] = None,
    status: str = "ok",
    latency_ms: Optional[int] = None,
    error_message: Optional[str] = None,
    detail: Optional[dict] = None,
) -> None:
    """Record one tool invocation in tool_audit_log. Fails silently."""
    try:
        with _engine().begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO tool_audit_log
                        (tool_id, tool_type, caller, run_id, status, latency_ms,
                         error_message, detail)
                    VALUES (:tool_id, :ttype, :caller, :run_id, :status,
                            :lat, :err, :detail)
                """),
                {"tool_id": tool_id, "ttype": tool_type, "caller": caller,
                 "run_id": run_id, "status": status, "lat": latency_ms,
                 "err": (error_message or "")[:500] if error_message else None,
                 "detail": _json.dumps(detail or {}, ensure_ascii=False)},
            )
    except Exception as exc:
        logger.warning(f"[telemetry] log_tool_call failed: {exc}")


# Callers allowed per high-risk tool. Fail-open: logs violation, never blocks.
_TOOL_PERMISSION_RULES: dict[str, list[str]] = {
    "save_brief":          ["save_to_db"],
    "add_portfolio_item":  ["dashboard", "line_webhook"],
    "delete_portfolio_item": ["dashboard", "line_webhook"],
    "update_portfolio_item": ["dashboard", "line_webhook"],
    "send_line":           ["send_notification", "alert_runner", "investment_workflow"],
    "send_telegram":       ["send_notification", "alert_runner", "investment_workflow"],
}


def validate_tool_permission(tool_id: str, caller: str) -> bool:
    """Return True if caller is allowed. Log violation but never block (fail-open)."""
    allowed = _TOOL_PERMISSION_RULES.get(tool_id)
    if allowed is None:
        return True
    if caller in allowed:
        return True
    logger.warning(f"[tool_permission] {caller!r} called {tool_id!r} — not in allowed list {allowed}")
    return False  # caller can decide whether to honour; production is fail-open


# ── Phase 3: Context engineering helpers ──────────────────────────────────────

def get_recent_accuracy_context(days: int = 14) -> str:
    """
    Return last N days of brief predictions vs actuals as a compact context string.
    Injected into chief_strategist_node user prompt.
    Returns "" when no matched data exists (no join rows yet).
    """
    try:
        with _engine().connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT b.trade_date,
                           b.gap_direction,
                           b.predicted_gap_pct,
                           a.actual_gap_pct,
                           CASE
                             WHEN (b.gap_direction = 'up'   AND a.actual_gap_pct >  0.3)
                               OR (b.gap_direction = 'down' AND a.actual_gap_pct < -0.3)
                               OR (b.gap_direction = 'flat' AND ABS(a.actual_gap_pct) <= 0.3)
                             THEN 1 ELSE 0
                           END AS correct
                    FROM daily_briefs b
                    JOIN market_actuals a ON b.trade_date = a.trade_date
                    WHERE b.trade_date >= CURDATE() - INTERVAL :days DAY
                    ORDER BY b.trade_date DESC
                    LIMIT 10
                """),
                {"days": days},
            ).fetchall()
    except Exception as exc:
        logger.warning(f"[context] get_recent_accuracy_context failed: {exc}")
        return ""

    if not rows:
        return ""

    correct_count = sum(int(r[4]) for r in rows)
    accuracy_pct  = correct_count / len(rows) * 100

    lines = [f"【近期預測準確率 {accuracy_pct:.0f}% ({correct_count}/{len(rows)}筆)】"]
    for r in rows:
        label = "✓" if int(r[4]) else "✗"
        pred_gap  = float(r[2]) if r[2] is not None else 0.0
        actual_gap = float(r[3]) if r[3] is not None else 0.0
        lines.append(
            f"  {r[0]} {label} 預測 {r[1]}({pred_gap:+.1f}%) → 實際 {actual_gap:+.1f}%"
        )
    return "\n".join(lines)


# ── Phase 4: Session episodes ──────────────────────────────────────────────────

def ensure_session_episodes_table() -> None:
    """Create session_episodes table. Called by ensure_observability_tables()."""
    try:
        with _engine().begin() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS session_episodes (
                    id                  BIGINT        AUTO_INCREMENT PRIMARY KEY,
                    run_id              VARCHAR(36)   NOT NULL,
                    trade_date          DATE          NOT NULL,
                    brief_id            BIGINT        NULL,
                    predicted_direction VARCHAR(10)   NULL,
                    predicted_gap_pct   DECIMAL(6,3)  NULL,
                    actual_direction    VARCHAR(10)   NULL,
                    actual_gap_pct      DECIMAL(6,3)  NULL,
                    direction_correct   TINYINT       NULL,
                    foreign_oi_net      INT           NULL,
                    trust_oi_net        INT           NULL,
                    dealer_oi_net       INT           NULL,
                    djia_chg_pct        DECIMAL(6,3)  NULL,
                    ndx_chg_pct         DECIMAL(6,3)  NULL,
                    sox_chg_pct         DECIMAL(6,3)  NULL,
                    tsm_adr_chg_pct     DECIMAL(6,3)  NULL,
                    divergence_signal   TINYINT       NULL,
                    regime_sox          VARCHAR(10)   NULL,
                    regime_foreign_oi   VARCHAR(10)   NULL,
                    workflow_cost_usd   DECIMAL(10,6) NULL,
                    created_at          TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_se_trade_date (trade_date),
                    INDEX idx_se_run     (run_id),
                    INDEX idx_se_created (created_at)
                )
            """))
    except Exception as exc:
        logger.warning(f"[migration] ensure_session_episodes_table failed: {exc}")


def log_session_episode(
    run_id: str,
    trade_date: date,
    brief_id: Optional[int] = None,
    predicted_direction: Optional[str] = None,
    predicted_gap_pct: Optional[float] = None,
    foreign_oi_net: Optional[int] = None,
    trust_oi_net: Optional[int] = None,
    dealer_oi_net: Optional[int] = None,
    djia_chg_pct: Optional[float] = None,
    ndx_chg_pct: Optional[float] = None,
    sox_chg_pct: Optional[float] = None,
    tsm_adr_chg_pct: Optional[float] = None,
    divergence_signal: Optional[bool] = None,
    workflow_cost_usd: Optional[float] = None,
) -> None:
    """Upsert one session episode. actual_* fields backfilled later by backtest_agent."""
    regime_sox = (
        "strong" if (sox_chg_pct or 0) > 1.0 else
        "weak"   if (sox_chg_pct or 0) < -1.0 else "neutral"
    )
    regime_foreign_oi = (
        "bearish" if (foreign_oi_net or 0) < -10000 else
        "bullish" if (foreign_oi_net or 0) > 0 else "neutral"
    )
    try:
        with _engine().begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO session_episodes
                        (run_id, trade_date, brief_id, predicted_direction, predicted_gap_pct,
                         foreign_oi_net, trust_oi_net, dealer_oi_net,
                         djia_chg_pct, ndx_chg_pct, sox_chg_pct, tsm_adr_chg_pct,
                         divergence_signal, regime_sox, regime_foreign_oi, workflow_cost_usd)
                    VALUES
                        (:run_id, :td, :brief_id, :pred_dir, :pred_gap,
                         :foreign, :trust, :dealer,
                         :djia, :ndx, :sox, :tsm,
                         :div, :regime_sox, :regime_foi, :cost)
                    ON DUPLICATE KEY UPDATE
                        run_id              = VALUES(run_id),
                        brief_id            = VALUES(brief_id),
                        predicted_direction = VALUES(predicted_direction),
                        predicted_gap_pct   = VALUES(predicted_gap_pct),
                        foreign_oi_net      = VALUES(foreign_oi_net),
                        trust_oi_net        = VALUES(trust_oi_net),
                        dealer_oi_net       = VALUES(dealer_oi_net),
                        djia_chg_pct        = VALUES(djia_chg_pct),
                        ndx_chg_pct         = VALUES(ndx_chg_pct),
                        sox_chg_pct         = VALUES(sox_chg_pct),
                        tsm_adr_chg_pct     = VALUES(tsm_adr_chg_pct),
                        divergence_signal   = VALUES(divergence_signal),
                        regime_sox          = VALUES(regime_sox),
                        regime_foreign_oi   = VALUES(regime_foreign_oi),
                        workflow_cost_usd   = VALUES(workflow_cost_usd)
                """),
                {"run_id": run_id, "td": trade_date, "brief_id": brief_id,
                 "pred_dir": predicted_direction, "pred_gap": predicted_gap_pct,
                 "foreign": foreign_oi_net, "trust": trust_oi_net, "dealer": dealer_oi_net,
                 "djia": djia_chg_pct, "ndx": ndx_chg_pct,
                 "sox": sox_chg_pct, "tsm": tsm_adr_chg_pct,
                 "div": int(divergence_signal) if divergence_signal is not None else None,
                 "regime_sox": regime_sox, "regime_foi": regime_foreign_oi,
                 "cost": workflow_cost_usd},
            )
    except Exception as exc:
        logger.warning(f"[telemetry] log_session_episode failed: {exc}")


def backfill_session_episode_actuals(
    trade_date: date,
    actual_direction: Optional[str],
    actual_gap_pct: Optional[float],
    direction_correct: Optional[int],
) -> bool:
    """UPDATE session_episodes actual_* fields for a given trade_date. Fail-silent."""
    try:
        with _engine().begin() as conn:
            result = conn.execute(
                text("""
                    UPDATE session_episodes
                    SET actual_direction  = :ad,
                        actual_gap_pct   = :ag,
                        direction_correct = :dc
                    WHERE trade_date = :td
                """),
                {"ad": actual_direction, "ag": actual_gap_pct, "dc": direction_correct, "td": trade_date},
            )
        updated = result.rowcount
        if updated > 0:
            logger.debug(f"[backfill] session_episodes actual_* updated for {trade_date}")
        else:
            logger.debug(f"[backfill] no session_episodes row for {trade_date} (workflow may not have run)")
        return updated > 0
    except Exception as exc:
        logger.warning(f"[backfill] backfill_session_episode_actuals failed: {exc}")
        return False


# ── Evaluation Framework ────────────────────────────────────────────────────────

def ensure_eval_tables() -> None:
    """Create eval_runs and eval_results tables. Called by ensure_observability_tables()."""
    try:
        with _engine().begin() as conn:
            conn.execute(text("""
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
                )
            """))
            conn.execute(text("""
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
                )
            """))
    except Exception as exc:
        logger.warning(f"[migration] ensure_eval_tables failed: {exc}")


def create_eval_run(
    trade_date: date,
    run_id_ref: Optional[str] = None,
    triggered_by: str = "manual",
    brief_quality_score: Optional[float] = None,
    direction_correct: Optional[int] = None,
    predicted_direction: Optional[str] = None,
    actual_direction: Optional[str] = None,
    status: str = "success",
) -> int:
    """Upsert one eval_run row (UNIQUE on trade_date). Returns lastrowid or -1 on error."""
    try:
        with _engine().begin() as conn:
            result = conn.execute(
                text("""
                    INSERT INTO eval_runs
                        (trade_date, run_id_ref, triggered_by, status,
                         brief_quality_score, direction_correct,
                         predicted_direction, actual_direction, completed_at)
                    VALUES
                        (:td, :rid, :tby, :status, :bqs, :dc, :pd, :ad, NOW())
                    ON DUPLICATE KEY UPDATE
                        run_id_ref          = VALUES(run_id_ref),
                        triggered_by        = VALUES(triggered_by),
                        status              = VALUES(status),
                        brief_quality_score = VALUES(brief_quality_score),
                        direction_correct   = VALUES(direction_correct),
                        predicted_direction = VALUES(predicted_direction),
                        actual_direction    = VALUES(actual_direction),
                        completed_at        = NOW()
                """),
                {"td": trade_date, "rid": run_id_ref, "tby": triggered_by,
                 "status": status, "bqs": brief_quality_score, "dc": direction_correct,
                 "pd": predicted_direction, "ad": actual_direction},
            )
            # For ON DUPLICATE KEY UPDATE, lastrowid returns the existing PK
            row_id = result.lastrowid
            if row_id == 0:
                # Fetch the existing id
                row = conn.execute(
                    text("SELECT id FROM eval_runs WHERE trade_date = :td"),
                    {"td": trade_date},
                ).fetchone()
                return int(row[0]) if row else -1
            return int(row_id)
    except Exception as exc:
        logger.warning(f"[eval] create_eval_run failed: {exc}")
        return -1


def save_eval_result(
    eval_run_id: int,
    trade_date: date,
    agent_name: str,
    quality_score: Optional[float] = None,
    schema_valid: Optional[bool] = None,
    missing_fields: Optional[list] = None,
    hallucination_flags: Optional[list] = None,
    extra_metrics: Optional[dict] = None,
) -> None:
    """Insert one eval_result row. Fail-silent."""
    import json as _json
    try:
        sv = int(schema_valid) if schema_valid is not None else None
        with _engine().begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO eval_results
                        (eval_run_id, trade_date, agent_name, quality_score,
                         schema_valid, missing_fields, hallucination_flags, extra_metrics)
                    VALUES
                        (:erid, :td, :agent, :qs, :sv, :mf, :hf, :em)
                """),
                {"erid": eval_run_id, "td": trade_date, "agent": agent_name,
                 "qs": quality_score, "sv": sv,
                 "mf": _json.dumps(missing_fields or [], ensure_ascii=False),
                 "hf": _json.dumps(hallucination_flags or [], ensure_ascii=False),
                 "em": _json.dumps(extra_metrics or {}, ensure_ascii=False)},
            )
    except Exception as exc:
        logger.warning(f"[eval] save_eval_result failed ({agent_name}): {exc}")


def get_eval_runs(days: int = 30) -> list:
    """Return eval_runs rows ordered by trade_date DESC, up to 50 rows."""
    try:
        with _engine().connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT id, trade_date, run_id_ref, triggered_by, status,
                           brief_quality_score, direction_correct,
                           predicted_direction, actual_direction, completed_at
                    FROM eval_runs
                    WHERE trade_date >= CURDATE() - INTERVAL :days DAY
                    ORDER BY trade_date DESC
                    LIMIT 50
                """),
                {"days": days},
            ).fetchall()
            return [dict(r._mapping) for r in rows]
    except Exception as exc:
        logger.warning(f"[eval] get_eval_runs failed: {exc}")
        return []


def get_eval_results(eval_run_ids: list) -> list:
    """Return eval_results rows for the given eval_run_id list."""
    if not eval_run_ids:
        return []
    try:
        placeholders = ",".join(f":id{i}" for i in range(len(eval_run_ids)))
        params = {f"id{i}": v for i, v in enumerate(eval_run_ids)}
        with _engine().connect() as conn:
            rows = conn.execute(
                text(f"""
                    SELECT eval_run_id, trade_date, agent_name,
                           quality_score, schema_valid,
                           missing_fields, hallucination_flags, extra_metrics
                    FROM eval_results
                    WHERE eval_run_id IN ({placeholders})
                    ORDER BY eval_run_id, agent_name
                """),
                params,
            ).fetchall()
            return [dict(r._mapping) for r in rows]
    except Exception as exc:
        logger.warning(f"[eval] get_eval_results failed: {exc}")
        return []


def get_eval_dashboard_kpis(days: int = 30) -> dict:
    """Return KPI aggregates for the Evaluation dashboard tab."""
    try:
        with _engine().connect() as conn:
            row = conn.execute(
                text("""
                    SELECT
                        COUNT(*)                              AS eval_count,
                        AVG(brief_quality_score)              AS avg_quality_score,
                        SUM(CASE WHEN direction_correct IS NOT NULL THEN 1 ELSE 0 END) AS dc_total,
                        SUM(COALESCE(direction_correct, 0))   AS dc_correct
                    FROM eval_runs
                    WHERE trade_date >= CURDATE() - INTERVAL :days DAY
                      AND status = 'success'
                """),
                {"days": days},
            ).fetchone()

            eval_count = int(row[0] or 0)
            avg_quality = float(row[1] or 0.0)
            dc_total = int(row[2] or 0)
            dc_correct = int(row[3] or 0)
            direction_pct = (dc_correct / dc_total * 100) if dc_total > 0 else 0.0

            # Schema pass rate: AVG across all agents
            schema_row = conn.execute(
                text("""
                    SELECT AVG(COALESCE(schema_valid, 0)) AS schema_pass_rate
                    FROM eval_results evr
                    JOIN eval_runs er ON evr.eval_run_id = er.id
                    WHERE er.trade_date >= CURDATE() - INTERVAL :days DAY
                      AND er.status = 'success'
                """),
                {"days": days},
            ).fetchone()
            schema_pass = float(schema_row[0] or 0.0) * 100 if schema_row else 0.0

        return {
            "eval_count": eval_count,
            "avg_quality_score": round(avg_quality, 1),
            "direction_accuracy_pct": round(direction_pct, 1),
            "schema_pass_rate": round(schema_pass, 1),
        }
    except Exception as exc:
        logger.warning(f"[eval] get_eval_dashboard_kpis failed: {exc}")
        return {}


def get_eval_agent_avg_scores(days: int = 20) -> list:
    """Return per-agent average quality scores over recent eval_runs."""
    try:
        with _engine().connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT evr.agent_name, AVG(evr.quality_score) AS avg_score
                    FROM eval_results evr
                    JOIN eval_runs er ON evr.eval_run_id = er.id
                    WHERE er.trade_date >= CURDATE() - INTERVAL :days DAY
                      AND er.status = 'success'
                    GROUP BY evr.agent_name
                    ORDER BY avg_score DESC
                """),
                {"days": days},
            ).fetchall()
            return [{"agent_name": r[0], "avg_score": round(float(r[1] or 0), 1)} for r in rows]
    except Exception as exc:
        logger.warning(f"[eval] get_eval_agent_avg_scores failed: {exc}")
        return []


def get_session_episode(trade_date: date) -> Optional[dict]:
    """Return one session_episodes row for the given trade_date, or None."""
    try:
        with _engine().connect() as conn:
            row = conn.execute(
                text("SELECT * FROM session_episodes WHERE trade_date = :td"),
                {"td": trade_date},
            ).fetchone()
            return dict(row._mapping) if row else None
    except Exception as exc:
        logger.warning(f"[eval] get_session_episode failed: {exc}")
        return None


# ── Adaptive Flywheel Phase 1: strategy_lessons ────────────────────────────────

def ensure_strategy_lessons_table() -> None:
    """Create strategy_lessons table. Called by ensure_observability_tables()."""
    try:
        with _engine().begin() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS strategy_lessons (
                    id                   BIGINT        AUTO_INCREMENT PRIMARY KEY,
                    trade_date           DATE          NOT NULL,
                    eval_run_id          BIGINT        NULL,
                    error_type           VARCHAR(30)   NOT NULL,
                    lesson_text          TEXT          NOT NULL,
                    direction_correct    TINYINT       NOT NULL DEFAULT 0,
                    predicted_direction  VARCHAR(10)   NULL,
                    actual_direction     VARCHAR(10)   NULL,
                    predicted_gap_pct    DECIMAL(6,3)  NULL,
                    actual_gap_pct       DECIMAL(6,3)  NULL,
                    gap_error_abs        DECIMAL(6,3)  NULL,
                    composite_score      DECIMAL(5,2)  NULL,
                    regime_sox           VARCHAR(10)   NULL,
                    regime_foreign_oi    VARCHAR(10)   NULL,
                    divergence_signal    TINYINT       NULL,
                    lesson_quality_score DECIMAL(3,1)  NULL,
                    is_active            TINYINT       NOT NULL DEFAULT 1,
                    expires_at           DATE          NULL,
                    created_at           TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_sl_trade_date  (trade_date),
                    INDEX idx_sl_error_type      (error_type),
                    INDEX idx_sl_regime          (regime_sox, regime_foreign_oi),
                    INDEX idx_sl_active_date     (is_active, trade_date DESC)
                )
            """))
            # Add column to pre-existing tables that lack it
            conn.execute(text(
                "ALTER TABLE strategy_lessons"
                " ADD COLUMN IF NOT EXISTS lesson_quality_score DECIMAL(3,1) NULL"
            ))
    except Exception as exc:
        logger.warning(f"[migration] ensure_strategy_lessons_table failed: {exc}")


def save_strategy_lesson(
    trade_date: date,
    error_type: str,
    lesson_text: str,
    direction_correct: int = 0,
    predicted_direction: Optional[str] = None,
    actual_direction: Optional[str] = None,
    predicted_gap_pct: Optional[float] = None,
    actual_gap_pct: Optional[float] = None,
    gap_error_abs: Optional[float] = None,
    composite_score: Optional[float] = None,
    regime_sox: Optional[str] = None,
    regime_foreign_oi: Optional[str] = None,
    divergence_signal: Optional[int] = None,
    eval_run_id: Optional[int] = None,
    lesson_quality_score: Optional[float] = None,
) -> None:
    """Upsert one strategy_lessons row. Expires after 90 days. Fail-silent."""
    try:
        with _engine().begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO strategy_lessons
                        (trade_date, eval_run_id, error_type, lesson_text,
                         direction_correct, predicted_direction, actual_direction,
                         predicted_gap_pct, actual_gap_pct, gap_error_abs,
                         composite_score, regime_sox, regime_foreign_oi,
                         divergence_signal, lesson_quality_score, is_active,
                         expires_at)
                    VALUES
                        (:td, :erid, :etype, :lesson,
                         :dc, :pd, :ad,
                         :pgap, :agap, :gerr,
                         :score, :rsox, :rfoi,
                         :div, :lqs, 1,
                         DATE_ADD(:td, INTERVAL 90 DAY))
                    ON DUPLICATE KEY UPDATE
                        eval_run_id          = VALUES(eval_run_id),
                        error_type           = VALUES(error_type),
                        lesson_text          = VALUES(lesson_text),
                        direction_correct    = VALUES(direction_correct),
                        predicted_direction  = VALUES(predicted_direction),
                        actual_direction     = VALUES(actual_direction),
                        predicted_gap_pct    = VALUES(predicted_gap_pct),
                        actual_gap_pct       = VALUES(actual_gap_pct),
                        gap_error_abs        = VALUES(gap_error_abs),
                        composite_score      = VALUES(composite_score),
                        regime_sox           = VALUES(regime_sox),
                        regime_foreign_oi    = VALUES(regime_foreign_oi),
                        divergence_signal    = VALUES(divergence_signal),
                        lesson_quality_score = VALUES(lesson_quality_score),
                        is_active            = 1,
                        expires_at           = DATE_ADD(VALUES(trade_date), INTERVAL 90 DAY)
                """),
                {"td": trade_date, "erid": eval_run_id, "etype": error_type,
                 "lesson": lesson_text, "dc": direction_correct,
                 "pd": predicted_direction, "ad": actual_direction,
                 "pgap": predicted_gap_pct, "agap": actual_gap_pct,
                 "gerr": gap_error_abs, "score": composite_score,
                 "rsox": regime_sox, "rfoi": regime_foreign_oi,
                 "div": divergence_signal, "lqs": lesson_quality_score},
            )
    except Exception as exc:
        logger.warning(f"[flywheel] save_strategy_lesson failed: {exc}")


def get_relevant_lessons(
    regime_sox: Optional[str],
    regime_foreign_oi: Optional[str],
    divergence_signal: Optional[int],
    limit: int = 3,
) -> list:
    """Return strategy_lessons ranked by regime similarity + recency + error type."""
    try:
        with _engine().connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT trade_date, error_type, lesson_text,
                           direction_correct, predicted_direction, actual_direction,
                           predicted_gap_pct, actual_gap_pct, gap_error_abs,
                           regime_sox, regime_foreign_oi, divergence_signal,
                           lesson_quality_score,
                           (CASE WHEN regime_sox       = :rsox THEN 20 ELSE 0 END +
                            CASE WHEN regime_foreign_oi = :rfoi THEN 15 ELSE 0 END +
                            CASE WHEN divergence_signal = :div  THEN 10 ELSE 0 END +
                            CASE WHEN DATEDIFF(CURDATE(), trade_date) <= 7  THEN 25 ELSE 0 END +
                            CASE WHEN DATEDIFF(CURDATE(), trade_date) <= 30 THEN 15 ELSE 0 END +
                            CASE WHEN error_type IN ('direction_error','overconfidence_error')
                                 THEN 10 ELSE 0 END +
                            CASE WHEN lesson_quality_score >= 4.0 THEN 5 ELSE 0 END
                           ) AS relevance
                    FROM strategy_lessons
                    WHERE is_active = 1
                      AND (expires_at IS NULL OR expires_at >= CURDATE())
                    ORDER BY relevance DESC, trade_date DESC
                    LIMIT :lim
                """),
                {"rsox": regime_sox, "rfoi": regime_foreign_oi,
                 "div": divergence_signal, "lim": limit},
            ).fetchall()
            return [dict(r._mapping) for r in rows]
    except Exception as exc:
        logger.warning(f"[flywheel] get_relevant_lessons failed: {exc}")
        return []


def cleanup_expired_lessons() -> int:
    """Set is_active=0 for lessons past expires_at. Returns affected row count."""
    try:
        with _engine().begin() as conn:
            result = conn.execute(
                text("""
                    UPDATE strategy_lessons
                    SET is_active = 0
                    WHERE expires_at IS NOT NULL AND expires_at < CURDATE()
                      AND is_active = 1
                """)
            )
            return result.rowcount
    except Exception as exc:
        logger.warning(f"[flywheel] cleanup_expired_lessons failed: {exc}")
        return 0


def get_flywheel_stats() -> dict:
    """Return active lesson count total and breakdown by error_type."""
    try:
        with _engine().connect() as conn:
            total = conn.execute(text(
                "SELECT COUNT(*) FROM strategy_lessons"
                " WHERE is_active=1 AND (expires_at IS NULL OR expires_at >= CURDATE())"
            )).scalar()
            rows = conn.execute(text(
                "SELECT error_type, COUNT(*) AS cnt FROM strategy_lessons"
                " WHERE is_active=1 AND (expires_at IS NULL OR expires_at >= CURDATE())"
                " GROUP BY error_type ORDER BY cnt DESC"
            )).fetchall()
            return {
                "total": int(total or 0),
                "by_error_type": [{"error_type": r[0], "count": int(r[1])} for r in rows],
            }
    except Exception as exc:
        logger.warning(f"[flywheel] get_flywheel_stats failed: {exc}")
        return {"total": 0, "by_error_type": []}


def get_recent_lessons(limit: int = 14) -> list[dict]:
    """Return most recent active strategy lessons ordered by trade_date DESC."""
    try:
        with _engine().connect() as conn:
            rows = conn.execute(text("""
                SELECT trade_date, error_type, direction_correct,
                       regime_sox, regime_foreign_oi, composite_score,
                       lesson_quality_score,
                       LEFT(lesson_text, 150) AS lesson_preview,
                       expires_at
                FROM strategy_lessons
                WHERE is_active = 1
                  AND (expires_at IS NULL OR expires_at >= CURDATE())
                ORDER BY trade_date DESC
                LIMIT :limit
            """), {"limit": limit}).fetchall()
            cols = ["trade_date", "error_type", "direction_correct",
                    "regime_sox", "regime_foreign_oi", "composite_score",
                    "lesson_quality_score", "lesson_preview", "expires_at"]
            return [dict(zip(cols, r)) for r in rows]
    except Exception as exc:
        logger.warning(f"[flywheel] get_recent_lessons failed: {exc}")
        return []


# ── Stock info (code → company name) ───────────────────────────────────────────

def ensure_stock_info_table() -> None:
    """Create stock_info table. Called by ensure_observability_tables()."""
    try:
        with _engine().begin() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS stock_info (
                    stock_id     VARCHAR(20)   NOT NULL,
                    company_name VARCHAR(100)  NOT NULL,
                    market       VARCHAR(10)   NULL,
                    last_synced  TIMESTAMP     DEFAULT CURRENT_TIMESTAMP
                                              ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (stock_id)
                )
            """))
    except Exception as exc:
        logger.warning(f"[migration] ensure_stock_info_table failed: {exc}")


def upsert_stock_info(stock_id: str, company_name: str, market: Optional[str] = None) -> None:
    """Insert or update a stock_id → company_name mapping. Fail-silent."""
    try:
        with _engine().begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO stock_info (stock_id, company_name, market)
                    VALUES (:sid, :name, :mkt)
                    ON DUPLICATE KEY UPDATE
                        company_name = VALUES(company_name),
                        market       = VALUES(market),
                        last_synced  = CURRENT_TIMESTAMP
                """),
                {"sid": stock_id, "name": company_name, "mkt": market},
            )
    except Exception as exc:
        logger.warning(f"[stock_info] upsert_stock_info failed for {stock_id}: {exc}")


def get_stock_name(stock_id: str) -> Optional[str]:
    """Return company_name for stock_id, or None if not found."""
    try:
        with _engine().connect() as conn:
            row = conn.execute(
                text("SELECT company_name FROM stock_info WHERE stock_id = :sid"),
                {"sid": stock_id},
            ).fetchone()
        return row[0] if row else None
    except Exception as exc:
        logger.warning(f"[stock_info] get_stock_name failed for {stock_id}: {exc}")
        return None


def get_portfolio_missing_names() -> list:
    """Return stock_ids in user_portfolio that have no entry in stock_info."""
    try:
        with _engine().connect() as conn:
            rows = conn.execute(text("""
                SELECT DISTINCT p.stock_id
                FROM user_portfolio p
                LEFT JOIN stock_info s ON s.stock_id = p.stock_id
                WHERE s.stock_id IS NULL
                ORDER BY p.stock_id
            """)).fetchall()
        return [r[0] for r in rows]
    except Exception as exc:
        logger.warning(f"[stock_info] get_portfolio_missing_names failed: {exc}")
        return []
