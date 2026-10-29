#!/bin/sh
# 固定的 Claude 会话里和 小伴 聊（ethan/chat.py）：读服务的环境（不打印），她的话从标准输入进来
set -a
. /home/ethan/.config/ethan/ethan.env
set +a
export PATH=/home/ethan/.local/bin:/usr/local/bin:/usr/bin:/bin
cd /home/ethan/Personal-OS && exec /usr/bin/python3 -m ethan.chat --data /home/ethan/data
