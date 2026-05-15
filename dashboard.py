"""
dashboard.py
Streamlit lightweight dashboard for the Taiwan Stock Futures Analysis Team.
Run: uv run streamlit run dashboard.py --server.port 8501
"""
import pandas as pd
import streamlit as st

from database_tools import get_recent_accuracy

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
