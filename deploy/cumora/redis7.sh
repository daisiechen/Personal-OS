#!/usr/bin/env bash
# 旧服务器：Redis 升到官方源的新版（11-04 装上的是 8）（Cumora 登录要用 GETDEL，6.2 才有；Ubuntu 22.04 自带 6.0）。可重复跑。
#   ssh my-other-server sudo bash /tmp/cumora/redis7.sh
set -euo pipefail
ver() { redis-server --version | sed -E 's/.*v=([0-9]+)\.([0-9]+).*/\1\2/'; }
if [ "$(ver)" -lt 62 ]; then
  apt-get install -y -q curl gnupg lsb-release
  curl -fsSL https://packages.redis.io/gpg | gpg --dearmor --yes -o /usr/share/keyrings/redis-archive-keyring.gpg
  echo "deb [signed-by=/usr/share/keyrings/redis-archive-keyring.gpg] https://packages.redis.io/deb $(lsb_release -cs) main" > /etc/apt/sources.list.d/redis.list
  apt-get update -q
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q -o Dpkg::Options::=--force-confold redis-server redis-tools
fi
# 官方包的 systemd 要 daemonize no + supervised systemd（沿用 Ubuntu 旧配置会起不来）
sed -i 's/^#\? *maxmemory .*/maxmemory 128mb/; s/^bind .*/bind 127.0.0.1/; s/^daemonize .*/daemonize no/; s/^#\? *supervised .*/supervised systemd/' /etc/redis/redis.conf
grep -q '^maxmemory ' /etc/redis/redis.conf || echo 'maxmemory 128mb' >> /etc/redis/redis.conf
systemctl restart redis-server || { journalctl -u redis-server -n 20 --no-pager; exit 1; }
redis-server --version
redis-cli -n 3 ping
if systemctl is-enabled cumora >/dev/null 2>&1; then
  systemctl restart cumora
  sleep 15
  curl -s localhost:5181/api/health; echo
fi
