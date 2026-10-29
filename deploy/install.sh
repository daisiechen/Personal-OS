#!/usr/bin/env bash
# 在服务器上以 root 或 sudo 运行。只创建 ethan 用户和 /home/ethan 下的内容，不触碰旧版目录与服务。
set -euo pipefail

id ethan >/dev/null 2>&1 || sudo useradd --create-home --shell /bin/bash ethan
sudo -u ethan mkdir -p /home/ethan/data /home/ethan/.config/ethan
sudo chmod 700 /home/ethan/.config/ethan

if [ ! -d /home/ethan/Personal-OS ]; then
  echo "请先以 ethan 用户 clone 仓库到 /home/ethan/Personal-OS" >&2
  exit 1
fi
if [ ! -f /home/ethan/.config/ethan/ethan.env ]; then
  echo "缺少密钥。请让 Mia 运行 python3 -m ethan.setup_secrets 录入后再运行本脚本" >&2
  exit 1
fi
sudo -H -u ethan git config --global user.name >/dev/null || sudo -H -u ethan git config --global user.name "小伴"
sudo -H -u ethan git config --global user.email >/dev/null || sudo -H -u ethan git config --global user.email "ethan@localhost"

sudo cp /home/ethan/Personal-OS/deploy/systemd/ethan2-*.service /home/ethan/Personal-OS/deploy/systemd/ethan2-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ethan2-telegram.service ethan2-weixin.service ethan2-inbox.service ethan2-workbench.service ethan2-backup.timer ethan2-vault.timer ethan2-weekly.timer ethan2-proactive.timer ethan2-planner.timer ethan2-autodeploy.timer ethan2-codex.timer ethan2-brain.timer ethan2-english.timer ethan2-prep.timer ethan2-memory.timer ethan2-money.timer
systemctl --no-pager status ethan2-telegram.service | head -5
