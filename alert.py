"""告警层：固定阈值 + 基于历史基线的动态阈值

为什么时延要用动态阈值？
  不同链路的"正常时延"差别很大：本机 0.05ms，跨省 30ms。
  固定一个 100ms 的阈值，对本机来说太松（从 0.05ms 涨到 80ms 明显异常却不告警），
  对远程链路又可能太紧。所以拿"这条链路自己的历史表现"作为参照：
      阈值 = 历史均值 + max(sigma × 标准差, 最小增量)
  其中"最小增量"用来防止历史数据太稳定（标准差接近 0）时，微小抖动也触发告警。
"""
import statistics

from db import recent_normal_values

LATENCY_METRICS = ("ping_latency_ms", "http_ms")


def dynamic_threshold(target, metric, cfg):
    a = cfg["alert"]
    hist = recent_normal_values(target, metric, a["history_window"])
    if len(hist) < a["min_history"]:
        return a["static_latency_ms"], f"历史数据仅{len(hist)}条，使用固定阈值"
    mean = statistics.mean(hist)
    std = statistics.pstdev(hist)
    thr = mean + max(a["sigma"] * std, a["latency_floor_ms"])
    return thr, f"历史均值{mean:.2f}ms，标准差{std:.2f}ms"


def check_alerts(target, metrics, cfg):
    """根据本次巡检结果生成告警列表（必须在保存本次数据【之前】调用）"""
    a = cfg["alert"]
    alerts = []

    def add(metric, value, threshold, message):
        alerts.append({"metric": metric, "value": value,
                       "threshold": threshold, "message": message})

    for name, (value, detail) in metrics.items():
        if value is None:
            continue
        if name in LATENCY_METRICS:
            thr, why = dynamic_threshold(target, name, cfg)
            if value > thr:
                add(name, value, thr,
                    f"{name} = {value:.1f}ms，超过动态阈值 {thr:.1f}ms（{why}）")
        elif name == "ping_loss" and value >= a["loss_threshold"]:
            add(name, value, a["loss_threshold"], f"ping 丢包率 {value:.0f}%")
        elif name.startswith("port_") and value == 0:
            add(name, value, 1, f"端口 {name[5:]} 连接失败（{detail}）")
        elif name == "http_ok" and value == 0:
            add(name, value, 1, f"网页访问失败（{detail}）")
        elif name.startswith("disk:") and value >= a["disk_threshold"]:
            add(name, value, a["disk_threshold"],
                f"磁盘 {name[5:]} 使用率 {value:.1f}%，超过 {a['disk_threshold']}%")
    return alerts
