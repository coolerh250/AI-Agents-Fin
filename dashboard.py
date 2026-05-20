"""
dashboard.py
Streamlit lightweight dashboard for the Taiwan Stock Futures Analysis Team.
Run: uv run streamlit run dashboard.py --server.port 8501
"""
import json
import os
from datetime import date

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from database_tools import (
    add_portfolio_item,
    delete_portfolio_item,
    get_cost_summary,
    get_cost_trend,
    get_eval_agent_avg_scores,
    get_eval_dashboard_kpis,
    get_eval_results,
    get_eval_runs,
    get_flywheel_stats,
    get_per_run_cost_summary,
    get_portfolio,
    get_recent_accuracy,
    get_recent_audit_log,
    get_recent_events,
    get_recent_lessons,
    get_recent_llm_traces,
    get_recent_shadow_runs,
    get_shadow_summary_per_agent,
    get_workflow_runs,
    save_actual,
    update_portfolio_item,
)

st.set_page_config(page_title="量化工作室看板", layout="wide")


def _require_auth() -> None:
    """LINE OTP is now the sole login path (DASH_PASSWORD removed)."""
    if st.session_state.get("authenticated"):
        return

    st.title("🔐 登入")
    st.info("請先在 LINE Bot 傳送「**登入代碼**」，再將收到的 8 位代碼輸入於此")
    code = st.text_input("登入代碼", max_chars=8, placeholder="AB12CD34")
    if st.button("登入"):
        from database_tools import consume_login_token
        uid = consume_login_token(code.strip().upper())
        if uid:
            st.session_state.authenticated = True
            st.session_state.line_user_id = uid
            st.rerun()
        else:
            st.error("代碼無效、已過期或已使用")
    st.stop()


_require_auth()
st.title("📈 台股期貨量化工作室")

# ── Sidebar: logged-in user + logout ─────────────────────────────────────────
with st.sidebar:
    _sid = st.session_state.get("line_user_id") or ""
    st.caption(f"已登入：LINE: {_sid[:15]}...")
    if st.button("登出"):
        for _k in ("authenticated", "line_user_id"):
            st.session_state.pop(_k, None)
        st.rerun()


# ── Cached P&L fetch (5-minute TTL) ──────────────────────────────────────────

@st.cache_data(ttl=300)
def _fetch_pnl(holdings_json: str) -> list[dict]:
    from portfolio_tools import calculate_pnl
    return calculate_pnl(json.loads(holdings_json), _caller="dashboard")


def load_pnl(user_id) -> list[dict]:
    holdings = get_portfolio(user_id, _caller="dashboard")
    if not holdings:
        return []
    return _fetch_pnl(json.dumps(holdings, default=str))


@st.cache_data(ttl=3600)
def get_stock_history(stock_id: str, period: str = "3mo") -> pd.DataFrame:
    import yfinance as yf
    hist = yf.Ticker(f"{stock_id}.TW").history(period=period)
    if hist.empty:
        return pd.DataFrame()
    hist.index = pd.to_datetime(hist.index).tz_localize(None)
    return hist[["Close"]].rename(columns={"Close": "收盤價"})


def _calc_accuracy_kpi(rows: list[dict]) -> dict:
    complete = [r for r in rows if r.get("actual_gap_pct") is not None]
    if not complete:
        return {}

    def _correct(r) -> bool:
        pred   = r.get("gap_direction", "")
        actual = float(r["actual_gap_pct"])
        if pred == "up":
            return actual > 0.3
        if pred == "down":
            return actual < -0.3
        return abs(actual) <= 0.3  # flat

    total   = len(complete)
    correct = sum(1 for r in complete if _correct(r))

    errors = [
        abs(float(r["predicted_gap_pct"]) - float(r["actual_gap_pct"]))
        for r in complete
        if r.get("predicted_gap_pct") is not None
    ]

    recent7  = complete[-7:]
    r7_total = len(recent7)
    r7_ok    = sum(1 for r in recent7 if _correct(r))

    return {
        "total":           total,
        "accuracy_pct":    correct / total * 100,
        "avg_error":       sum(errors) / len(errors) if errors else None,
        "recent7_pct":     r7_ok / r7_total * 100 if r7_total else None,
        "recent7_total":   r7_total,
    }


# ── Tabs ──────────────────────────────────────────────────────────────────────

(tab_accuracy, tab_cost, tab_portfolio, tab_health, tab_events,
 tab_eval, tab_flywheel, tab_audit, tab_shadow) = st.tabs([
    "📊 預測準確度",
    "💰 API 成本分析",
    "💼 個人持倉管理",
    "🟢 系統健康",
    "📋 事件日誌",
    "📝 評估",
    "🔄 Flywheel",
    "🔍 稽核 / Trace",
    "👥 Shadow 比對",
])


# ── Tab 1: Prediction Accuracy ────────────────────────────────────────────────

with tab_accuracy:
    rows = get_recent_accuracy(30)
    if not rows:
        st.warning("尚無回測數據 — 請先執行 backtest_agent.py")
    else:
        df = pd.DataFrame(rows)
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        df = df.sort_values("trade_date")

        # ── KPI metrics ───────────────────────────────────────────────────────
        kpi = _calc_accuracy_kpi(rows)
        if kpi:
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("累計回測天數",   kpi["total"])
            k2.metric("整體方向準確率", f"{kpi['accuracy_pct']:.1f}%")
            k3.metric("近 7 日準確率",
                      f"{kpi['recent7_pct']:.1f}%" if kpi["recent7_pct"] is not None else "–",
                      help=f"樣本數 {kpi['recent7_total']} 天")
            k4.metric("幅度平均誤差",
                      f"{kpi['avg_error']:.2f}%" if kpi["avg_error"] is not None else "–")
            st.divider()

        # ── Charts ────────────────────────────────────────────────────────────
        col1, col2 = st.columns(2)
        with col1:
            st.subheader("預測方向分佈")
            st.bar_chart(df["gap_direction"].value_counts())
        with col2:
            st.subheader("預測 vs 實際跳空幅度 (%)")
            chart_df = (
                df[["trade_date", "predicted_gap_pct", "actual_gap_pct"]]
                .dropna(subset=["predicted_gap_pct"])
                .set_index("trade_date")
            )
            st.line_chart(chart_df)

        st.subheader("近期回測明細")
        st.dataframe(
            df.rename(columns={
                "trade_date":        "日期",
                "gap_direction":     "預測方向",
                "predicted_gap_pct": "預測跳空%",
                "actual_gap_pct":    "實際跳空%",
                "open_price":        "開盤指數",
                "close_price":       "收盤指數",
            }),
            use_container_width=True,
        )

        # ── Manual actual data entry ──────────────────────────────────────────
        with st.expander("✏️ 手動輸入今日實際走勢"):
            with st.form("manual_actual_form"):
                mc1, mc2, mc3, mc4 = st.columns(4)
                m_date  = mc1.date_input("交易日", value=date.today())
                m_gap   = mc2.number_input("實際跳空幅度 (%)", value=0.0, step=0.01, format="%.2f")
                m_open  = mc3.number_input("開盤指數", min_value=0.0, value=0.0, step=1.0)
                m_close = mc4.number_input("收盤指數", min_value=0.0, value=0.0, step=1.0)
                if st.form_submit_button("寫入 market_actuals", use_container_width=True):
                    save_actual(m_date, m_open, m_close, m_gap, notes="manual")
                    st.success(f"已寫入 {m_date} 跳空 {m_gap:+.2f}%")
                    st.rerun()


# ── Tab 2: Cost Analytics ─────────────────────────────────────────────────────

with tab_cost:
    cost_rows  = get_cost_summary(30)
    trend_rows = get_cost_trend(30)
    run_rows   = get_per_run_cost_summary(30)

    if not cost_rows:
        st.info("尚無成本記錄 — 請先執行含成本追蹤的 investment_workflow.py")
    else:
        cost_df = pd.DataFrame(cost_rows)
        cost_df["total_cost_usd"]  = cost_df["total_cost_usd"].astype(float)
        cost_df["avg_latency_ms"]  = cost_df["avg_latency_ms"].astype(float)
        cost_df["total_thinking"]  = cost_df.get("total_thinking", pd.Series([0]*len(cost_df))).fillna(0).astype(int)

        col3, col4 = st.columns(2)
        with col3:
            st.subheader("各節點總成本（30 天）")
            st.bar_chart(cost_df.set_index("agent_name")["total_cost_usd"])
        with col4:
            if trend_rows:
                st.subheader("每日成本趨勢")
                trend_df = pd.DataFrame(trend_rows)
                trend_df["daily_cost_usd"] = trend_df["daily_cost_usd"].astype(float)
                trend_df["day"] = pd.to_datetime(trend_df["day"]).dt.date
                st.line_chart(trend_df.set_index("day")["daily_cost_usd"])
            else:
                st.subheader("每日成本趨勢")
                st.info("累積不足兩天，趨勢圖待更新")

        # Thinking tokens chart (Opus visibility)
        thinking_df = cost_df[cost_df["total_thinking"] > 0]
        if not thinking_df.empty:
            st.subheader("Opus 思考 Token 分佈（chief_strategist）")
            st.bar_chart(thinking_df.set_index("agent_name")["total_thinking"])

        st.subheader("節點效能明細")
        st.dataframe(
            cost_df.rename(columns={
                "agent_name":     "節點",
                "model_name":     "模型",
                "total_input":    "輸入 Token",
                "total_output":   "輸出 Token",
                "total_thinking": "思考 Token",
                "total_cost_usd": "總成本 (USD)",
                "avg_latency_ms": "平均耗時 (ms)",
                "runs":           "執行次數",
            }),
            use_container_width=True,
        )

        # Per-run cost table
        if run_rows:
            st.subheader("每次執行成本明細（30 天）")
            run_df = pd.DataFrame(run_rows)
            run_df["total_cost_usd"] = run_df["total_cost_usd"].astype(float)
            run_df["trade_date"]     = pd.to_datetime(run_df["trade_date"]).dt.date
            over_threshold = run_df["total_cost_usd"] > 0.15
            if over_threshold.any():
                st.warning(f"⚠️ {over_threshold.sum()} 次執行成本超過 $0.15 閾值")
            st.dataframe(
                run_df[["trade_date", "status", "total_cost_usd",
                        "opus_thinking_tokens", "snapshot_age_seconds",
                        "duration_seconds"]].rename(columns={
                    "trade_date":            "日期",
                    "status":                "狀態",
                    "total_cost_usd":        "成本 (USD)",
                    "opus_thinking_tokens":  "Opus 思考 Token",
                    "snapshot_age_seconds":  "快照年齡 (秒)",
                    "duration_seconds":      "執行耗時 (秒)",
                }),
                use_container_width=True,
            )


# ── Tab 3: Portfolio Management ───────────────────────────────────────────────

with tab_portfolio:
    _current_uid = st.session_state.get("line_user_id")
    col_form, col_main = st.columns([1, 3])

    # ── Add holding form (left column) ───────────────────────────────────────
    with col_form:
        st.subheader("➕ 新增持倉")
        with st.form("add_holding_form", clear_on_submit=True):
            new_stock  = st.text_input("股票代碼", placeholder="2330")
            new_entry  = st.number_input("成本價 (元)", min_value=0.01, value=100.0, step=0.5)
            new_qty    = st.number_input("股數", min_value=1, value=1000, step=100)
            new_sl     = st.number_input("止損比例 (%)", min_value=0.1, max_value=50.0, value=5.0, step=0.5)
            new_strat  = st.selectbox("策略類型", ["波段", "長抱", "存股", "當沖"])
            add_clicked = st.form_submit_button("新增", use_container_width=True)

        if add_clicked:
            if not new_stock.strip():
                st.error("請輸入股票代碼")
            else:
                ok = add_portfolio_item(
                    new_stock.strip().upper(),
                    new_entry, int(new_qty), new_sl, new_strat,
                    user_id=_current_uid,
                    _caller="dashboard",
                )
                if ok:
                    st.success(f"已新增 {new_stock.strip().upper()}")
                    st.cache_data.clear()
                    st.rerun()
                else:
                    st.error("新增失敗（相同代碼+成本已存在）")

    # ── Holdings P&L (right column) ──────────────────────────────────────────
    with col_main:
        pnl_rows = load_pnl(_current_uid)

        if not pnl_rows:
            st.info("尚無持倉資料 — 請使用左側表單新增持倉")
        else:
            total_cost    = sum(float(r["entry_price"]) * int(r["quantity"]) for r in pnl_rows)
            total_value   = sum(r["current_price"] * int(r["quantity"]) for r in pnl_rows)
            total_pnl     = total_value - total_cost
            total_pnl_pct = total_pnl / total_cost * 100 if total_cost else 0

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("持倉總成本",  f"${total_cost:,.0f}")
            m2.metric("持倉現值",    f"${total_value:,.0f}")
            m3.metric("未實現損益",  f"${total_pnl:,.0f}", f"{total_pnl_pct:+.2f}%")
            m4.metric("持倉筆數",    len(pnl_rows))

            # P&L display
            st.subheader("持倉損益總覽")
            display_rows = [
                {
                    "ID":        r["id"],
                    "代碼":      r["stock_id"],
                    "策略":      r["strategy_type"],
                    "成本(元)":  float(r["entry_price"]),
                    "現價(元)":  round(r["current_price"], 2),
                    "股數":      int(r["quantity"]),
                    "未實現損益": round(r["unrealized_pnl"], 0),
                    "損益%":     round(r["pnl_pct"], 2),
                    "止損觸發":  "🔴" if r["stop_loss_triggered"] else "🟢",
                    "止損%":     float(r["stop_loss_level"]),
                }
                for r in pnl_rows
            ]
            st.dataframe(
                pd.DataFrame(display_rows).drop(columns=["ID"]),
                use_container_width=True,
            )

            # Edit holdings
            st.subheader("編輯持倉")
            edit_df = pd.DataFrame([
                {
                    "id":       int(r["id"]),
                    "代碼":     r["stock_id"],
                    "成本(元)": float(r["entry_price"]),
                    "股數":     int(r["quantity"]),
                    "止損%":    float(r["stop_loss_level"]),
                    "策略":     r["strategy_type"],
                }
                for r in pnl_rows
            ])
            edited = st.data_editor(
                edit_df,
                column_config={
                    "id":       st.column_config.NumberColumn("ID",      disabled=True),
                    "代碼":     st.column_config.TextColumn("代碼",      disabled=True),
                    "成本(元)": st.column_config.NumberColumn("成本(元)", min_value=0.01, step=0.5),
                    "股數":     st.column_config.NumberColumn("股數",     min_value=1, step=100),
                    "止損%":    st.column_config.NumberColumn("止損%",    min_value=0.1, max_value=50.0, step=0.5),
                    "策略":     st.column_config.SelectboxColumn("策略",  options=["波段", "長抱", "存股", "當沖"]),
                },
                hide_index=True,
                use_container_width=True,
                key="portfolio_editor",
            )

            if st.button("💾 儲存變更"):
                for _, row in edited.iterrows():
                    update_portfolio_item(
                        int(row["id"]),
                        int(row["股數"]),
                        float(row["止損%"]),
                        str(row["策略"]),
                        entry_price=float(row["成本(元)"]),
                        user_id=_current_uid,
                        _caller="dashboard",
                    )
                st.cache_data.clear()
                st.success("已儲存所有變更")
                st.rerun()

            # Delete holding
            with st.expander("🗑️ 刪除持倉"):
                options = {
                    f"{r['stock_id']} (成本 {r['entry_price']} 元, ID={r['id']})": int(r["id"])
                    for r in pnl_rows
                }
                selected = st.selectbox("選擇要刪除的持倉", list(options.keys()))
                if st.button("確認刪除", type="primary"):
                    delete_portfolio_item(options[selected], user_id=_current_uid,
                                          _caller="dashboard")
                    st.cache_data.clear()
                    st.success(f"已刪除：{selected}")
                    st.rerun()

            # Historical price chart
            st.divider()
            st.subheader("📈 持倉歷史走勢")
            period_opt = st.radio(
                "顯示區間", ["1mo", "3mo", "6mo", "1y"],
                index=1, horizontal=True,
                format_func=lambda x: {"1mo": "1 個月", "3mo": "3 個月", "6mo": "6 個月", "1y": "1 年"}[x],
            )
            for r in pnl_rows:
                stock_id   = r["stock_id"]
                entry      = float(r["entry_price"])
                hist_df    = get_stock_history(stock_id, period=period_opt)
                if hist_df.empty:
                    st.warning(f"{stock_id}: 無法取得歷史價格")
                    continue
                hist_df["成本"] = entry
                pnl_series = (hist_df["收盤價"] - entry) * int(r["quantity"])
                st.caption(
                    f"**{stock_id}** — 成本 {entry:,.0f} 元｜"
                    f"現價 {hist_df['收盤價'].iloc[-1]:,.0f} 元｜"
                    f"未實現損益 {pnl_series.iloc[-1]:+,.0f} 元"
                )
                st.line_chart(hist_df[["收盤價", "成本"]])


# ── Tab 4: System Health ──────────────────────────────────────────────────────

with tab_health:
    runs = get_workflow_runs(30)
    if not runs:
        st.warning("尚無 workflow_runs 資料 — 請先執行一次 investment_workflow.py（已升級 schema）")
    else:
        runs_df = pd.DataFrame(runs)
        runs_df["started_at"]    = pd.to_datetime(runs_df["started_at"])
        runs_df["total_cost_usd"] = runs_df["total_cost_usd"].astype(float)
        runs_df["trade_date"]    = runs_df["started_at"].dt.date

        _STATUS_ICON = {"success": "🟢", "failed": "🔴", "running": "🟡"}
        runs_df["狀態"] = runs_df["status"].map(_STATUS_ICON).fillna("⚪")

        success_rate = runs_df["status"].eq("success").mean() * 100
        avg_cost     = runs_df["total_cost_usd"].mean()
        over_thresh  = runs_df["total_cost_usd"].gt(0.15).sum()

        h1, h2, h3, h4 = st.columns(4)
        h1.metric("執行次數（30 天）", len(runs_df))
        h2.metric("成功率", f"{success_rate:.1f}%")
        h3.metric("平均成本 / run", f"${avg_cost:.4f}")
        h4.metric("超出閾值次數", over_thresh, help="單次成本 > $0.15")

        st.subheader("工作流執行紀錄")
        st.dataframe(
            runs_df[["狀態", "trade_date", "total_cost_usd",
                     "snapshot_age_seconds", "error_message"]].rename(columns={
                "trade_date":            "日期",
                "total_cost_usd":        "成本 (USD)",
                "snapshot_age_seconds":  "快照年齡 (秒)",
                "error_message":         "錯誤訊息",
            }),
            use_container_width=True,
        )

        # Cost over time
        if len(runs_df) > 1:
            st.subheader("每次執行成本走勢")
            chart_df = runs_df[runs_df["status"] == "success"][["trade_date", "total_cost_usd"]].copy()
            chart_df = chart_df.set_index("trade_date").sort_index()
            st.line_chart(chart_df)


# ── Tab 5: Event Log ──────────────────────────────────────────────────────────

with tab_events:
    ev_col1, ev_col2 = st.columns([1, 4])
    with ev_col1:
        sev_filter = st.multiselect(
            "嚴重程度", ["info", "warn", "error"],
            default=["warn", "error"],
        )
        ev_days = st.slider("查詢天數", 1, 30, 7)

    events = get_recent_events(days=ev_days, severity_filter=sev_filter or None)

    if not events:
        st.success(f"最近 {ev_days} 天無符合條件的事件")
    else:
        ev_df = pd.DataFrame(events)
        ev_df["created_at"] = pd.to_datetime(ev_df["created_at"])

        _SEV_ICON = {"error": "🔴", "warn": "🟡", "info": "🔵"}
        ev_df["●"] = ev_df["severity"].map(_SEV_ICON).fillna("⚪")

        e1, e2, e3 = st.columns(3)
        e1.metric("事件總計", len(ev_df))
        e2.metric("錯誤", ev_df["severity"].eq("error").sum())
        e3.metric("警告", ev_df["severity"].eq("warn").sum())

        # Detail column: pretty-print JSON
        ev_df["detail_str"] = ev_df["detail"].apply(
            lambda d: str(d)[:120] if d else ""
        )

        st.dataframe(
            ev_df[["●", "created_at", "event_type", "node_name",
                   "run_id", "detail_str"]].rename(columns={
                "created_at": "時間",
                "event_type": "事件類型",
                "node_name":  "節點",
                "run_id":     "Run ID",
                "detail_str": "詳情",
            }),
            use_container_width=True,
        )

# ── Tab 6: Evaluation ─────────────────────────────────────────────────────────

with tab_eval:
    st.subheader("Agent 品質評估")

    kpis = get_eval_dashboard_kpis(30)

    if not kpis:
        st.info("尚無評估記錄。執行 evaluation_runner.py 或 backtest_agent.py 後將自動填入。")
    else:
        e1, e2, e3, e4 = st.columns(4)
        e1.metric("評估次數（30 天）", kpis.get("eval_count", 0))
        e2.metric("平均品質分", f"{kpis.get('avg_quality_score', 0.0):.1f} / 100")
        e3.metric("方向準確率", f"{kpis.get('direction_accuracy_pct', 0.0):.1f}%")
        e4.metric("Schema 通過率", f"{kpis.get('schema_pass_rate', 0.0):.1f}%")

    st.divider()

    # Bar chart: per-agent average quality score
    agent_avg = get_eval_agent_avg_scores(20)
    if agent_avg:
        st.subheader("各代理平均品質分（近 20 次評估）")
        avg_df = pd.DataFrame(agent_avg).set_index("agent_name")
        st.bar_chart(avg_df["avg_score"])

    # Detailed table: last 20 eval runs
    st.subheader("近 20 次評估明細")
    eval_run_rows = get_eval_runs(60)[:20]

    if not eval_run_rows:
        st.info("無評估記錄。")
    else:
        run_ids = [int(r["id"]) for r in eval_run_rows]
        result_rows = get_eval_results(run_ids)

        # Pivot result_rows → {eval_run_id: {agent_name: quality_score}}
        pivot: dict = {}
        for rr in result_rows:
            rid = int(rr["eval_run_id"])
            pivot.setdefault(rid, {})[str(rr["agent_name"])] = rr["quality_score"]

        display = []
        for r in eval_run_rows:
            rid = int(r["id"])
            scores = pivot.get(rid, {})
            dc = r.get("direction_correct")
            display.append({
                "日期":          str(r["trade_date"]),
                "觸發":          r.get("triggered_by", "—"),
                "品質分":        r.get("brief_quality_score"),
                "方向":          (
                    "✅" if dc == 1 else
                    "❌" if dc == 0 else "—"
                ),
                "預測→實際":     (
                    f"{r.get('predicted_direction','?')}→{r.get('actual_direction','?')}"
                    if r.get("predicted_direction") else "—"
                ),
                "data_collector":    scores.get("data_collector"),
                "chip_analyst":      scores.get("chip_analyst"),
                "tech_analyst":      scores.get("tech_analyst"),
                "chief_strategist":  scores.get("chief_strategist"),
                "format_agent":      scores.get("format_agent"),
                "backtest":          scores.get("backtest_evaluator"),
            })

        eval_display_df = pd.DataFrame(display)
        st.dataframe(eval_display_df, use_container_width=True)


# ── Tab 7: Flywheel ───────────────────────────────────────────────────────────

with tab_flywheel:
    st.subheader("🔄 Adaptive Data Flywheel — 策略教訓學習")

    fw_stats   = get_flywheel_stats()
    fw_lessons = get_recent_lessons(14)
    eval_rows  = get_eval_runs(30)

    # ── KPI row ───────────────────────────────────────────────────────────────
    error_counts = {r["error_type"]: r["count"] for r in fw_stats["by_error_type"]}
    top_error    = max(error_counts, key=error_counts.get) if error_counts else "—"
    dir_errors   = error_counts.get("direction_error", 0) + error_counts.get("overconfidence_error", 0)

    fw1, fw2, fw3, fw4 = st.columns(4)
    fw1.metric("活躍教訓總數", fw_stats["total"], help="90 天滾動視窗（strategy_lessons）")
    fw2.metric("最常見錯誤類型", top_error)
    fw3.metric("方向類錯誤筆數", dir_errors, help="direction_error + overconfidence_error")
    fw4.metric("最近評估次數（30 天）", len(eval_rows))

    st.divider()

    # ── Charts row ────────────────────────────────────────────────────────────
    c_left, c_right = st.columns(2)

    with c_left:
        st.subheader("錯誤類型分佈")
        if fw_stats["by_error_type"]:
            err_df = pd.DataFrame(fw_stats["by_error_type"]).set_index("error_type")
            st.bar_chart(err_df["count"])
        else:
            st.info("尚無教訓資料 — backtest_agent.py 執行後自動填入")

    with c_right:
        st.subheader("建議書品質分趨勢（30 天）")
        if eval_rows:
            q_df = pd.DataFrame([
                {"日期": str(r["trade_date"]), "品質分": r.get("brief_quality_score")}
                for r in eval_rows
                if r.get("brief_quality_score") is not None
            ])
            if not q_df.empty:
                q_df = q_df.sort_values("日期").set_index("日期")
                st.line_chart(q_df["品質分"])
            else:
                st.info("尚無品質分資料")
        else:
            st.info("尚無評估記錄")

    st.divider()

    # ── Lessons detail table ──────────────────────────────────────────────────
    st.subheader("近 14 筆教訓明細")
    if not fw_lessons:
        st.info("strategy_lessons 尚無資料，請執行 backtest_agent.py 後重新整理。")
    else:
        _DIR_ICON = {1: "✅", 0: "❌"}
        rows_display = [
            {
                "日期":       str(r["trade_date"]),
                "錯誤類型":   r["error_type"],
                "方向":       _DIR_ICON.get(r["direction_correct"], "—"),
                "SOX 態勢":   r["regime_sox"] or "—",
                "外資 OI":    r["regime_foreign_oi"] or "—",
                "品質分":     round(float(r["composite_score"]), 1) if r["composite_score"] is not None else None,
                "教訓評分":   round(float(r["lesson_quality_score"]), 1) if r.get("lesson_quality_score") is not None else None,
                "教訓摘要":   str(r["lesson_preview"] or "").replace("\n", " ")[:120],
                "到期日":     str(r["expires_at"]),
            }
            for r in fw_lessons
        ]
        fw_df = pd.DataFrame(rows_display)
        st.dataframe(fw_df, use_container_width=True, hide_index=True)


# ── Tab 8: Audit / LLM Trace ──────────────────────────────────────────────────

with tab_audit:
    st.subheader("最近 7 天稽核紀錄（portfolio CRUD / 變更）")
    audit_days = st.slider("天數", 1, 30, 7, key="audit_days")
    audit_rows = get_recent_audit_log(days=audit_days, limit=100)
    if not audit_rows:
        st.info("近期無稽核紀錄。")
    else:
        audit_df = pd.DataFrame([
            {
                "時間":  str(r["created_at"]),
                "表":    r["table_name"],
                "操作":  r["operation"],
                "id":    r["record_id"],
                "actor": r["actor"],
                "before": (r.get("before_json") or "")[:80],
                "after":  (r.get("after_json")  or "")[:80],
            }
            for r in audit_rows
        ])
        st.dataframe(audit_df, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("最近 LLM Trace（含 finish_reason、token 與耗時）")
    col_fr, col_days = st.columns(2)
    with col_fr:
        finish_filter = st.selectbox(
            "finish_reason 過濾",
            ["(全部)", "end_turn", "max_tokens", "stop_sequence", "tool_use"],
            key="trace_fr",
        )
    with col_days:
        trace_days = st.slider("天數", 1, 30, 7, key="trace_days")
    fr_arg = None if finish_filter == "(全部)" else finish_filter
    trace_rows = get_recent_llm_traces(days=trace_days, limit=100, finish_reason=fr_arg)
    if not trace_rows:
        st.info("近期無 LLM Trace。")
    else:
        trace_df = pd.DataFrame([
            {
                "時間":      str(r["created_at"]),
                "agent":     r["agent_name"],
                "model":     r["model_name"].replace("claude-", "").replace("-20251001", ""),
                "in_tok":    r["input_tokens"],
                "out_tok":   r["output_tokens"],
                "think_tok": r["thinking_tokens"],
                "latency_ms": r["latency_ms"],
                "finish":    r["finish_reason"] or "—",
                "run_id":    (r["run_id"] or "")[:8],
            }
            for r in trace_rows
        ])
        st.dataframe(trace_df, use_container_width=True, hide_index=True)


# ── Tab 9: Shadow 比對 (Phase 1) ───────────────────────────────────────────────

with tab_shadow:
    st.subheader("Shadow 比對概覽")
    st.caption("Phase 1 多 agent 轉型：每個啟用 shadow 的 agent 會在每次 workflow 跑時同時跑「新版（含 tool-use 迴圈）」"
               "與舊版，這裡呈現兩者輸出的差異趨勢。production 永遠送舊版，新版資料只供事後比對。")

    shadow_days = st.slider("資料天數", 1, 60, 14, key="shadow_days")
    summary = get_shadow_summary_per_agent(days=shadow_days)
    if not summary:
        st.info("尚無 shadow runs — 請設定 .env 的 `SHADOW_AGENTS=tech_analyst,portfolio_manager` 並等下一次 workflow 執行。")
    else:
        sum_df = pd.DataFrame([
            {
                "agent":           r["agent_name"],
                "runs":            int(r["runs"] or 0),
                "avg divergence":  round(float(r["avg_divergence"] or 0), 3),
                "avg primary $":   round(float(r["avg_primary_cost"] or 0), 4),
                "avg shadow $":    round(float(r["avg_shadow_cost"] or 0), 4),
                "shadow errors":   int(r["shadow_errors"] or 0),
            }
            for r in summary
        ])
        st.dataframe(sum_df, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Divergence 趨勢")
    agent_filter = st.selectbox(
        "Agent", ["(全部)"] + [r["agent_name"] for r in summary] if summary
                else ["(全部)"],
        key="shadow_agent_filter",
    )
    arg = None if agent_filter == "(全部)" else agent_filter
    runs = get_recent_shadow_runs(agent_name=arg, days=shadow_days, limit=200)
    if not runs:
        st.info("無資料。")
    else:
        run_df = pd.DataFrame([
            {
                "time":       str(r["created_at"]),
                "agent":      r["agent_name"],
                "shadow ver": r["shadow_version"],
                "divergence": float(r["divergence_score"] or 0),
                "kind":       r["divergence_kind"] or "—",
                "primary $":  float(r["primary_cost_usd"] or 0),
                "shadow $":   float(r["shadow_cost_usd"] or 0),
                "error":      (r["shadow_error"] or "")[:60],
                "run_id":     (r["run_id"] or "")[:8],
            }
            for r in runs
        ])
        # Plot per-agent divergence over time
        try:
            plot_df = run_df.sort_values("time")[["time", "agent", "divergence"]]
            pivot = plot_df.pivot_table(index="time", columns="agent",
                                        values="divergence", aggfunc="mean")
            st.line_chart(pivot)
        except Exception:
            pass
        st.dataframe(run_df, use_container_width=True, hide_index=True)
