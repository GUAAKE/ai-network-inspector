#!/usr/bin/env bash
# 一键准备实验环境（在 WSL Ubuntu / Ubuntu 虚拟机里运行）：bash setup.sh
set -e

echo "== 1/4 安装系统软件 =="
sudo apt update
sudo apt install -y nginx iproute2 iptables iputils-ping python3-venv python3-pip

echo "== 2/4 启动 nginx（被监控的服务）=="
# 部分环境不支持 IPv6，nginx 默认的 "listen [::]:80" 会导致启动失败，这里自动注释掉
if ! sudo nginx -t >/dev/null 2>&1; then
  sudo sed -i --follow-symlinks 's/^\(\s*listen \[::\]:80.*\)$/# \1/' /etc/nginx/sites-enabled/default
fi
sudo service nginx start

echo "== 3/4 创建 100MB 测试盘，用于模拟磁盘写满 =="
sudo mkdir -p /mnt/inspect_disk
mountpoint -q /mnt/inspect_disk || sudo mount -t tmpfs -o size=100M tmpfs /mnt/inspect_disk

echo "== 4/4 创建 Python 虚拟环境并安装依赖 =="
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

echo
echo "完成！接下来："
echo "  ./venv/bin/python main.py --once      # 巡检一次试试"
echo "注意：WSL 重启后 nginx 和测试盘会失效，重新运行 bash setup.sh 即可（很快）"
