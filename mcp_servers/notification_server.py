"""
mcp_servers/notification_server.py
Notification MCP Server — LINE/Telegram push with dedup, auth, and audit
Tools: push_investment_brief | push_raw
Env: LINE_* + TELEGRAM_* + MCP_NOTIFY_TOKEN + TIDB_* (read-only for dedup)
"""
import os
import sys
from datetime import date as _date
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger
from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

from utils.mcp_audit import audit_tool, _get_audit_engine

logger.remove()
logger.add(sys.stderr, format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}", level="DEBUG")

mcp = FastMCP("notification")
logger.info("Notification MCP Server initializing")

NOTIFY_TOKEN = os.getenv("MCP_NOTIFY_TOKEN", "")


def _check_notify_auth(key: str) -> bool:
    return bool(NOTIFY_TOKEN) and key == NOTIFY_TOKEN


def _log_unauthorized(tool_name: str) -> None:
    logger.warning(f"[notification] unauthorized call to {tool_name}")
    try:
        from utils.mcp_audit import _write_audit
        _write_audit("notification", tool_name, "", "unauthorized", 0, None)
    except Exception:
        pass


def _already_sent_today() -> bool:
    engine = _get_audit_engine()
    if engine is None:
        return False
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            count = conn.execute(text("""
                SELECT COUNT(*) FROM tool_audit_log
                WHERE tool_id  = 'notification.push_investment_brief'
                  AND status   = 'ok'
                  AND DATE(created_at) = :d
            """), {"d": str(_date.today())}).scalar()
        return (count or 0) > 0
    except Exception as exc:
        logger.warning(f"[notification] dedup check failed (assuming not sent): {exc}")
        return False


@mcp.tool()
def push_investment_brief(brief_text: str, api_key: str = "") -> dict:
    """Format and push investment brief to all configured channels.
    Deduplicates: skips if already sent today (checked via tool_audit_log).
    Message truncated to 4000 chars. Requires MCP_NOTIFY_TOKEN in api_key.
    Returns: {"line": {...}, "telegram": {...}, "dedup_skipped": bool}"""
    if not _check_notify_auth(api_key):
        _log_unauthorized("push_investment_brief")
        return {"line": {"status": "error"}, "telegram": {"status": "error"},
                "error": "unauthorized", "dedup_skipped": False}

    if _already_sent_today():
        logger.info("[notification] Already sent today — dedup skipped")
        return {"line":     {"status": "skipped", "reason": "dedup"},
                "telegram": {"status": "skipped", "reason": "dedup"},
                "dedup_skipped": True}

    try:
        from messenger_tools import format_brief, send_line, send_telegram
        message = format_brief(brief_text)
        if len(message) > 4000:
            message = message[:4000]

        with audit_tool("notification", "push_investment_brief"):
            line_result = send_line(message)
            tg_result   = send_telegram(message)

        logger.success("[notification] Brief pushed successfully")
        return {"line": line_result, "telegram": tg_result, "dedup_skipped": False}
    except Exception as exc:
        logger.error(f"[push_investment_brief] Failed: {exc}")
        return {"line": {"status": "error", "detail": str(exc)},
                "telegram": {"status": "error", "detail": str(exc)},
                "error": str(exc), "dedup_skipped": False}


@mcp.tool()
def push_raw(channel: str, message: str, api_key: str = "") -> dict:
    """Push raw text to a single channel (line|telegram). For manual use and testing only.
    Sent as-is — no format_brief transformation. Max 4000 chars. Requires MCP_NOTIFY_TOKEN."""
    if not _check_notify_auth(api_key):
        _log_unauthorized("push_raw")
        return {"status": "error", "error": "unauthorized"}
    if channel not in ("line", "telegram"):
        return {"status": "error", "error": f"unknown channel: {channel}"}

    msg = message[:4000]
    try:
        with audit_tool("notification", "push_raw"):
            from messenger_tools import send_line, send_telegram
            result = send_line(msg) if channel == "line" else send_telegram(msg)
        logger.success(f"[push_raw] Sent to {channel}")
        return result
    except Exception as exc:
        logger.error(f"[push_raw] Failed: {exc}")
        return {"status": "error", "error": str(exc)}


if __name__ == "__main__":
    logger.info("Starting Notification MCP Server via stdio transport")
    mcp.run(transport="stdio")
