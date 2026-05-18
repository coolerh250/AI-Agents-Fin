"""
utils/mcp_env.py
Per-server env builders for StdioServerParameters.
Each server receives only the credentials it needs — no full parent env inheritance.
"""
import os


def _base_env() -> dict:
    env = {k: os.environ[k] for k in ["PATH", "HOME", "LANG", "VIRTUAL_ENV", "UV_CACHE_DIR"]
           if k in os.environ}
    # Ensure ~/.local/bin is in PATH so `uv` is findable in MCP subprocesses
    home = os.environ.get("HOME", "")
    local_bin = f"{home}/.local/bin" if home else ""
    if local_bin and local_bin not in env.get("PATH", ""):
        env["PATH"] = local_bin + ":" + env.get("PATH", "")
    return env


def market_data_env() -> dict:
    return _base_env()


def persistence_env() -> dict:
    required = ["TIDB_HOST", "TIDB_PORT", "TIDB_USER", "TIDB_PASSWORD",
                "TIDB_DB", "MCP_WRITE_TOKEN"]
    return {**_base_env(), **{k: os.environ.get(k, "") for k in required}}


def notification_env() -> dict:
    required = ["LINE_CHANNEL_ACCESS_TOKEN", "LINE_USER_ID",
                "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "MCP_NOTIFY_TOKEN",
                "TIDB_HOST", "TIDB_PORT", "TIDB_USER", "TIDB_PASSWORD", "TIDB_DB"]
    return {**_base_env(), **{k: os.environ.get(k, "") for k in required}}


def system_env() -> dict:
    return _base_env()


def _env_for_server(server_script: str) -> dict:
    if "market_data" in server_script:
        return market_data_env()
    if "persistence" in server_script:
        return persistence_env()
    if "notification" in server_script:
        return notification_env()
    if "system" in server_script:
        return system_env()
    return {}
