#!/usr/bin/env python
"""
scripts/promote_profile.py
CLI for managing agent_strategy_profiles versions:
    status    <agent>           — show active + shadow + recent shadow stats
    list      <agent>           — show every version row for the agent
    proposals <agent>           — show optimizer_proposals ledger for the agent
    promote   <agent> <version> — set <version> active (clears old active,
                                  clears shadow flag if it was the shadow)
    revert    <agent> <version> — same as promote; explicit "going back"
    shadow    <agent> <version> — mark <version> is_shadow=1 (clears old shadow)

All mutations run in a transaction and write an entry to audit_log.
"""
import argparse
import json
import os
import sys
from datetime import date, timedelta

# Allow `python scripts/promote_profile.py` from either the repo root or
# anywhere else by adding the repo root to sys.path.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from dotenv import load_dotenv
from sqlalchemy import text

load_dotenv()


def _engine():
    from database_tools import _engine as _e
    return _e()


# ── Display ───────────────────────────────────────────────────────────────────

def cmd_list(agent: str) -> int:
    with _engine().connect() as c:
        rows = c.execute(
            text("""
                SELECT version, is_active, is_shadow, model_name, max_tokens,
                       created_by, parent_version, activated_at, deprecated_at,
                       created_at
                FROM agent_strategy_profiles
                WHERE agent_name = :a
                ORDER BY version DESC
            """),
            {"a": agent},
        ).fetchall()
    if not rows:
        print(f"  (no rows for {agent})")
        return 1
    print(f"  Versions for {agent!r}:")
    print(f"  {'ver':>3} {'active':>6} {'shadow':>6} {'model':<32} "
          f"{'max_tok':>7} {'by':<10} {'parent':>6} created")
    for r in rows:
        ver, act, sh, model, mt, cb, pv, *_, created = r
        print(f"  {int(ver):>3} {int(act):>6} {int(sh):>6} "
              f"{model:<32} {int(mt):>7} {cb:<10} {(pv or '—'):>6} {created}")
    return 0


def cmd_proposals(agent: str) -> int:
    with _engine().connect() as c:
        rows = c.execute(
            text("""
                SELECT proposed_version, parent_version, status,
                       score_baseline, score_predicted, score_actual,
                       sample_count, optimizer_cost_usd, created_at, decided_by
                FROM optimizer_proposals
                WHERE agent_name = :a
                ORDER BY proposed_version DESC
            """),
            {"a": agent},
        ).fetchall()
    if not rows:
        print(f"  (no optimizer proposals for {agent})")
        return 1
    print(f"  Optimizer proposals for {agent!r}:")
    print(f"  {'ver':>3} {'parent':>6} {'status':<11} {'baseline':>8} "
          f"{'predict':>8} {'actual':>8} {'n':>4} {'cost':>9} {'by':<14} created")

    def _f(x):
        return f"{float(x):.3f}" if x is not None else "—"

    for r in rows:
        pv, par, st, sb, sp, sa, n, cost, created, by = r
        cost_s = f"${float(cost):.4f}" if cost is not None else "—"
        print(f"  {int(pv):>3} {int(par):>6} {st:<11} {_f(sb):>8} "
              f"{_f(sp):>8} {_f(sa):>8} {int(n or 0):>4} {cost_s:>9} "
              f"{(by or '—'):<14} {created}")
    return 0


def cmd_status(agent: str) -> int:
    from strategy_profile import load_active_profile, load_shadow_profile
    from database_tools import get_recent_shadow_runs

    act = load_active_profile(agent)
    sha = load_shadow_profile(agent)
    print(f"  Status for {agent!r}:")
    if act:
        print(f"    ACTIVE  v{act.version}  model={act.model_name} "
              f"max_tokens={act.max_tokens} prompt_len={len(act.system_prompt)}")
    else:
        print(f"    ACTIVE  (none)")
    if sha:
        print(f"    SHADOW  v{sha.version}  model={sha.model_name} "
              f"tools={sha.tool_whitelist} max_iter={sha.params.get('max_iter')} "
              f"budget={sha.params.get('token_budget')}")
    else:
        print(f"    SHADOW  (none)")

    runs = get_recent_shadow_runs(agent_name=agent, days=14, limit=200)
    if runs:
        scored = [r for r in runs if r["divergence_score"] is not None]
        if scored:
            avg_div = sum(float(r["divergence_score"]) for r in scored) / len(scored)
            avg_cost = sum(float(r["shadow_cost_usd"] or 0) for r in scored) / len(scored)
            errors = sum(1 for r in runs if r.get("shadow_error"))
            print(f"    SHADOW DATA (last 14d): runs={len(runs)} "
                  f"avg_divergence={avg_div:.3f} avg_shadow_cost=${avg_cost:.4f} "
                  f"errors={errors}")
    else:
        print(f"    SHADOW DATA (last 14d): (none)")
    return 0


# ── Mutation ──────────────────────────────────────────────────────────────────

def _audit(conn, agent: str, op: str, before: dict, after: dict) -> None:
    """Write a log entry to audit_log (using JSON snapshots)."""
    conn.execute(
        text("""
            INSERT INTO audit_log (table_name, operation, record_id, actor,
                                   before_json, after_json)
            VALUES ('agent_strategy_profiles', :op, :rid, 'promote_profile_cli',
                    :before, :after)
        """),
        {"op": op, "rid": int(after.get("version") or before.get("version") or 0),
         "before": json.dumps(before, default=str, ensure_ascii=False),
         "after":  json.dumps(after,  default=str, ensure_ascii=False)},
    )


def _sync_proposal_status(conn, agent: str, version: int,
                          new_status: str, from_status: str) -> int:
    """If an optimizer_proposals row exists for (agent, version) in
    `from_status`, move it to `new_status`. Returns rows updated."""
    result = conn.execute(
        text("""
            UPDATE optimizer_proposals
            SET status = :ns, decided_at = NOW(), decided_by = 'promote_profile_cli'
            WHERE agent_name = :a AND proposed_version = :v AND status = :fs
        """),
        {"ns": new_status, "a": agent, "v": version, "fs": from_status},
    )
    return result.rowcount


def _promote(agent: str, version: int, op_name: str) -> int:
    """Transaction: set <version> is_active=1 + clears its is_shadow flag,
    set all other versions for this agent is_active=0. Also syncs the
    optimizer_proposals ledger: PROMOTE marks the target's shadowing
    proposal 'promoted'; REVERT marks the abandoned version's promoted
    proposal 'reverted'."""
    with _engine().begin() as conn:
        before_rows = conn.execute(
            text("""
                SELECT version, is_active, is_shadow
                FROM agent_strategy_profiles WHERE agent_name = :a
                ORDER BY version
            """),
            {"a": agent},
        ).fetchall()
        before_state = [dict(r._mapping) for r in before_rows]
        prev_active = [int(r[0]) for r in before_rows if int(r[1]) == 1]
        target = next((r for r in before_rows if int(r[0]) == version), None)
        if not target:
            print(f"  ERROR: {agent} has no version {version}")
            return 1

        # Deactivate all
        conn.execute(
            text("""
                UPDATE agent_strategy_profiles
                SET is_active = 0,
                    deprecated_at = CASE WHEN is_active = 1 THEN NOW() ELSE deprecated_at END
                WHERE agent_name = :a AND is_active = 1
            """),
            {"a": agent},
        )
        # Activate target, clear its shadow flag
        conn.execute(
            text("""
                UPDATE agent_strategy_profiles
                SET is_active = 1, is_shadow = 0, activated_at = NOW(),
                    deprecated_at = NULL
                WHERE agent_name = :a AND version = :v
            """),
            {"a": agent, "v": version},
        )
        # Sync the optimizer_proposals ledger
        if op_name == "PROMOTE":
            _sync_proposal_status(conn, agent, version, "promoted", "shadowing")
        elif op_name == "REVERT":
            for pv in prev_active:
                if pv != version:
                    _sync_proposal_status(conn, agent, pv, "reverted", "promoted")
        after_rows = conn.execute(
            text("""
                SELECT version, is_active, is_shadow
                FROM agent_strategy_profiles WHERE agent_name = :a
                ORDER BY version
            """),
            {"a": agent},
        ).fetchall()
        after_state = [dict(r._mapping) for r in after_rows]
        _audit(conn, agent, op_name,
               {"versions": before_state},
               {"versions": after_state, "promoted_to": version})
    print(f"  OK: {agent} v{version} is now is_active=1 (was shadow={int(target[2])})")
    return 0


def cmd_promote(agent: str, version: int) -> int:
    return _promote(agent, version, op_name="PROMOTE")


def cmd_revert(agent: str, version: int) -> int:
    return _promote(agent, version, op_name="REVERT")


def cmd_shadow(agent: str, version: int) -> int:
    """Set <version> is_shadow=1; clears any other shadow row for this agent."""
    with _engine().begin() as conn:
        before_rows = conn.execute(
            text("SELECT version, is_shadow FROM agent_strategy_profiles WHERE agent_name = :a"),
            {"a": agent},
        ).fetchall()
        target = next((r for r in before_rows if int(r[0]) == version), None)
        if not target:
            print(f"  ERROR: {agent} has no version {version}")
            return 1
        conn.execute(
            text("UPDATE agent_strategy_profiles SET is_shadow = 0 WHERE agent_name = :a AND is_shadow = 1"),
            {"a": agent},
        )
        conn.execute(
            text("UPDATE agent_strategy_profiles SET is_shadow = 1 WHERE agent_name = :a AND version = :v"),
            {"a": agent, "v": version},
        )
        _audit(conn, agent, "SHADOW",
               {"versions": [dict(r._mapping) for r in before_rows]},
               {"set_shadow": version})
    print(f"  OK: {agent} v{version} is now is_shadow=1")
    return 0


# ── Entry ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Manage agent_strategy_profiles versions (active / shadow / revert).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    for c in ("status", "list", "proposals"):
        sp = sub.add_parser(c)
        sp.add_argument("agent")
    for c in ("promote", "revert", "shadow"):
        sp = sub.add_parser(c)
        sp.add_argument("agent")
        sp.add_argument("version", type=int)
    args = parser.parse_args()

    if args.cmd == "status":   return cmd_status(args.agent)
    if args.cmd == "list":     return cmd_list(args.agent)
    if args.cmd == "proposals": return cmd_proposals(args.agent)
    if args.cmd == "promote":  return cmd_promote(args.agent, args.version)
    if args.cmd == "revert":   return cmd_revert(args.agent, args.version)
    if args.cmd == "shadow":   return cmd_shadow(args.agent, args.version)
    return 2


if __name__ == "__main__":
    sys.exit(main())
