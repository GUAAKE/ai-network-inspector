"""巡检层：检测连通性、端口、网页和磁盘

每个检测函数都返回一个字典，既给巡检主流程用，也作为"工具"给 AI 调用。
"""
import re
import shutil
import socket
import subprocess
import time

import requests


def ping(host, count=3, timeout=3):
    """调用系统 ping 命令，解析丢包率和平均时延"""
    try:
        out = subprocess.run(
            ["ping", "-c", str(count), "-W", str(timeout), host],
            capture_output=True, text=True, timeout=count * timeout + 5).stdout
    except subprocess.TimeoutExpired:
        return {"host": host, "reachable": False, "loss": 100.0, "latency_ms": None}
    except FileNotFoundError:
        return {"host": host, "reachable": False, "loss": 100.0, "latency_ms": None,
                "error": "系统没有 ping 命令，请先安装 iputils-ping"}

    # 例：3 packets transmitted, 3 received, 0% packet loss
    m_loss = re.search(r"([\d.]+)% packet loss", out)
    loss = float(m_loss.group(1)) if m_loss else 100.0
    # 例：rtt min/avg/max/mdev = 0.031/0.045/0.060/0.012 ms  → 取第二个数（平均值）
    m_rtt = re.search(r"= [\d.]+/([\d.]+)/", out)
    latency = float(m_rtt.group(1)) if m_rtt else None
    return {"host": host, "reachable": loss < 100, "loss": loss, "latency_ms": latency}


def check_port(host, port, timeout=3):
    """尝试建立 TCP 连接。

    注意区分两种失败：
      refused（被拒绝）：对方主机在，但这个端口没有程序在监听 → 通常是服务没启动
      timeout（超时）  ：数据包有去无回 → 通常是被防火墙丢弃，或网络不通
    """
    start = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"host": host, "port": port, "open": True, "error": None,
                    "ms": round((time.time() - start) * 1000, 2)}
    except TimeoutError:
        err = "timeout"
    except ConnectionRefusedError:
        err = "refused"
    except OSError as e:
        err = str(e)
    return {"host": host, "port": port, "open": False, "error": err,
            "ms": round((time.time() - start) * 1000, 2)}


def check_http(url, timeout=3):
    """访问网页，记录状态码和响应时间"""
    start = time.time()
    try:
        r = requests.get(url, timeout=timeout)
        ms = round((time.time() - start) * 1000, 2)
        ok = r.status_code < 400       # 4xx/5xx 都算访问失败
        return {"url": url, "ok": ok, "status": r.status_code, "ms": ms,
                "detail": f"HTTP {r.status_code}, {ms}ms"}
    except requests.exceptions.Timeout:
        detail = "timeout"
    except requests.exceptions.ConnectionError as e:
        detail = "refused" if "refused" in str(e).lower() else "connection error"
    except Exception as e:  # noqa: BLE001
        detail = str(e)[:100]
    return {"url": url, "ok": False, "status": None,
            "ms": round((time.time() - start) * 1000, 2), "detail": detail}


def disk_percent(mount):
    """磁盘使用率（%），挂载点不存在时返回 None"""
    try:
        u = shutil.disk_usage(mount)
        return round(u.used / u.total * 100, 1)
    except (FileNotFoundError, ZeroDivisionError):
        return None


def inspect_target(t, cfg):
    """对一个目标做完整巡检，返回 {指标名: (值, 说明)}"""
    ins = cfg["inspection"]
    m = {}

    p = ping(t["host"], ins["ping_count"], ins["timeout_seconds"])
    m["ping_loss"] = (p["loss"], p.get("error", ""))
    m["ping_latency_ms"] = (p["latency_ms"], "")

    for port in t.get("ports", []):
        r = check_port(t["host"], port, ins["timeout_seconds"])
        m[f"port_{port}"] = (1.0 if r["open"] else 0.0,
                             r["error"] or f"{r['ms']}ms")

    if t.get("http_url"):
        h = check_http(t["http_url"], ins["timeout_seconds"])
        m["http_ok"] = (1.0 if h["ok"] else 0.0, h["detail"])
        m["http_ms"] = (h["ms"] if h["ok"] else None, "")

    for mount in t.get("disk_mounts", []):
        pct = disk_percent(mount)
        if pct is not None:
            m[f"disk:{mount}"] = (pct, "")

    return m
