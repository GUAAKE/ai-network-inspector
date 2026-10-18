"""故障注入工具（需要 root 权限）

用法：
  sudo ./venv/bin/python fault_inject.py list                   查看所有场景
  sudo ./venv/bin/python fault_inject.py inject firewall_reject 注入某个场景
  sudo ./venv/bin/python fault_inject.py recover firewall_reject
  sudo ./venv/bin/python fault_inject.py recover all            恢复所有故障

场景分三组：
  known    已知单一故障：规则编写时就考虑过的典型故障
  confuse  易混淆故障  ：和已知故障【现象相同、原因不同】，需要进一步取证才能区分
  compound 复合故障    ：两个独立故障同时发生，需要全部找出
"""
import subprocess
import sys

NGINX_SITE = "/etc/nginx/sites-enabled/default"
# sed 加 --follow-symlinks，避免把软链接改成普通文件
_LISTEN_TO_8081 = (f"grep -q 'listen 80 default_server' {NGINX_SITE} && "
                   f"sed -i --follow-symlinks -e 's/listen 80 default_server/listen 8081 default_server/' "
                   f"-e 's/listen \\[::\\]:80 default_server/listen [::]:8081 default_server/' {NGINX_SITE} "
                   f"&& nginx -s reload")
_LISTEN_TO_80 = (f"sed -i --follow-symlinks -e 's/listen 8081 default_server/listen 80 default_server/' "
                 f"-e 's/listen \\[::\\]:8081 default_server/listen [::]:80 default_server/' {NGINX_SITE} "
                 f"&& (nginx -s reload 2>/dev/null || true)")

# ---------- 基础故障 ----------
BASE = {
    # 已知单一故障
    "service_down": {
        "desc": "nginx 服务停止", "category": "service_down",
        "inject": "service nginx stop", "recover": "service nginx start"},
    "firewall_block": {
        "desc": "防火墙 DROP 80 端口（静默丢包 → 连接超时）", "category": "firewall_block",
        "inject": "iptables -I INPUT -p tcp --dport 80 -j DROP",
        "recover": "iptables -D INPUT -p tcp --dport 80 -j DROP"},
    "network_latency": {
        "desc": "本机网卡增加 300ms 时延", "category": "network_latency",
        "inject": "tc qdisc add dev lo root netem delay 300ms",
        "recover": "tc qdisc del dev lo root"},
    "disk_full": {
        "desc": "测试盘写满到 95%", "category": "disk_full",
        "inject": "mountpoint -q /mnt/inspect_disk && dd if=/dev/zero of=/mnt/inspect_disk/fill.bin bs=1M count=95 status=none",
        "recover": "rm -f /mnt/inspect_disk/fill.bin",
        "hint": "测试盘未挂载（WSL 重启后会失效），请先运行 bash setup.sh"},
    # 易混淆故障
    "firewall_reject": {
        "desc": "防火墙 REJECT 80 端口（主动拒绝 → 连接 refused，现象和服务停止一样）",
        "category": "firewall_block",
        "inject": "iptables -I INPUT -p tcp --dport 80 -j REJECT --reject-with tcp-reset",
        "recover": "iptables -D INPUT -p tcp --dport 80 -j REJECT --reject-with tcp-reset"},
    "config_error": {
        "desc": "nginx 配置被改成监听 8081（服务在运行，但 80 端口 refused）",
        "category": "config_error",
        "inject": _LISTEN_TO_8081, "recover": _LISTEN_TO_80},
    "permission_error": {
        "desc": "网站目录权限被改成 000（端口正常，网页返回 403）",
        "category": "permission_error",
        "inject": "chmod 000 /var/www/html", "recover": "chmod 755 /var/www/html"},
}

# ---------- 评测场景（单一故障 + 复合故障） ----------
SCENARIOS = {"none": {"desc": "无故障（对照组）", "group": "control", "parts": []}}
for _k in ("service_down", "firewall_block", "network_latency", "disk_full"):
    SCENARIOS[_k] = {"desc": BASE[_k]["desc"], "group": "known", "parts": [_k]}
for _k in ("firewall_reject", "config_error", "permission_error"):
    SCENARIOS[_k] = {"desc": BASE[_k]["desc"], "group": "confuse", "parts": [_k]}
SCENARIOS.update({
    "service_down+disk_full": {
        "desc": "服务停止 + 磁盘写满", "group": "compound",
        "parts": ["service_down", "disk_full"]},
    "firewall_block+network_latency": {
        "desc": "防火墙丢包 + 网络时延", "group": "compound",
        "parts": ["network_latency", "firewall_block"]},
    "permission_error+network_latency": {
        "desc": "目录权限错误 + 网络时延", "group": "compound",
        "parts": ["network_latency", "permission_error"]},
})

GROUP_NAMES = {"control": "无故障对照", "known": "已知单一故障",
               "confuse": "易混淆故障", "compound": "复合故障"}


def expected_categories(scenario):
    parts = SCENARIOS[scenario]["parts"]
    return {BASE[p]["category"] for p in parts} or {"normal"}


def sh(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def inject(scenario):
    """注入一个场景的全部故障；任何一个失败就回滚已注入的部分"""
    done = []
    for p in SCENARIOS[scenario]["parts"]:
        ok, msg = sh(BASE[p]["inject"])
        if not ok:
            for q in reversed(done):
                sh(BASE[q]["recover"])
            return False, f"{p} 注入失败：{msg or BASE[p].get('hint', '命令返回非 0')}"
        done.append(p)
    return True, ""


def recover(scenario):
    for p in reversed(SCENARIOS[scenario]["parts"]):
        sh(BASE[p]["recover"])
    return True, ""


def recover_all():
    """把所有故障都恢复一遍（忽略"本来就没注入"导致的报错），最后确保 nginx 在运行"""
    for name, f in BASE.items():
        if name.startswith("firewall"):      # 可能残留多条规则，删到删不动为止
            for _ in range(10):
                if not sh(f["recover"])[0]:
                    break
        elif name != "service_down":
            sh(f["recover"])
    sh("pgrep -x nginx >/dev/null || service nginx start")
    sh("nginx -s reload")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] == "list":
        for k, v in SCENARIOS.items():
            print(f"[{GROUP_NAMES[v['group']]}] {k:34s} {v['desc']}")
        sys.exit(0)
    action, name = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
    if action == "recover" and name == "all":
        recover_all()
        print("已恢复全部故障")
    elif name in SCENARIOS and action in ("inject", "recover"):
        ok, msg = (inject if action == "inject" else recover)(name)
        print(("成功" if ok else "失败") + (f"：{msg}" if msg else ""))
    else:
        print(__doc__)
