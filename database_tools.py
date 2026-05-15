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
) -> int:
    """Insert a daily brief. Returns the new row id."""
    with _engine().begin() as conn:
        result = conn.execute(
            text("""
                INSERT INTO daily_briefs
                    (trade_date, brief_text, predicted_gap_pct, gap_direction)
                VALUES (:d, :brief, :gap_pct, :direction)
            """),
            {"d": trade_date, "brief": brief_text,
             "gap_pct": predicted_gap_pct, "direction": gap_direction},
        )
        return result.lastrowid


def get_brief(trade_date: date) -> Optional[dict]:
    """Fetch the most recent brief for a given trade date."""
    with _engine().connect() as conn:
        row = conn.execute(
            text("""
                SELECT id, trade_date, brief_text, predicted_gap_pct, gap_direction, created_at
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
    user_id: Optional[str] = None,
) -> None:
    with _engine().begin() as conn:
        before_row = conn.execute(
            text("SELECT * FROM user_portfolio WHERE id = :id"), {"id": item_id}
        ).fetchone()
        before = dict(before_row._mapping) if before_row else {}
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

    ensure_tool_audit_log_table()   # Phase 2
    ensure_session_episodes_table() # Phase 4

    # Migrate cost_logs: add thinking_tokens, run_id if missing
    for stmt, label in [
        ("ALTER TABLE cost_logs ADD COLUMN thinking_tokens INT NOT NULL DEFAULT 0", "add thinking_tokens"),
        ("ALTER TABLE cost_logs ADD COLUMN run_id VARCHAR(36) DEFAULT NULL", "add run_id"),
        ("ALTER TABLE cost_logs ADD INDEX idx_cost_run_id (run_id)", "add idx_cost_run_id"),
        ("ALTER TABLE daily_briefs ADD UNIQUE KEY uq_trade_date (trade_date)", "add uq_trade_date"),
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
