"""公共工具：读取配置文件"""
import os

import yaml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_config(path=None):
    path = path or os.path.join(BASE_DIR, "config.yaml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)
