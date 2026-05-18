"""
utils/mcp_audit.py
Fail-silent audit context manager for MCP tool calls.
Writes to the existing tool_audit_log table (tool_id = "server.tool_name", tool_type = "mcp").
No-ops silently if TIDB_HOST is not set in the subprocess env (e.g. market_data_server).
"""
import contextlib
import os
import time
from typing import Optional

_audit_engine_instance = None


def _get_audit_engine():
    global _audit_engine_instance
    if _audit_engine_instance is None:
        host = os.environ.get("TIDB_HOST", "")
        if not host:
            return None
        from sqlalchemy import create_engine
        port = os.environ.get("TIDB_PORT", "4000")
        user = os.environ.get("TIDB_USER", "root")
        pw   = os.environ.get("TIDB_PASSWORD", "")
        db   = os.environ.get("TIDB_DB", "agent_memory")
        url  = f"mysql+pymysql://{user}:{pw}@{host}:{port}/{db}?charset=utf8mb4"
        _audit_engine_instance = create_engine(
            url, pool_size=1, max_overflow=0, pool_timeout=5,
            connect_args={"connect_timeout": 10},
        )
    return _audit_engine_instance


def audit_tool(server: str, tool_name: str, caller: str = ""):
    @contextlib.contextmanager
    def _ctx():
        t0 = time.monotonic()
        status, err = "ok", None
        try:
            yield
        except Exception as exc:
            status = "error"
            err = str(exc)[:500]
            raise
        finally:
            ms = int((time.monotonic() - t0) * 1000)
            try:
                _write_audit(server, tool_name, caller, status, ms, err)
            except Exception:
                pass
    return _ctx()


def _write_audit(server: str, tool_name: str, caller: str,
                 status: str, latency_ms: int, err: Optional[str]) -> None:
    engine = _get_audit_engine()
    if engine is None:
        return
    from sqlalchemy import text
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO tool_audit_log
                (tool_id, tool_type, caller, status, latency_ms, error_message)
            VALUES (:tid, 'mcp', :caller, :status, :lat, :err)
        """), {
            "tid":    f"{server}.{tool_name}",
            "caller": caller or None,
            "status": status,
            "lat":    latency_ms,
            "err":    err,
        })
        conn.commit()
