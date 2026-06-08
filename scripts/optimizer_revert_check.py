#!/usr/bin/env python
"""scripts/optimizer_revert_check.py — Phase 2

Daily regression watch for optimizer-promoted strategy versions.

For every optimizer proposal promoted within the last 7 days it recomputes
the promoted version's score over a 7-day window and compares it to the
score_baseline recorded at proposal time. If the promoted version now
scores below REGRESSION_FACTOR x baseline it sends a LINE alert.

Per user decision E this NEVER auto-reverts — it only alerts; the human
decides whether to run  promote_profile.py revert <agent> <old_version>.

Scheduled daily 02:00 Taipei. Run manually:
    uv run python scripts/optimizer_revert_check.py
"""
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from dotenv import load_dotenv

load_dotenv()

REGRESSION_FACTOR = 0.90       # promoted score below 90% of baseline => regression
SCORE_WINDOW_DAYS = 14         # aligned with optimizer_run.SHADOW_WINDOW_DAYS (2026-06-08)
ALERT_DEDUP_HOURS = 24


def _send_alert(agent: str, version: int, message: str) -> None:
    """Send a LINE regression alert, deduplicated for 24h via alert_history."""
    from database_tools import was_alert_sent_recently, record_alert_sent

    alert_id = f"opt_regress_{agent}_v{version}"[:20]
    if was_alert_sent_recently(alert_id, hours=ALERT_DEDUP_HOURS):
        print(f"[revert_check] alert {alert_id} already sent within "
              f"{ALERT_DEDUP_HOURS}h — skip")
        return
    try:
        from messenger_tools import send_line
        send_line(message, _caller="optimizer_revert_check")
        record_alert_sent(alert_id, "critical", message)
        print(f"[revert_check] alert sent: {alert_id}")
    except Exception as exc:
        print(f"[revert_check] WARN: send alert failed: {exc}")


def main() -> int:
    from database_tools import get_promoted_within_days
    from optimizer_scoring import score_version

    promoted = get_promoted_within_days(days=SCORE_WINDOW_DAYS)
    print(f"[revert_check] {len(promoted)} optimizer version(s) promoted in last {SCORE_WINDOW_DAYS}d")

    regressions = 0
    for row in promoted:
        agent = row["agent_name"]
        version = int(row["proposed_version"])
        baseline = row.get("score_baseline")

        if baseline is None:
            print(f"[revert_check] {agent} v{version}: no baseline score — skip")
            continue

        current = score_version(agent, version, window_days=SCORE_WINDOW_DAYS)
        s_now = current.get("score")
        if s_now is None:
            print(f"[revert_check] {agent} v{version}: only "
                  f"{current.get('sample_count', 0)} samples — not enough to judge")
            continue

        threshold = float(baseline) * REGRESSION_FACTOR
        status = "OK" if s_now >= threshold else "REGRESSION"
        print(f"[revert_check] {agent} v{version}: score now={s_now:.3f} "
              f"baseline={float(baseline):.3f} threshold={threshold:.3f} -> {status}")

        if s_now < threshold:
            regressions += 1
            msg = (
                f"⚠️ Optimizer 回歸警報\n"
                f"{agent} v{version} 升版後表現下滑：\n"
                f"目前 score {s_now:.3f} < 基準 {float(baseline):.3f} × {REGRESSION_FACTOR}\n"
                f"建議人工檢視並考慮 revert：\n"
                f"  promote_profile.py revert {agent} <前一版>"
            )
            _send_alert(agent, version, msg)

    print(f"[revert_check] done — {regressions} regression(s) flagged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
