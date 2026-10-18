"""巡检主程序

用法：
  python main.py --once        只巡检一次
  python main.py               按 config.yaml 的间隔持续巡检
  python main.py --ai          持续巡检，出现告警时自动调用 AI 诊断
"""
import argparse
import time

from alert import check_alerts
from common import load_config
from db import init_db, save_alerts, save_diagnosis, save_metrics
from inspector import inspect_target
from notify import push

COOLDOWN_SECONDS = 600   # 同一组告警 10 分钟内只诊断、推送一次，避免刷屏


def run_once(cfg, use_ai=False, cooldown=None):
    cooldown = cooldown if cooldown is not None else {}
    for t in cfg["targets"]:
        metrics = inspect_target(t, cfg)
        alerts = check_alerts(t["name"], metrics, cfg)        # 先判断告警
        save_metrics(t["name"], metrics, {a["metric"] for a in alerts})  # 再存数据

        now = time.strftime("%H:%M:%S")
        lat = metrics.get("ping_latency_ms", (None,))[0]
        lat_txt = f"{lat:.2f}ms" if lat is not None else "-"
        print(f"[{now}] {t['name']}: ping {lat_txt} | "
              + ("正常" if not alerts else f"{len(alerts)} 条告警"))
        if not alerts:
            continue

        save_alerts(t["name"], alerts)
        for a in alerts:
            print(f"   ⚠ {a['message']}")

        key = (t["name"], tuple(sorted(a["metric"] for a in alerts)))
        if time.time() - cooldown.get(key, 0) < COOLDOWN_SECONDS:
            print("   （同类告警冷却中，本次不重复诊断/推送）")
            continue
        cooldown[key] = time.time()

        text = f"【巡检告警】{t['name']}\n" + "\n".join(a["message"] for a in alerts)
        if use_ai:
            from diagnose import diagnose
            print("   AI 开始诊断……")
            r = diagnose(t, alerts, cfg)
            save_diagnosis(t["name"], alerts, r)
            print(f"   ✅ AI 结论：[{r['category']}] {r['root_cause']}")
            print(f"      建议：{r['suggestion']}")
            text += f"\n\n【AI 诊断】{r['root_cause']}\n处置建议：{r['suggestion']}"
        push(text, cfg)


def main():
    parser = argparse.ArgumentParser(description="网络智能巡检")
    parser.add_argument("--once", action="store_true", help="只巡检一次")
    parser.add_argument("--ai", action="store_true", help="告警时调用 AI 诊断")
    args = parser.parse_args()

    cfg = load_config()
    init_db()
    if args.once:
        run_once(cfg, args.ai)
        return

    cooldown = {}
    interval = cfg["inspection"]["interval_seconds"]
    print(f"开始持续巡检，间隔 {interval} 秒，按 Ctrl+C 停止")
    try:
        while True:
            run_once(cfg, args.ai, cooldown)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
