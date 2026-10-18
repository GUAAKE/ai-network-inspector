"""告警推送：企业微信 / 钉钉 / 飞书 群机器人

在群设置里添加"自定义机器人"，复制 Webhook 地址填到 config.yaml 即可。
钉钉机器人如果设置了"自定义关键词"，关键词填"巡检"即可（消息里都带这两个字）。
"""
import requests


def push(text, cfg):
    url = cfg["alert"].get("webhook_url")
    if not url:
        return False
    kind = cfg["alert"].get("webhook_type", "wecom")
    if kind == "feishu":
        payload = {"msg_type": "text", "content": {"text": text}}
    else:  # 企业微信和钉钉的文本消息格式相同
        payload = {"msgtype": "text", "text": {"content": text}}
    try:
        r = requests.post(url, json=payload, timeout=5)
        return r.ok
    except requests.RequestException as e:
        print(f"   [推送失败] {e}")
        return False
