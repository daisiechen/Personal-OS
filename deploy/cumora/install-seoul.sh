#!/usr/bin/env bash
# 旧服务器：Cumora 群聊服务一次性安装（可重复跑、出错即停）。洛杉矶服务器会话执行：
#   scp -r deploy/cumora my-other-server:/tmp/
#   ssh my-other-server sudo bash /tmp/cumora/install-seoul.sh
# 避让旧服务器已有的东西（实查）：443 是翻墙服务 → 入口用 8443；已有 Postgres 17（5433）→ 只建一个库；
# 系统 Node 20 有别的程序在用 → Node 22 用 nvm 只装给 cumora 用户；已有 Caddyfile → 只追加一行 import。
# 不启动 cumora 服务：等 Mia 本人把 GitHub 登录钥匙填进 /home/cumora/cumora.env。
set -euo pipefail
COMMIT=fd3d6c58cb426dcd8371426e8f92526dcf56136e
ORIGIN=https://team.example.com:8443
PGPORT=5433
ENV=/home/cumora/cumora.env
say() { echo "== $*"; }

say "1 端口检查（不碰 443）"
if ss -ltn | awk '{print $4}' | grep -qE ':8443$'; then echo "8443 已被占用，停下"; exit 1; fi
ss -ltn | awk '{print $4}' | grep -qE '(^|:)80$' && echo "注意：80 已被占用，Caddy 申请证书可能失败" || true

say "2 Postgres（复用 $PGPORT 上已有的那套）"
sudo -u postgres psql -p "$PGPORT" -tAc 'select version()' | head -1
id cumora >/dev/null 2>&1 || useradd -m -s /bin/bash cumora
if [ ! -f "$ENV" ]; then
  PW=$(openssl rand -hex 24)
  if sudo -u postgres psql -p "$PGPORT" -tAc "select 1 from pg_roles where rolname='cumora'" | grep -q 1; then
    sudo -u postgres psql -p "$PGPORT" -qc "ALTER ROLE cumora LOGIN PASSWORD '$PW'"
  else
    sudo -u postgres psql -p "$PGPORT" -qc "CREATE ROLE cumora LOGIN PASSWORD '$PW'"
  fi
  sudo -u postgres psql -p "$PGPORT" -tAc "select 1 from pg_database where datname='cumora'" | grep -q 1 \
    || sudo -u postgres psql -p "$PGPORT" -qc "CREATE DATABASE cumora OWNER cumora"
  sudo -u postgres psql -p "$PGPORT" -d cumora -qc "CREATE EXTENSION IF NOT EXISTS pg_trgm"
  REDIS_URL=redis://127.0.0.1:6379/3
  cat > "$ENV" <<EOF
NODE_ENV=production
PORT=5181
DATABASE_URL=postgres://cumora:$PW@127.0.0.1:$PGPORT/cumora
REDIS_URL=$REDIS_URL
OPENAI_API_KEY=sk-not-used
IDLE_INTERVAL_MS=0
ROUTING_ONE_OF_US=true
CUMORA_PUBLIC_ORIGIN=$ORIGIN
CUMORA_AUTH_DONE_URL=$ORIGIN/
CUMORA_AUTH_RETURN_ALLOWLIST=$ORIGIN
GITHUB_CLIENT_ID=
GITHUB_CLIENT_SECRET=
CUMORA_ADMIN_EMAILS=
EOF
  chown cumora:cumora "$ENV"; chmod 600 "$ENV"
fi
# 生产环境必须有自己的 agent 运行时密钥，否则 Cumora 拒绝启动；只在服务器上生成，不经人手
grep -q '^AGENT_RUNTIME_SECRET=.\+' "$ENV" || echo "AGENT_RUNTIME_SECRET=$(openssl rand -hex 32)" >> "$ENV"
# 早先版本留下的 <占位符> 换成空值（shell 读不了尖括号）
sed -i -E 's/^(GITHUB_CLIENT_ID|GITHUB_CLIENT_SECRET|CUMORA_ADMIN_EMAILS)=<.*>$/\1=/' "$ENV"

say "3 Redis（有就复用 db 3，没有就装并限 128M、只听本机）"
command -v redis-server >/dev/null || { apt-get update -q; apt-get install -y -q redis-server || true; }
# Redis 6 不认 “-::1” 这种写法（7 才有），只听 127.0.0.1
sed -i 's/^#\? *maxmemory .*/maxmemory 128mb/; s/^bind .*/bind 127.0.0.1/' /etc/redis/redis.conf
grep -q '^maxmemory ' /etc/redis/redis.conf || echo 'maxmemory 128mb' >> /etc/redis/redis.conf
systemctl restart redis-server || { journalctl -u redis-server -n 20 --no-pager; exit 1; }
redis-cli -n 3 ping
# Cumora 要 Redis ≥6.2（GETDEL），Ubuntu 22.04 自带 6.0：升到官方源的 7
bash "$(dirname "$0")/redis7.sh"

say "4 Node 22（nvm，只给 cumora 用户）"
sudo -iu cumora bash -s <<'EOS'
set -euo pipefail
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] || curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
. "$NVM_DIR/nvm.sh"
nvm install 22 >/dev/null
nvm alias default 22 >/dev/null
node -v
EOS
NODEBIN=$(sudo -iu cumora bash -c '. ~/.nvm/nvm.sh && dirname "$(nvm which 22)"')

say "5 代码、建表、前端"
sudo -iu cumora bash -s <<EOS
set -euo pipefail
export PATH="$NODEBIN:\$PATH"
[ -d ~/cumora/.git ] || git clone -q https://github.com/yetone/cumora.git ~/cumora
cd ~/cumora && git fetch -q origin && git checkout -q $COMMIT
npm ci --ignore-scripts --no-audit --no-fund
export \$(grep -E '^(DATABASE_URL|REDIS_URL|OPENAI_API_KEY)=' ~/cumora.env | xargs)
npm run migrate
VITE_CUMORA_API_BASE=$ORIGIN npm run build
EOS

say "6 systemd（不启动）"
cat > /etc/systemd/system/cumora.service <<EOF
[Unit]
Description=Cumora (Agent-Box) team chat server
After=network-online.target
Wants=network-online.target

[Service]
User=cumora
WorkingDirectory=/home/cumora/cumora
EnvironmentFile=$ENV
Environment=PATH=$NODEBIN:/usr/local/bin:/usr/bin:/bin
ExecStart=$NODEBIN/node /home/cumora/cumora/node_modules/tsx/dist/cli.mjs server/src/index.ts
Restart=on-failure
RestartSec=5
MemoryMax=900M
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload

say "7 Caddy（8443，已有 Caddyfile 只追加 import）"
if ! command -v caddy >/dev/null; then
  apt-get install -y -q debian-keyring debian-archive-keyring apt-transport-https curl gnupg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q && apt-get install -y -q caddy
fi
mkdir -p /etc/caddy
cat > /etc/caddy/cumora.caddy <<'EOF'
team.example.com:8443 {
	encode gzip
	reverse_proxy 127.0.0.1:5181
}
EOF
touch /etc/caddy/Caddyfile
grep -qxF 'import /etc/caddy/cumora.caddy' /etc/caddy/Caddyfile || echo 'import /etc/caddy/cumora.caddy' >> /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl enable --now caddy
systemctl reload caddy

say "8 结果"
free -m
systemctl is-active caddy
echo "旧服务器装好了（cumora 服务没启动）。下一步：Mia 本人在 $ENV 里填 GITHUB_CLIENT_ID、GITHUB_CLIENT_SECRET、CUMORA_ADMIN_EMAILS 三行等号后面，然后 systemctl enable --now cumora"
