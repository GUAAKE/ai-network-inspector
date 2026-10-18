"""故障注入评测：自动注入故障 → 巡检 → 三种方法诊断 → 恢复，统计准确率

三种诊断方法：
  简单规则：只看告警指标类型（端口不通 → 服务停止）
  专家规则：运维经验决策树（timeout → 防火墙，refused → 服务停止，可报多个故障）
  AI 诊断 ：大模型调用工具多步取证

判定标准：诊断出的故障类别集合必须与实际【完全一致】才算正确
（复合故障漏报一个、或单一故障多报一个，都算错）。

用法（需要 root）：
  sudo DEEPSEEK_API_KEY=$DEEPSEEK_API_KEY ./venv/bin/python evaluate.py --rounds 3
  sudo ./venv/bin/python evaluate.py --no-ai                  只测两种规则（不花 API 费用）
  sudo ... evaluate.py --groups confuse compound              只测某几组场景
  sudo ... evaluate.py --scenarios firewall_reject config_error
"""
import argparse
import csv
import os
import sys
import time

from alert import check_alerts
from common import BASE_DIR, load_config
from db import init_db, save_alerts, save_diagnosis, save_metrics
from diagnose import rule_expert, rule_simple
from fault_inject import (GROUP_NAMES, SCENARIOS, expected_categories, inject,
                          recover, recover_all)
from inspector import inspect_target

RESULT_CSV = os.path.join(BASE_DIR, "eval_results.csv")
METHODS = [("rule_simple", "简单规则"), ("rule_expert", "专家规则"), ("ai", "AI诊断")]


def fmt(cats):
    return ",".join(sorted(cats))


def inspect_and_alert(t, cfg):
    metrics = inspect_target(t, cfg)
    alerts = check_alerts(t["name"], metrics, cfg)
    save_metrics(t["name"], metrics, {a["metric"] for a in alerts})
    if alerts:
        save_alerts(t["name"], alerts)
    return metrics, alerts


def print_summary(rows, use_ai):
    methods = METHODS if use_ai else METHODS[:2]

    def acc(sub, key):
        return sum(r[f"{key}_ok"] for r in sub) / len(sub) * 100

    header = f"{'':36s}{'次数':>4s}" + "".join(f"{name:>10s}" for _, name in methods)
    print("\n" + "=" * 80 + "\n按场景：\n" + header)
    for sc in dict.fromkeys(r["scenario"] for r in rows):
        sub = [r for r in rows if r["scenario"] == sc]
        print(f"{sc:38s}{len(sub):>4d}" + "".join(f"{acc(sub, k):>11.0f}%" for k, _ in methods))

    print("\n按场景组：\n" + header)
    for g in dict.fromkeys(r["group"] for r in rows):
        sub = [r for r in rows if r["group"] == g]
        print(f"{GROUP_NAMES[g]:32s}{len(sub):>4d}" + "".join(f"{acc(sub, k):>11.0f}%" for k, _ in methods))
    print(f"{'总计':34s}{len(rows):>4d}" + "".join(f"{acc(rows, k):>11.0f}%" for k, _ in methods))

    if use_ai:
        diag = [r for r in rows if r["ai_steps"] > 0]
        if diag:
            print(f"\nAI 平均排查步数 {sum(r['ai_steps'] for r in diag) / len(diag):.1f}，"
                  f"平均耗时 {sum(r['ai_seconds'] for r in diag) / len(diag):.1f}s")
        wrong = [r for r in rows if not r["ai_ok"]]
        if wrong:
            print("\nAI 判错的案例（面试时可以重点分析）：")
            for r in wrong:
                print(f"  {r['scenario']} 第{r['round']}轮：实际 {r['expected']}，AI 判为 {r['ai']}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--no-ai", action="store_true")
    parser.add_argument("--groups", nargs="+", choices=list(GROUP_NAMES))
    parser.add_argument("--scenarios", nargs="+", choices=list(SCENARIOS))
    args = parser.parse_args()

    if os.geteuid() != 0:
        sys.exit("需要 root 权限，请用：sudo DEEPSEEK_API_KEY=$DEEPSEEK_API_KEY ./venv/bin/python evaluate.py")

    scenarios = args.scenarios or [s for s, v in SCENARIOS.items()
                                   if not args.groups or v["group"] in args.groups]
    use_ai = not args.no_ai
    cfg = load_config()
    init_db()
    t = cfg["targets"][0]
    if use_ai:
        from diagnose import diagnose

    recover_all()
    time.sleep(2)
    warm = cfg["alert"]["min_history"] + 5
    print(f"预热：采集 {warm} 次正常数据，建立动态阈值基线……")
    for _ in range(warm):
        inspect_and_alert(t, cfg)
        time.sleep(0.5)

    rows = []
    for sc in scenarios:
        info = SCENARIOS[sc]
        exp = expected_categories(sc)
        print(f"\n===== [{GROUP_NAMES[info['group']]}] {sc}：{info['desc']}  （正确答案：{fmt(exp)}）")
        for r in range(args.rounds):
            ok, msg = inject(sc)
            if not ok:
                print(f"  注入失败，跳过该场景：{msg}")
                break
            time.sleep(2)

            metrics, alerts = inspect_and_alert(t, cfg)
            simple, expert = rule_simple(alerts), rule_expert(alerts, metrics)
            ai, elapsed, steps = None, 0.0, 0
            if use_ai:
                if alerts:
                    try:
                        res = diagnose(t, alerts, cfg, verbose=False)
                        save_diagnosis(t["name"], alerts, res)
                        ai, elapsed, steps = set(res["categories"]), res["elapsed"], len(res["steps"])
                    except Exception as e:  # noqa: BLE001
                        ai = {"error"}
                        print(f"  AI 调用出错：{e}")
                else:
                    ai = {"normal"}

            recover(sc)
            time.sleep(2)
            # 恢复后验证巡检：确认系统恢复正常（持续监控本来就会做这一步）
            _, after = inspect_and_alert(t, cfg)
            if after:
                print(f"  ⚠ 恢复后仍有告警：{after[0]['message']}，尝试全部恢复")
                recover_all()
                time.sleep(2)
                inspect_and_alert(t, cfg)

            row = {"scenario": sc, "group": info["group"], "round": r + 1,
                   "alerts": len(alerts), "expected": fmt(exp),
                   "rule_simple": fmt(simple), "rule_simple_ok": simple == exp,
                   "rule_expert": fmt(expert), "rule_expert_ok": expert == exp,
                   "ai": fmt(ai) if ai else "-", "ai_ok": ai == exp,
                   "ai_steps": steps, "ai_seconds": elapsed}
            rows.append(row)

            def mark(okv):
                return "✓" if okv else "✗"
            line = (f"  第{r + 1}轮 告警{len(alerts)}条 | 简单规则 {mark(row['rule_simple_ok'])} {row['rule_simple']:28s}"
                    f"| 专家规则 {mark(row['rule_expert_ok'])} {row['rule_expert']:28s}")
            if use_ai:
                line += f"| AI {mark(row['ai_ok'])} {row['ai']}（{steps}步 {elapsed}s）"
            print(line)

    recover_all()
    if not rows:
        print("没有产生任何结果")
        return
    with open(RESULT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print_summary(rows, use_ai)
    print(f"\n日志过滤（log_filter）：{'开' if cfg['llm'].get('log_filter', True) else '关'}")
    print(f"详细结果已保存到 {RESULT_CSV}")


if __name__ == "__main__":
    main()
