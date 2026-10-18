"""数据库层：用 SQLite 保存巡检数据、告警和 AI 诊断结果

SQLite 不需要安装数据库服务，整个数据库就是一个 inspection.db 文件。
巡检数据采用"长表"格式：每个指标一行，方便按指标查询历史。
"""
import json
import os
import sqlite3
import time

from common import BASE_DIR

DB_PATH = os.path.join(BASE_DIR, "inspection.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS inspections (
    ts REAL,            -- 时间戳（秒）
    target TEXT,        -- 巡检目标名
    metric TEXT,        -- 指标名，如 ping_latency_ms、port_80、disk:/
    value REAL,         -- 指标值
    detail TEXT,        -- 补充信息，如错误原因
    is_anomaly INTEGER DEFAULT 0   -- 是否触发了告警（异常值不参与计算动态阈值）
);
CREATE INDEX IF NOT EXISTS idx_insp ON inspections(target, metric, ts);

CREATE TABLE IF NOT EXISTS alerts (
    ts REAL, target TEXT, metric TEXT, value REAL, threshold REAL, message TEXT
);

CREATE TABLE IF NOT EXISTS diagnoses (
    ts REAL, target TEXT, alert_summary TEXT, category TEXT,
    root_cause TEXT, suggestion TEXT, steps TEXT, elapsed REAL
);
"""


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with connect() as c:
        c.executescript(SCHEMA)


def save_metrics(target, metrics, anomalies=(), ts=None):
    """metrics: {指标名: (值, 说明)}；anomalies: 本次触发告警的指标名集合"""
    ts = ts or time.time()
    rows = [(ts, target, m, v, d, 1 if m in anomalies else 0)
            for m, (v, d) in metrics.items()]
    with connect() as c:
        c.executemany("INSERT INTO inspections VALUES (?,?,?,?,?,?)", rows)


def recent_normal_values(target, metric, n):
    """取最近 n 条【正常】数据，用于计算动态阈值"""
    with connect() as c:
        rows = c.execute(
            "SELECT value FROM inspections WHERE target=? AND metric=? "
            "AND is_anomaly=0 AND value IS NOT NULL ORDER BY ts DESC LIMIT ?",
            (target, metric, n)).fetchall()
    return [r["value"] for r in rows]


def last_normal_ts(target):
    """上一次【所有指标都正常】的巡检时间；即本次异常开始之前的最后一个健康时刻"""
    with connect() as c:
        row = c.execute("SELECT ts FROM inspections WHERE target=? GROUP BY ts "
                        "HAVING MAX(is_anomaly)=0 ORDER BY ts DESC LIMIT 1", (target,)).fetchone()
    return row["ts"] if row else None


def history(target, metric, n=10):
    """取最近 n 条数据（含异常），给 AI 查询用"""
    with connect() as c:
        rows = c.execute(
            "SELECT ts, value, detail, is_anomaly FROM inspections "
            "WHERE target=? AND metric=? ORDER BY ts DESC LIMIT ?",
            (target, metric, n)).fetchall()
    return [{"time": time.strftime("%H:%M:%S", time.localtime(r["ts"])),
             "value": r["value"], "detail": r["detail"],
             "anomaly": bool(r["is_anomaly"])} for r in rows]


def save_alerts(target, alerts, ts=None):
    ts = ts or time.time()
    with connect() as c:
        c.executemany("INSERT INTO alerts VALUES (?,?,?,?,?,?)",
                      [(ts, target, a["metric"], a["value"], a["threshold"], a["message"])
                       for a in alerts])


def save_diagnosis(target, alerts, result):
    summary = "；".join(a["message"] for a in alerts)
    with connect() as c:
        c.execute("INSERT INTO diagnoses VALUES (?,?,?,?,?,?,?,?)",
                  (time.time(), target, summary, result["category"],
                   result["root_cause"], result["suggestion"],
                   json.dumps(result["steps"], ensure_ascii=False), result["elapsed"]))
