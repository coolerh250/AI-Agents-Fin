"""
dashboard.py
Streamlit lightweight dashboard for the Taiwan Stock Futures Analysis Team.
Run: uv run streamlit run dashboard.py --server.port 8501
"""
import json
from datetime import date

import pandas as pd
import streamlit as st

from database_tools import (
    add_portfolio_item,
    delete_portfolio_item,
    get_cost_summary,
    get_cost_trend,
    get_portfolio,
    get_recent_accuracy,
    save_actual,
    update_portfolio_item,
)

st.set_page_config(page_title="量化工作室看板", layout="wide")
st.title("📈 台股期貨量化工作室")


# ── Cached P&L fetch (5-minute TTL) ──────────────────────────────────────────

@st.cache_data(ttl=300)
def _fetch_pnl(holdings_json: str) -> list[dict]:
    from portfolio_tools import calculate_pnl
    return calculate_pnl(json.loads(holdings_json))


def load_pnl() -> list[dict]:
    holdings = get_portfolio()
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

tab_accuracy, tab_cost, tab_portfolio = st.tabs([
    "📊 預測準確度",
    "💰 API 成本分析",
    "💼 個人持倉管理",
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

    if not cost_rows:
        st.info("尚無成本記錄 — 請先執行含成本追蹤的 investment_workflow.py")
    else:
        cost_df = pd.DataFrame(cost_rows)
        cost_df["total_cost_usd"] = cost_df["total_cost_usd"].astype(float)
        cost_df["avg_latency_ms"] = cost_df["avg_latency_ms"].astype(float)

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

        st.subheader("節點效能明細")
        st.dataframe(
            cost_df.rename(columns={
                "agent_name":     "節點",
                "model_name":     "模型",
                "total_input":    "輸入 Token",
                "total_output":   "輸出 Token",
                "total_cost_usd": "總成本 (USD)",
                "avg_latency_ms": "平均耗時 (ms)",
                "runs":           "執行次數",
            }),
            use_container_width=True,
        )


# ── Tab 3: Portfolio Management ───────────────────────────────────────────────

with tab_portfolio:
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
                )
                if ok:
                    st.success(f"已新增 {new_stock.strip().upper()}")
                    st.cache_data.clear()
                    st.rerun()
                else:
                    st.error("新增失敗（相同代碼+成本已存在）")

    # ── Holdings P&L (right column) ──────────────────────────────────────────
    with col_main:
        pnl_rows = load_pnl()

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
                    "成本(元)": st.column_config.NumberColumn("成本(元)", disabled=True),
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
                    delete_portfolio_item(options[selected])
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
