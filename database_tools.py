"""
database_tools.py
SQLAlchemy/PyMySQL helpers for agent_memory on TiDB.
"""
import os
from datetime import date
from functools import lru_cache
from typing import Optional

from dotenv import load_dotenv
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
                estimated_cost_usd DECIMAL(10,6)  NOT NULL DEFAULT 0.000000,
                latency_ms         INT,
                logged_at          TIMESTAMP      DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_agent (agent_name),
                INDEX idx_logged_at (logged_at)
            )
        """))


def log_cost(
    agent_name: str,
    model_name: str,
    input_tokens: int,
    output_tokens: int,
    estimated_cost_usd: float,
    latency_ms: Optional[int] = None,
) -> None:
    with _engine().begin() as conn:
        conn.execute(
            text("""
                INSERT INTO cost_logs
                    (agent_name, model_name, input_tokens, output_tokens,
                     estimated_cost_usd, latency_ms)
                VALUES (:agent, :model, :in_tok, :out_tok, :cost, :lat)
            """),
            {"agent": agent_name, "model": model_name,
             "in_tok": input_tokens, "out_tok": output_tokens,
             "cost": estimated_cost_usd, "lat": latency_ms},
        )


def get_cost_summary(days: int = 30) -> list[dict]:
    sql = """
        SELECT agent_name, model_name,
               SUM(input_tokens)       AS total_input,
               SUM(output_tokens)      AS total_output,
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
        conn.execute(
            text("""
                DELETE FROM user_portfolio
                WHERE id = :id
                  AND (line_user_id = :uid OR (:uid IS NULL AND line_user_id IS NULL))
            """),
            {"id": item_id, "uid": user_id},
        )


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
