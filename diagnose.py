"""AI 故障诊断：基于大语言模型的工具调用（function calling）

普通做法：把告警丢给大模型，让它"猜"原因。
本项目做法：给大模型提供几个工具（ping、端口检测、白名单 Linux 命令、历史数据查询），
让它像运维工程师一样自己决定"先查什么、再查什么"，拿到证据后再下结论。

安全设计：AI 不能执行任意命令，只能从 SAFE_COMMANDS 白名单里选，且全部是只读命令。
"""
import calendar
import json
import os
import re
import subprocess
import time

from openai import OpenAI

from db import history, last_normal_ts
from inspector import check_http, check_port, ping

# ---------- 白名单命令（全部只读） ----------
SAFE_COMMANDS = {
    "service_status": "service {service} status",
    "listening_ports": "ss -tlnp",
    "firewall_rules": "iptables -L INPUT -n --line-numbers",
    "traffic_control": "tc qdisc show",
    "disk_usage": "df -h",
    "memory_usage": "free -m",
    "system_load": "uptime",
    # 错误日志不直接 tail：只返回本次异常开始后的记录，见 recent_error_log()
    "service_error_log": "tail /var/log/{service}/error.log（仅返回本次异常开始后的新记录）",
    "service_config": "grep -rnE 'listen|root' /etc/{service}/sites-enabled/",
    "web_root_permission": "ls -ld /var/www/html",
}

CATEGORIES = {
    "service_down": "服务进程停止",
    "firewall_block": "防火墙拦截",
    "network_latency": "网络时延/丢包劣化",
    "disk_full": "磁盘空间不足",
    "config_error": "服务配置错误（如监听端口不对）",
    "permission_error": "文件/目录权限错误",
    "other": "其他",
}

# ---------- 告诉大模型有哪些工具可用 ----------
TOOLS = [
    {"type": "function", "function": {
        "name": "ping", "description": "ping 目标主机，返回丢包率和平均时延",
        "parameters": {"type": "object",
                       "properties": {"host": {"type": "string"}},
                       "required": ["host"]}}},
    {"type": "function", "function": {
        "name": "check_port",
        "description": "检测 TCP 端口，返回是否连通；失败时 error 为 refused（无程序监听）或 timeout（数据包被丢弃）",
        "parameters": {"type": "object",
                       "properties": {"host": {"type": "string"},
                                      "port": {"type": "integer"}},
                       "required": ["host", "port"]}}},
    {"type": "function", "function": {
        "name": "check_http", "description": "访问网页，返回状态码与响应时间",
        "parameters": {"type": "object",
                       "properties": {"url": {"type": "string"}},
                       "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "run_safe_command",
        "description": "在被监控主机上执行一条白名单内的只读命令。可选："
                       + "；".join(f"{k}（{v}）" for k, v in SAFE_COMMANDS.items()),
        "parameters": {"type": "object",
                       "properties": {"name": {"type": "string",
                                               "enum": list(SAFE_COMMANDS)}},
                       "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "query_history",
        "description": "查询某个指标最近的历史巡检数据，用于判断是突发还是持续问题。"
                       "指标名如 ping_latency_ms、http_ms、port_80、http_ok、disk:/",
        "parameters": {"type": "object",
                       "properties": {"metric": {"type": "string"},
                                      "n": {"type": "integer"}},
                       "required": ["metric"]}}},
]

SYSTEM_PROMPT = """你是一名电信运营商的网络运维专家，负责对巡检告警做故障辅助诊断。

排查原则：
1. 按"网络层 → 传输层 → 应用层 → 系统资源"的顺序排查：先看 ping 时延和丢包，再看端口，再看服务状态、配置和日志，最后看磁盘、内存。
2. 每个结论都必须有工具返回的证据支撑，不要凭猜测下结论。
3. 同一种现象可能有多种原因（例如端口连接失败，可能是服务停止、防火墙、配置错误），下结论前要用工具排除其他可能。
4. 端口连接失败的经验：refused 通常表示没有进程监听，timeout 通常表示数据包被丢弃；但这只是经验，需要用服务状态、监听端口、防火墙规则等证据确认。
5. 可能同时存在多个相互独立的故障，要逐条核实所有告警，不要找到一个原因就停止。
6. 日志里可能残留历史记录，只有时间与当前告警吻合、且与当前现象一致的记录才能作为证据；每个故障类别都要有当前状态的直接证据（例如权限错误应当在当前的目录权限上得到确认）。
7. 只能使用提供的工具，命令只能从白名单中选择。

故障类别（categories 只能从中选择）：
service_down（服务进程停止）、firewall_block（防火墙拦截）、network_latency（网络时延/丢包劣化）、
disk_full（磁盘空间不足）、config_error（服务配置错误）、permission_error（文件/目录权限错误）、other（其他）

排查结束后，只输出一个 JSON 对象，不要输出任何其他内容：
{"categories": ["类别1", "可能有类别2"],
 "root_cause": "一句话说明根因，多个故障分别说明",
 "evidence": ["证据1", "证据2"],
 "suggestion": "处置建议，包含具体命令"}"""


def recent_error_log(path, since_ts=None, max_lines=20):
    """只返回 since_ts 之后的错误日志（since_ts = 上次巡检正常的时间）。

    为什么不直接 tail？日志会保留历史记录，直接 tail 可能把之前已经恢复的
    故障记录当成当前故障的证据（本项目评测中实际发生过）。
    since_ts 为 None 时退回旧行为（直接 tail），用于对比实验。
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return f"日志文件不存在：{path}"
    except PermissionError:
        return f"没有权限读取 {path}（需要 root）"
    if since_ts is None:
        return "\n".join(lines[-max_lines:]) or "（日志为空）"

    now, since = time.time(), int(since_ts)
    recent, older = [], 0
    for line in lines:
        m = re.match(r"(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})", line)
        if not m:
            continue
        st = time.strptime(m.group(1), "%Y/%m/%d %H:%M:%S")
        # 服务进程的时区可能和系统不一致（常见：服务按 UTC 写日志），两种解释都试一下
        if any(since <= ts <= now + 60 for ts in (time.mktime(st), calendar.timegm(st))):
            recent.append(line)
        else:
            older += 1
    head = (f"当前时间 {time.strftime('%H:%M:%S')}，"
            f"自上次巡检正常（{time.strftime('%H:%M:%S', time.localtime(since_ts))}）以来的错误日志：")
    tail = f"（另有 {older} 条更早的历史记录未显示）"
    if not recent:
        return f"{head}无{tail}"
    return head + "\n" + "\n".join(recent[-max_lines:]) + "\n" + tail


def run_safe_command(name, target, log_filter=True):
    service = target.get("service", "nginx")
    if name not in SAFE_COMMANDS:
        return f"拒绝执行：{name} 不在白名单内"
    if name == "service_error_log":
        since = None
        if log_filter:
            since = last_normal_ts(target["name"]) or (time.time() - 300)  # 没有健康记录时看最近 5 分钟
        return recent_error_log(f"/var/log/{service}/error.log", since)
    cmd = SAFE_COMMANDS[name].format(service=service)
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        out = (r.stdout + r.stderr).strip()
        return out[-2000:] or "（命令无输出）"
    except subprocess.TimeoutExpired:
        return "命令执行超时"


def call_tool(name, args, target, log_filter=True):
    """把 AI 的工具调用请求转成真正的函数调用"""
    if name == "ping":
        result = ping(args.get("host", target["host"]))
    elif name == "check_port":
        result = check_port(args.get("host", target["host"]), int(args["port"]))
    elif name == "check_http":
        result = check_http(args.get("url", target.get("http_url")))
    elif name == "run_safe_command":
        return run_safe_command(args.get("name", ""), target, log_filter)
    elif name == "query_history":
        result = history(target["name"], args["metric"], int(args.get("n", 10)))
    else:
        result = {"error": f"未知工具 {name}"}
    return json.dumps(result, ensure_ascii=False)


def parse_result(text):
    """从大模型回复中提取 JSON 结论，categories 统一成列表"""
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        data = json.loads(m.group(0))
    except (AttributeError, json.JSONDecodeError):
        data = {"root_cause": (text or "")[:200]}
    cats = data.get("categories", data.get("category", []))
    if isinstance(cats, str):
        cats = [cats]
    cats = sorted({c for c in cats if c in CATEGORIES}) or ["other"]
    data["categories"] = cats
    data["category"] = ",".join(cats)
    data.setdefault("root_cause", "")
    data.setdefault("evidence", [])
    data.setdefault("suggestion", "")
    return data


def diagnose(target, alerts, cfg, verbose=True):
    """AI 多步排查，返回 {category, root_cause, evidence, suggestion, steps, elapsed}"""
    llm = cfg["llm"]
    key = os.environ.get(llm["api_key_env"])
    if not key:
        raise RuntimeError(f"没有找到 API Key，请先执行：export {llm['api_key_env']}=你的key")
    client = OpenAI(api_key=key, base_url=llm["base_url"])

    alert_text = "\n".join(f"- {a['message']}" for a in alerts)
    user_msg = (f"巡检目标：{target['name']}（主机 {target['host']}，"
                f"服务 {target.get('service')}，端口 {target.get('ports')}，"
                f"URL {target.get('http_url')}）\n当前告警：\n{alert_text}\n请开始排查。")
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg}]
    steps, t0 = [], time.time()

    for _ in range(llm.get("max_steps", 6)):
        resp = client.chat.completions.create(
            model=llm["model"], messages=messages, tools=TOOLS, temperature=0)
        msg = resp.choices[0].message
        if not msg.tool_calls:          # AI 不再调用工具 → 给出了最终结论
            return _finish(msg.content, steps, t0)

        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [{"id": tc.id, "type": "function",
                                         "function": {"name": tc.function.name,
                                                      "arguments": tc.function.arguments}}
                                        for tc in msg.tool_calls]})
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = call_tool(tc.function.name, args, target, llm.get("log_filter", True))
            if verbose:
                print(f"      AI 调用 {tc.function.name}({args}) → {result[:120]}")
            steps.append({"tool": tc.function.name, "args": args, "result": result[:800]})
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    # 达到最大步数，强制给结论
    messages.append({"role": "user", "content": "已达到最大排查步数，请根据已有证据直接输出 JSON 结论。"})
    resp = client.chat.completions.create(
        model=llm["model"], messages=messages, tools=TOOLS, tool_choice="none", temperature=0)
    return _finish(resp.choices[0].message.content, steps, t0)


def _finish(text, steps, t0):
    result = parse_result(text)
    result["steps"] = steps
    result["elapsed"] = round(time.time() - t0, 1)
    return result


def rule_simple(alerts):
    """对照组 1：简单规则——只看"哪类指标告警"，按优先级给出一个结论。

    很多简单监控系统就是这样做的：端口不通 → 服务挂了。
    """
    if not alerts:
        return {"normal"}
    metrics = [a["metric"] for a in alerts]
    if any(m.startswith("disk:") for m in metrics):
        return {"disk_full"}
    if any(m in ("ping_latency_ms", "http_ms", "ping_loss") for m in metrics):
        return {"network_latency"}
    if any(m.startswith("port_") or m == "http_ok" for m in metrics):
        return {"service_down"}
    return {"other"}


def rule_expert(alerts, metrics):
    """对照组 2：专家规则——模拟运维人员手写的决策树。

    比简单规则强在两点：
      1. 逐个子系统独立判断，能同时报出多个故障；
      2. 利用连接失败类型：timeout → 防火墙，refused → 服务停止。
    它代表"把已知故障经验写成规则"的上限：写过的模式判得准，
    没写过的模式（新型故障、现象相同原因不同）就会判错。
    """
    if not alerts:
        return {"normal"}
    cats = set()
    alerted = {a["metric"] for a in alerts}
    if any(m.startswith("disk:") for m in alerted):
        cats.add("disk_full")
    if alerted & {"ping_latency_ms", "http_ms", "ping_loss"}:
        cats.add("network_latency")
    port_alerts = [m for m in alerted if m.startswith("port_")]
    for m in port_alerts:
        detail = metrics.get(m, (None, ""))[1]
        if detail == "timeout":
            cats.add("firewall_block")
        elif detail == "refused":
            cats.add("service_down")
        else:
            cats.add("other")
    if "http_ok" in alerted and not port_alerts:
        cats.add("other")      # 端口通但网页异常：规则无法判断具体原因
    return cats or {"other"}
