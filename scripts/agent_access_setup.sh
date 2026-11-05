#!/usr/bin/env bash
# 一次性设置（Mia 在 Mac 终端跑一次）：让洛杉矶的服务器会话以后自己干活，不再一遍遍找她。
#   bash scripts/agent_access_setup.sh            # 只设洛杉矶和旧服务器
#   bash scripts/agent_access_setup.sh <北京别名>  # 北京也设（Mac 上 ssh 北京用的名字）
# 做三件事：
#   1. 洛杉矶 ethan 用户生成一把自己的钥匙（已有就不动）；
#   2. 把这把钥匙加到旧服务器（和北京）的 authorized_keys，并在洛杉矶写好 my-other-server / beijing 两个连接名；
#   3. 洛杉矶给 ethan 开一条免密 sudo：只允许装 bubblewrap 和 socat（Cumora 连接程序要用），别的照旧要密码。
# 要输 sudo 密码时她本人输；脚本不打印任何密钥。
set -euo pipefail
LA=my-server
SEOUL=my-other-server
BJ="${1:-}"

echo "== 1/3 洛杉矶 ethan 的钥匙"
ssh -t "$LA" 'sudo -u ethan bash -c "mkdir -p ~/.ssh && chmod 700 ~/.ssh && { test -f ~/.ssh/id_ed25519 || ssh-keygen -q -t ed25519 -N \"\" -C ethan@my-server -f ~/.ssh/id_ed25519; }"'
PUB=$(ssh "$LA" 'sudo cat /home/ethan/.ssh/id_ed25519.pub')

add_key() {  # $1 = Mac 上的连接名，$2 = 洛杉矶上要用的连接名
  local host user ip
  ip=$(ssh -G "$1" | awk '/^hostname /{print $2}')
  user=$(ssh -G "$1" | awk '/^user /{print $2}')
  ssh "$1" "mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && grep -qxF '$PUB' ~/.ssh/authorized_keys || echo '$PUB' >> ~/.ssh/authorized_keys"
  ssh "$LA" "sudo -u ethan bash -c 'touch ~/.ssh/config && chmod 600 ~/.ssh/config && grep -q \"^Host $2\$\" ~/.ssh/config || printf \"\nHost $2\n  HostName $ip\n  User $user\n  StrictHostKeyChecking accept-new\n\" >> ~/.ssh/config'"
  ssh "$LA" "sudo -u ethan ssh -o BatchMode=yes -o ConnectTimeout=8 $2 hostname" && echo "   洛杉矶 → $2 通了"
}

echo "== 2/3 洛杉矶 → 旧服务器"
add_key "$SEOUL" my-other-server
if [ -n "$BJ" ]; then
  echo "== 2/3 洛杉矶 → 北京"
  add_key "$BJ" beijing
else
  echo "   （没给北京别名，跳过北京）"
fi

echo "== 3/3 洛杉矶免密 sudo（只限装 bubblewrap socat）"
ssh -t "$LA" 'echo "ethan ALL=(root) NOPASSWD: /usr/bin/apt-get install -y bubblewrap socat" | sudo tee /etc/sudoers.d/ethan-agent >/dev/null && sudo chmod 440 /etc/sudoers.d/ethan-agent && sudo visudo -cf /etc/sudoers.d/ethan-agent'
echo "全部设好。回 Claude 会话说一声“设好了”。"
