"""deploy/bootstrap.py — single Python entrypoint for fresh-deploy init.

Replaces 4 inline `uv run python -c "..."` chains from the README:
  - `db-init`: ensure_cost_logs + ensure_observability + ensure_portfolio tables
  - `seed`:    seed_initial_profiles + seed_sentiment_curator
  - `snapshot`: produce market_snapshot.json via test_collection.py
  - `wait-db`: poll TiDB until SELECT 1 succeeds (avoids race after compose up)

All steps idempotent. Each is exposed as a subcommand for individual re-runs
from the Makefile (`make db-init`, `make seed`, ...).

Usage:
    uv run python deploy/bootstrap.py wait-db [--timeout 60]
    uv run python deploy/bootstrap.py db-init
    uv run python deploy/bootstrap.py seed
    uv run python deploy/bootstrap.py snapshot
    uv run python deploy/bootstrap.py all      # wait-db + db-init + seed + snapshot
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv

load_dotenv(_REPO_ROOT / ".env")


def _step(msg: str) -> None:
    print(f"\033[36m[bootstrap]\033[0m {msg}", flush=True)


def _ok(msg: str) -> None:
    print(f"\033[32m[bootstrap] OK\033[0m {msg}", flush=True)


def _fail(msg: str) -> int:
    print(f"\033[31m[bootstrap] FAIL\033[0m {msg}", flush=True)
    return 1


# ── wait-db ────────────────────────────────────────────────────────────────────

def wait_db(timeout: int = 60) -> int:
    """Poll TiDB on TIDB_HOST:TIDB_PORT until SELECT 1 succeeds."""
    from sqlalchemy import create_engine, text

    host = os.getenv("TIDB_HOST", "127.0.0.1")
    port = os.getenv("TIDB_PORT", "4000")
    user = os.getenv("TIDB_USER", "root")
    pw   = os.getenv("TIDB_PASSWORD", "")
    db   = os.getenv("TIDB_DB", "agent_memory")
    url  = f"mysql+pymysql://{user}:{pw}@{host}:{port}/?charset=utf8mb4"

    _step(f"polling {host}:{port} (timeout {timeout}s)")
    deadline = time.monotonic() + timeout
    last_err = ""
    while time.monotonic() < deadline:
        try:
            eng = create_engine(url, connect_args={"connect_timeout": 3})
            with eng.connect() as conn:
                conn.execute(text("SELECT 1"))
                conn.execute(text(f"CREATE DATABASE IF NOT EXISTS {db}"))
            _ok(f"TiDB reachable; database `{db}` ensured")
            return 0
        except Exception as exc:
            last_err = str(exc)[:120]
            time.sleep(2)
    return _fail(f"TiDB unreachable after {timeout}s — last error: {last_err}")


# ── db-init ────────────────────────────────────────────────────────────────────

def db_init() -> int:
    """Run all 19 ensure_*_table calls (transitively via ensure_observability_tables)."""
    _step("ensuring tables")
    try:
        from database_tools import (
            ensure_cost_logs_table,
            ensure_observability_tables,
            ensure_portfolio_table,
        )
        ensure_cost_logs_table()
        ensure_observability_tables()
        ensure_portfolio_table()
    except Exception as exc:
        return _fail(f"table creation failed: {exc}")
    _ok("schema bootstrap complete (idempotent)")
    return 0


# ── seed ───────────────────────────────────────────────────────────────────────

def seed() -> int:
    """Seed v1 active strategy profiles for all 6 pipeline agents + sentiment_curator."""
    _step("seeding agent_strategy_profiles")
    try:
        from strategy_profile import seed_initial_profiles, seed_sentiment_curator
        n_main = seed_initial_profiles()
        n_curator = seed_sentiment_curator()
    except Exception as exc:
        return _fail(f"seed failed: {exc}")
    _ok(f"seeded {n_main} main agents + {n_curator} sentiment_curator (existing rows skipped)")
    return 0


# ── snapshot ───────────────────────────────────────────────────────────────────

def snapshot() -> int:
    """Run test_collection.py to produce market_snapshot.json."""
    _step("running test_collection.py")
    out = _REPO_ROOT / "market_snapshot.json"
    rc = subprocess.run(
        [sys.executable, str(_REPO_ROOT / "test_collection.py")],
        cwd=str(_REPO_ROOT),
    ).returncode
    if rc != 0:
        return _fail(f"test_collection.py exit {rc}")
    if not out.exists():
        return _fail("test_collection.py succeeded but market_snapshot.json absent")
    _ok(f"market_snapshot.json written ({out.stat().st_size} bytes)")
    return 0


# ── all ────────────────────────────────────────────────────────────────────────

def run_all(timeout: int) -> int:
    for step in (lambda: wait_db(timeout), db_init, seed, snapshot):
        rc = step()
        if rc != 0:
            return rc
    return 0


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="AI Agent Studio bootstrap")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("wait-db").add_argument("--timeout", type=int, default=60)
    sub.add_parser("db-init")
    sub.add_parser("seed")
    sub.add_parser("snapshot")
    sub.add_parser("all").add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()

    if args.cmd == "wait-db":  return wait_db(args.timeout)
    if args.cmd == "db-init":  return db_init()
    if args.cmd == "seed":     return seed()
    if args.cmd == "snapshot": return snapshot()
    if args.cmd == "all":      return run_all(args.timeout)
    return 2


if __name__ == "__main__":
    sys.exit(main())
