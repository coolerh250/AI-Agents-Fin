"""
alert_runner.py
Standalone health-check + alert runner for the AI Agent Studio.

Crontab (CRON_TZ=Asia/Taipei):
  20 8 * * 1-5  ~/.local/bin/uv run python alert_runner.py
  0  9 * * 0    ~/.local/bin/uv run python alert_runner.py --weekly

Each alert is dedup'd 24 hours by alert_id (alert_history table). Telegram is
attempted first; if it returns non-OK (e.g. unconfigured) the message falls
back to LINE.

Alerts implemented:
  A-001  Workflow not completed by check time              critical
  A-002  Daily brief missing from DB                       critical
  A-003  No successful notification delivery               critical
  A-007  Opus thinking token spike (> 2048)                warning
  A-008  data_collector fallback rate > 20% (weekly)       info
  A-009  Backtest direction accuracy < 40% (weekly)        info
  A-010  Brief quality score drop > 20% week-over-week     warning
"""
import argparse
import sys
from datetime import date

from dotenv import load_dotenv
from loguru import logger

load_dotenv()

logger.remove()
logger.add(sys.stdout, format="{time:HH:mm:ss} | {level:<8} | {message}",
           level="INFO", colorize=False)


# ── Unified send helper with 24h dedup + multi-channel routing ───────────────

def _send(alert_id: str, severity: str, msg: str) -> None:
    """
    Send an alert with 24h dedup keyed on alert_id.
    severity: "critical" → LINE + Telegram
              "warning" / "info" → Telegram preferred, LINE fallback
    """
    from database_tools import was_alert_sent_recently, record_alert_sent
    if was_alert_sent_recently(alert_id, hours=24):
        logger.info(f"[ALERT-DEDUP] {alert_id} sent within 24h — skipping")
        return

    from messenger_tools import send_line, send_telegram
    if severity == "critical":
        send_line(msg)
        send_telegram(msg)
    else:
        if send_telegram(msg).get("status") != "ok":
            send_line(msg)  # fallback when Telegram unconfigured/unavailable

    record_alert_sent(alert_id, severity, msg)
    logger.warning(f"[{severity.upper()}] {alert_id}: {msg[:80]}")


# ── A-001: Workflow not completed ─────────────────────────────────────────────

def check_a001(today: date) -> bool:
    from database_tools import get_run_status
    run = get_run_status(today)
    if run is None:
        _send("A-001", "critical",
              "🚨 [A-001] CRITICAL: 今日工作流未執行\n"
              "排程可能失敗，請手動執行 investment_workflow.py")
        return False
    if run["status"] == "failed":
        _send("A-001", "critical",
              f"🚨 [A-001] CRITICAL: 今日工作流失敗\n"
              f"錯誤：{run.get('error_message', 'unknown')[:200]}")
        return False
    return True


# ── A-002: Daily brief missing ────────────────────────────────────────────────

def check_a002(today: date) -> bool:
    from database_tools import get_brief
    if get_brief(today) is None:
        _send("A-002", "critical",
              "🚨 [A-002] CRITICAL: 今日建議書未寫入 TiDB\n"
              "Backtest 無法評估，請手動檢查 save_to_db_node")
        return False
    return True


# ── A-003: No successful delivery ────────────────────────────────────────────

def check_a003(today: date) -> bool:
    from database_tools import get_run_status, get_run_events
    run = get_run_status(today)
    if not run:
        return True  # A-001 already fired
    events = get_run_events(run["id"])
    if not any(e["event_type"] == "delivery_success" for e in events):
        _send("A-003", "critical",
              "🚨 [A-003] CRITICAL: LINE/Telegram 推播均失敗\n"
              "請確認 LINE_CHANNEL_ACCESS_TOKEN 及 TELEGRAM_BOT_TOKEN 是否有效")
        return False
    return True


# ── A-007: Opus thinking token spike ─────────────────────────────────────────

def check_a007(today: date) -> None:
    from database_tools import _engine
    from sqlalchemy import text
    with _engine().connect() as conn:
        row = conn.execute(
            text("""
                SELECT cl.thinking_tokens, cl.estimated_cost_usd
                FROM cost_logs cl
                JOIN workflow_runs wr ON cl.run_id = wr.id
                WHERE DATE(wr.started_at) = :d
                  AND cl.agent_name = 'chief_strategist'
                ORDER BY cl.logged_at DESC LIMIT 1
            """),
            {"d": today},
        ).fetchone()
    if row and row[0] > 2048:
        _send("A-007", "warning",
              f"⚠️ [A-007] Opus 思考 Token 超過預算上限\n"
              f"實際 thinking_tokens: {row[0]}\n"
              f"預期上限：2048（budget_tokens 設定可能未生效）\n"
              f"今日 chief_strategist 成本：${float(row[1]):.4f}")


# ── A-008: data_collector fallback rate > 20% (weekly) ───────────────────────

def check_a008() -> None:
    from database_tools import _engine
    from sqlalchemy import text
    with _engine().connect() as conn:
        result = conn.execute(text("""
            SELECT
                COUNT(DISTINCT we.run_id) AS fallback_runs,
                (SELECT COUNT(*) FROM workflow_runs
                 WHERE run_type = 'investment'
                   AND started_at >= NOW() - INTERVAL 7 DAY) AS total_runs
            FROM workflow_events we
            JOIN workflow_runs wr ON we.run_id = wr.id
            WHERE we.event_type = 'fallback_activated'
              AND we.node_name = 'data_collector'
              AND wr.started_at >= NOW() - INTERVAL 7 DAY
        """)).fetchone()
    if not result or not result[1]:
        return
    rate = result[0] / result[1] * 100
    if rate > 20:
        _send("A-008", "info",
              f"📊 [A-008] data_collector Fallback 率過高\n"
              f"過去 7 天：{result[0]}/{result[1]} 次 ({rate:.0f}%)\n"
              f"請檢查 Haiku JSON 輸出格式與 TAIFEX 爬蟲")


# ── A-009: Backtest accuracy < 40% (weekly) ───────────────────────────────────

_A009_MIN_SAMPLES = 5

def check_a009() -> None:
    from database_tools import _engine
    from sqlalchemy import text
    with _engine().connect() as conn:
        rows = conn.execute(text("""
            SELECT b.gap_direction, a.actual_gap_pct
            FROM daily_briefs b
            JOIN market_actuals a ON b.trade_date = a.trade_date
            WHERE b.trade_date >= CURDATE() - INTERVAL 14 DAY
            ORDER BY b.trade_date DESC LIMIT 10
        """)).fetchall()

    if len(rows) < _A009_MIN_SAMPLES:
        logger.debug(f"[A-009] only {len(rows)} samples (need ≥{_A009_MIN_SAMPLES}) — skipping")
        return

    correct = sum(
        1 for r in rows
        if (r[0] == "up"   and float(r[1]) >  0.3)
        or (r[0] == "down" and float(r[1]) < -0.3)
        or (r[0] == "flat" and abs(float(r[1])) <= 0.3)
    )
    accuracy = correct / len(rows) * 100
    if accuracy < 40:
        _send("A-009", "info",
              f"📊 [A-009] 方向預測準確率低於 40%\n"
              f"近 {len(rows)} 日準確率：{accuracy:.0f}% ({correct}/{len(rows)})\n"
              f"請檢查 chief_strategist 系統提示與輸入品質")


# ── A-010: Brief quality score drop > 20% week-over-week (weekly) ────────────

def check_a010() -> None:
    from database_tools import _engine
    from sqlalchemy import text
    with _engine().connect() as conn:
        row = conn.execute(text("""
            SELECT
                AVG(CASE WHEN trade_date >= CURDATE() - INTERVAL 7 DAY
                         THEN brief_quality_score END) AS this_week,
                AVG(CASE WHEN trade_date >= CURDATE() - INTERVAL 14 DAY
                         AND trade_date < CURDATE() - INTERVAL 7 DAY
                         THEN brief_quality_score END) AS last_week,
                COUNT(CASE WHEN trade_date >= CURDATE() - INTERVAL 7 DAY
                           THEN 1 END) AS this_week_n,
                COUNT(CASE WHEN trade_date >= CURDATE() - INTERVAL 14 DAY
                           AND trade_date < CURDATE() - INTERVAL 7 DAY
                           THEN 1 END) AS last_week_n
            FROM eval_runs
            WHERE trade_date >= CURDATE() - INTERVAL 14 DAY
              AND status = 'success'
              AND brief_quality_score IS NOT NULL
        """)).fetchone()

    if not row or row[0] is None or row[1] is None:
        return
    if int(row[2] or 0) < 2 or int(row[3] or 0) < 2:
        return  # Not enough data for a meaningful comparison

    this_week = float(row[0])
    last_week = float(row[1])
    drop_pct = (last_week - this_week) / last_week * 100 if last_week > 0 else 0

    if drop_pct > 20:
        _send("A-010", "warning",
              f"⚠️ [A-010] 建議書品質分數週對週下降 {drop_pct:.0f}%\n"
              f"本週均分：{this_week:.1f} ({int(row[2])} 筆)\n"
              f"上週均分：{last_week:.1f} ({int(row[3])} 筆)\n"
              f"請檢查 evaluation_runner.py 或 LLM 輸出品質")


# ── Weekly digest ─────────────────────────────────────────────────────────────

def send_weekly_digest() -> None:
    from database_tools import get_recent_events, get_cost_summary, get_workflow_runs
    runs   = get_workflow_runs(7)
    events = get_recent_events(days=7)
    costs  = get_cost_summary(7)

    total_cost     = sum(float(r.get("total_cost_usd", 0) or 0) for r in costs)
    fallback_count = sum(1 for e in events if e["event_type"] == "fallback_activated")
    error_count    = sum(1 for e in events if e["severity"] == "error")
    success_runs   = sum(1 for r in runs if r["status"] == "success")

    _send("DIGEST", "info",
          f"📊 [Weekly Digest] 過去 7 天摘要\n"
          f"工作流執行：{len(runs)} 次（成功 {success_runs}）\n"
          f"總 API 成本：${total_cost:.4f}\n"
          f"Fallback 觸發：{fallback_count} 次\n"
          f"錯誤事件：{error_count} 次\n"
          f"詳細請查看 Streamlit 儀表板")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="AI Agent Studio Alert Runner")
    parser.add_argument("--weekly", action="store_true",
                        help="Run weekly checks (A-008, A-009, A-010, digest) in addition to daily")
    parser.add_argument("--date", default=None,
                        help="Override check date (YYYY-MM-DD). Default: today")
    args = parser.parse_args()

    check_date = date.fromisoformat(args.date) if args.date else date.today()
    logger.info(f"[AlertRunner] Running daily checks for {check_date}")

    from database_tools import ensure_observability_tables
    ensure_observability_tables()

    ok_001 = check_a001(check_date)
    if ok_001:
        check_a002(check_date)
        check_a003(check_date)
        check_a007(check_date)

    if args.weekly:
        logger.info("[AlertRunner] Running weekly checks")
        check_a008()
        check_a009()
        check_a010()
        send_weekly_digest()

    logger.info("[AlertRunner] Done")


if __name__ == "__main__":
    main()
