"""
mcp_servers/persistence_server.py
Persistence MCP Server — TiDB read/write with auth enforcement
Tools: save_brief (write) | save_actual (write) | get_brief (read) | get_recent_accuracy (read)
Env: TIDB_* + MCP_WRITE_TOKEN
"""
import os
import sys
from datetime import date as _date
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from loguru import logger
from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

from utils.mcp_audit import audit_tool

logger.remove()
logger.add(sys.stderr, format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}", level="DEBUG")

mcp = FastMCP("persistence")
logger.info("Persistence MCP Server initializing")

WRITE_TOKEN = os.getenv("MCP_WRITE_TOKEN", "")


def _check_write_auth(key: str) -> bool:
    return bool(WRITE_TOKEN) and key == WRITE_TOKEN


def _log_unauthorized(tool_name: str) -> None:
    logger.warning(f"[persistence] unauthorized call to {tool_name}")
    try:
        from utils.mcp_audit import _write_audit
        _write_audit("persistence", tool_name, "", "unauthorized", 0, None)
    except Exception:
        pass


@mcp.tool()
def save_brief(
    trade_date: str,
    brief_text: str,
    predicted_gap_pct: Optional[float] = None,
    gap_direction: Optional[str] = None,
    api_key: str = "",
) -> dict:
    """Save daily investment brief to TiDB daily_briefs. Upserts on trade_date.
    Requires MCP_WRITE_TOKEN in api_key.
    Returns: {"success": bool, "row_id": int, "error": str|None}"""
    if not _check_write_auth(api_key):
        _log_unauthorized("save_brief")
        return {"success": False, "row_id": 0, "error": "unauthorized"}
    try:
        with audit_tool("persistence", "save_brief"):
            from database_tools import save_brief as _save_brief
            d = _date.fromisoformat(trade_date)
            row_id = _save_brief(d, brief_text, predicted_gap_pct, gap_direction)
            logger.success(f"[save_brief] row_id={row_id} trade_date={trade_date}")
            return {"success": True, "row_id": row_id, "error": None}
    except Exception as exc:
        logger.error(f"[save_brief] Failed: {exc}")
        return {"success": False, "row_id": 0, "error": str(exc)}


@mcp.tool()
def save_actual(
    trade_date: str,
    open_price: float,
    close_price: float,
    actual_gap_pct: float,
    notes: str = "",
    api_key: str = "",
) -> dict:
    """Upsert TAIEX actuals to market_actuals. Requires MCP_WRITE_TOKEN.
    Phase 1 stub — consumer wiring (backtest_agent.py) is Phase 2."""
    if not _check_write_auth(api_key):
        _log_unauthorized("save_actual")
        return {"success": False, "error": "unauthorized"}
    try:
        with audit_tool("persistence", "save_actual"):
            from database_tools import save_actual as _save_actual
            d = _date.fromisoformat(trade_date)
            _save_actual(d, open_price, close_price, actual_gap_pct, notes)
            logger.success(f"[save_actual] written trade_date={trade_date}")
            return {"success": True, "error": None}
    except Exception as exc:
        logger.error(f"[save_actual] Failed: {exc}")
        return {"success": False, "error": str(exc)}


@mcp.tool()
def get_brief(trade_date: str) -> dict:
    """Fetch most recent daily brief for trade_date (YYYY-MM-DD).
    Returns the full brief row or {"found": false} if no record."""
    try:
        from database_tools import get_brief as _get_brief
        d = _date.fromisoformat(trade_date)
        record = _get_brief(d)
        if record is None:
            return {"found": False}
        record["trade_date"] = str(record["trade_date"])
        return {"found": True, **record}
    except Exception as exc:
        logger.error(f"[get_brief] Failed: {exc}")
        return {"found": False, "error": str(exc)}


@mcp.tool()
def get_recent_accuracy(days: int = 7) -> list:
    """Return last N trade days joining daily_briefs + market_actuals.
    Each entry includes trade_date, predicted_gap_pct, actual_gap_pct, direction_correct."""
    try:
        from database_tools import get_recent_accuracy as _get_recent
        rows = _get_recent(days)
        for r in rows:
            if "trade_date" in r:
                r["trade_date"] = str(r["trade_date"])
        return rows
    except Exception as exc:
        logger.error(f"[get_recent_accuracy] Failed: {exc}")
        return []


if __name__ == "__main__":
    logger.info("Starting Persistence MCP Server via stdio transport")
    mcp.run(transport="stdio")
