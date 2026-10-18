"""可视化看板（Streamlit）

用法：./venv/bin/streamlit run dashboard.py
然后浏览器打开终端提示的地址（一般是 http://localhost:8501）
"""
import json
import os
import sqlite3
import time

import pandas as pd
import streamlit as st

from common import BASE_DIR, load_config
from db import DB_PATH

st.set_page_config(page_title="网络智能巡检", layout="wide")
st.title("网络智能巡检与故障辅助诊断")

if not os.path.exists(DB_PATH):
    st.info("还没有数据，请先运行：python main.py")
    st.stop()

conn = sqlite3.connect(DB_PATH)
hours = st.sidebar.slider("时间范围（小时）", 1, 72, 24)
since = time.time() - hours * 3600


def to_time(s):
    return pd.to_datetime(s, unit="s", utc=True).dt.tz_convert("Asia/Shanghai")


df = pd.read_sql("SELECT * FROM inspections WHERE ts > ?", conn, params=(since,))
if df.empty:
    st.info("所选时间范围内没有巡检数据")
    st.stop()
df["time"] = to_time(df["ts"])

# ---------- 当前状态 ----------
st.subheader("当前状态")
latest = df.sort_values("ts").groupby(["target", "metric"]).tail(1)
cols = st.columns(min(len(latest), 6))
for col, (_, row) in zip(cols * 10, latest.iterrows()):
    flag = "🔴" if row["is_anomaly"] else "🟢"
    val = "-" if pd.isna(row["value"]) else f"{row['value']:.2f}"
    col.metric(f"{flag} {row['metric']}", val)

# ---------- 时延趋势 ----------
st.subheader("时延趋势（ms）")
lat = df[df["metric"].isin(["ping_latency_ms", "http_ms"])]
if not lat.empty:
    st.line_chart(lat.pivot_table(index="time", columns="metric", values="value"))

# ---------- 告警记录 ----------
st.subheader("告警记录")
alerts = pd.read_sql("SELECT * FROM alerts WHERE ts > ? ORDER BY ts DESC LIMIT 100",
                     conn, params=(since,))
if alerts.empty:
    st.write("暂无告警 ✅")
else:
    alerts["time"] = to_time(alerts["ts"])
    st.dataframe(alerts[["time", "target", "message"]], width="stretch")

# ---------- AI 诊断 ----------
st.subheader("AI 诊断记录")
diag = pd.read_sql("SELECT * FROM diagnoses WHERE ts > ? ORDER BY ts DESC LIMIT 30",
                   conn, params=(since,))
if diag.empty:
    st.write("暂无诊断记录")
for _, d in diag.iterrows():
    t = pd.to_datetime(d["ts"], unit="s", utc=True).tz_convert("Asia/Shanghai")
    with st.expander(f"{t:%m-%d %H:%M:%S}  [{d['category']}]  {d['root_cause']}"):
        st.write("**告警：**", d["alert_summary"])
        st.write("**处置建议：**", d["suggestion"])
        st.write(f"**排查过程（耗时 {d['elapsed']}s）：**")
        for i, s in enumerate(json.loads(d["steps"]), 1):
            st.code(f"第{i}步 {s['tool']}({s['args']})\n→ {s['result'][:300]}")

# ---------- 评测结果 ----------
csv_path = os.path.join(BASE_DIR, "eval_results.csv")
if os.path.exists(csv_path):
    st.subheader("故障注入评测结果")
    ev = pd.read_csv(csv_path)
    if "rule_expert_ok" in ev.columns:
        acc_cols = {"简单规则": "rule_simple_ok", "专家规则": "rule_expert_ok", "AI诊断": "ai_ok"}
        for key, title in (("group", "按场景组"), ("scenario", "按场景")):
            summary = ev.groupby(key, sort=False).agg(
                次数=("round", "count"), **{k: (v, "mean") for k, v in acc_cols.items()})
            summary[list(acc_cols)] = (summary[list(acc_cols)] * 100).round(0)
            st.write(f"**{title}（准确率 %）**")
            st.dataframe(summary, width="stretch")
            if key == "group":
                st.bar_chart(summary[list(acc_cols)])
        wrong = ev[~ev["ai_ok"].astype(bool) & (ev["ai"] != "-")]
        if not wrong.empty:
            st.write("**AI 判错的案例**")
            st.dataframe(wrong[["scenario", "round", "expected", "ai"]], width="stretch")
    else:
        st.info("这是旧版评测结果，请重新运行 evaluate.py")

# ---------- 自然语言查询 ----------
st.subheader("用中文问问运维数据")
q = st.text_input("例如：过去这段时间哪个指标最不稳定？有没有需要关注的问题？")
if q:
    cfg = load_config()
    key = os.environ.get(cfg["llm"]["api_key_env"])
    if not key:
        st.error(f"未设置环境变量 {cfg['llm']['api_key_env']}")
    else:
        from openai import OpenAI
        stats = df.groupby(["target", "metric"])["value"].agg(
            ["count", "mean", "min", "max"]).round(2).to_string()
        anomaly = df.groupby("metric")["is_anomaly"].sum().to_string()
        recent_alerts = "\n".join(alerts["message"].head(20)) if not alerts.empty else "无"
        context = (f"时间范围：最近{hours}小时\n指标统计：\n{stats}\n"
                   f"各指标异常次数：\n{anomaly}\n最近告警：\n{recent_alerts}")
        client = OpenAI(api_key=key, base_url=cfg["llm"]["base_url"])
        with st.spinner("AI 分析中……"):
            resp = client.chat.completions.create(
                model=cfg["llm"]["model"], temperature=0,
                messages=[{"role": "system",
                           "content": "你是网络运维助手，只根据给出的巡检统计数据回答，数据里没有的不要编造。"},
                          {"role": "user", "content": f"{context}\n\n问题：{q}"}])
        st.write(resp.choices[0].message.content)
