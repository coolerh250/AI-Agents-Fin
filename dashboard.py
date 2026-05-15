"""
dashboard.py
Streamlit lightweight dashboard for the Taiwan Stock Futures Analysis Team.
Run: uv run streamlit run dashboard.py --server.port 8501
"""
import pandas as pd
import streamlit as st

from database_tools import get_cost_summary, get_cost_trend, get_recent_accuracy

st.set_page_config(page_title="量化工作室看板", layout="wide")
st.title("📈 台股期貨量化工作室")

rows = get_recent_accuracy(30)

if not rows:
    st.warning("尚無回測數據 — 請先執行 backtest_agent.py")
    st.stop()

df = pd.DataFrame(rows)
df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
df = df.sort_values("trade_date")

col1, col2 = st.columns(2)

with col1:
    st.subheader("預測方向分佈")
    counts = df["gap_direction"].value_counts()
    st.bar_chart(counts)

with col2:
    st.subheader("預測 vs 實際跳空幅度 (%)")
    chart_df = (
        df[["trade_date", "predicted_gap_pct", "actual_gap_pct"]]
        .dropna(subset=["predicted_gap_pct"])
        .set_index("trade_date")
    )
    st.line_chart(chart_df)

st.subheader("近期回測明細")
display_df = df.rename(columns={
    "trade_date":        "日期",
    "gap_direction":     "預測方向",
    "predicted_gap_pct": "預測跳空%",
    "actual_gap_pct":    "實際跳空%",
    "open_price":        "開盤價",
    "close_price":       "收盤價",
})
st.dataframe(display_df, use_container_width=True)

# ── Cost Analytics ────────────────────────────────────────────────────────────
st.divider()
st.header("💰 API 成本分析儀表板")

cost_rows  = get_cost_summary(30)
trend_rows = get_cost_trend(30)

if not cost_rows:
    st.info("尚無成本記錄 — 請先執行含成本追蹤的 investment_workflow.py")
else:
    cost_df  = pd.DataFrame(cost_rows)
    cost_df["total_cost_usd"]  = cost_df["total_cost_usd"].astype(float)
    cost_df["avg_latency_ms"]  = cost_df["avg_latency_ms"].astype(float)

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
    detail_df = cost_df.rename(columns={
        "agent_name":       "節點",
        "model_name":       "模型",
        "total_input":      "輸入 Token",
        "total_output":     "輸出 Token",
        "total_cost_usd":   "總成本 (USD)",
        "avg_latency_ms":   "平均耗時 (ms)",
        "runs":             "執行次數",
    })
    st.dataframe(detail_df, use_container_width=True)
