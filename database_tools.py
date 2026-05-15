"""
database_tools.py
SQLAlchemy/PyMySQL helpers for agent_memory on TiDB.
"""
import os
from datetime import date
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

load_dotenv()


def _engine() -> Engine:
    host = os.getenv("TIDB_HOST", "127.0.0.1")
    port = os.getenv("TIDB_PORT", "4000")
    user = os.getenv("TIDB_USER", "root")
    password = os.getenv("TIDB_PASSWORD", "")
    db = os.getenv("TIDB_DB", "agent_memory")
    url = f"mysql+pymysql://{user}:{password}@{host}:{port}/{db}?charset=utf8mb4"
    return create_engine(url, pool_pre_ping=True)


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
