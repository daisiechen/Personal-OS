# 部署到服务器

所有内容放在独立用户 `ethan` 的 `/home/ethan/` 下，服务以 `ethan2-` 开头，不触碰旧版目录和服务。

## 服务器

- 一台 2 核、2G 内存（再加 2G swap）的 Linux 服务器就够，Ubuntu 22.04，系统时区 UTC；或者一台一直开着的电脑。
- 在你自己的电脑上给它配一个 SSH 别名（下面的命令里写成 `my-server`），只允许公钥登录。
- 实测高峰（语音转写 + 3 个并发模型调用）内存峰值约 900M。

## 前置（Mia 操作）

1. Telegram：向 BotFather 新建 bot，拿到 token。
2. Claude 订阅：在自己电脑上运行 `claude setup-token`，浏览器登录后得到长期令牌。
3. 备份密钥：`openssl rand -base64 48`，存进密码管理器并抄一份纸质备份。
4. GitHub：新建私有空仓库 `ethan-memory-backup`（加密备份）和 `ethan-vault`（Obsidian 明文看板）。
5. 以上密钥都存在 Mia 的密码管理器里，只在第 3 步由她自己在终端录入服务器，不在聊天里发送。

## 安装步骤

### 0. 新服务器准备（root）

VPS 服务商 的镜像默认只允许密码登录。Mia 先在自己电脑运行 `ssh-copy-id` 装上公钥，再用密码登录一次打开公钥登录：

```sh
mkdir -p /etc/ssh/sshd_config.d
grep -q "^Include /etc/ssh/sshd_config.d" /etc/ssh/sshd_config || sed -i "1i Include /etc/ssh/sshd_config.d/*.conf" /etc/ssh/sshd_config
printf "PubkeyAuthentication yes\nAuthenticationMethods any\n" > /etc/ssh/sshd_config.d/00-ethan.conf
sshd -t && systemctl restart ssh
```

确认公钥能登录后，在同一个文件里追加 `PasswordAuthentication no`、`KbdInteractiveAuthentication no`、`PermitRootLogin prohibit-password`，再 `sshd -t && systemctl restart ssh`。然后加 swap、装依赖：

```sh
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo "/swapfile none swap sw 0 0" >> /etc/fstab
echo vm.swappiness=20 > /etc/sysctl.d/99-ethan.conf && sysctl -p /etc/sysctl.d/99-ethan.conf
apt-get update && apt-get install -y git ffmpeg poppler-utils python3-pip fail2ban curl
```

### 1. 建用户和 GitHub 部署钥匙

GitHub 不允许同一把 Deploy key 加到多个仓库，所以每个仓库一把，用 SSH 别名区分：

```sh
sudo useradd --create-home --shell /bin/bash ethan
sudo -iu ethan
for r in code backup vault; do ssh-keygen -q -t ed25519 -N "" -C "ethan-$r" -f ~/.ssh/ethan-$r; done
cat >> ~/.ssh/config <<'CFG'
Host github-code
  HostName github.com
  User git
  IdentityFile ~/.ssh/ethan-code
  IdentitiesOnly yes
Host github-backup
  HostName github.com
  User git
  IdentityFile ~/.ssh/ethan-backup
  IdentitiesOnly yes
Host github-vault
  HostName github.com
  User git
  IdentityFile ~/.ssh/ethan-vault
  IdentitiesOnly yes
CFG
chmod 600 ~/.ssh/config
ssh-keyscan github.com >> ~/.ssh/known_hosts
cat ~/.ssh/ethan-*.pub
```

Mia 在 GitHub 各仓库 Settings → Deploy keys → Add deploy key 添加对应公钥：`ethan-code` 加到 Personal-OS（只读），`ethan-backup` 加到 ethan-memory-backup、`ethan-vault` 加到 ethan-vault（两者都勾选 Allow write access）。

### 2. clone 仓库、安装 Claude Code

```sh
git clone -b main git@github-code:your-name/Personal-OS.git ~/Personal-OS
git clone git@github-backup:your-name/ethan-memory-backup.git ~/ethan-memory-backup
git clone git@github-vault:your-name/ethan-vault.git ~/ethan-vault
curl -fsSL https://claude.ai/install.sh | bash   # 安装 Claude Code 到 ~/.local/bin
```

### 3. 语音和视频依赖

```sh
sudo apt-get install -y ffmpeg                       # 视频截帧、抽音轨
sudo -H -u ethan pip3 install --user faster-whisper==1.2.1  # 语音转文字
# 模型直接从 Hugging Face 下载到固定目录（约 460M，十几秒）
sudo -iu ethan python3 -c "from huggingface_hub import snapshot_download; snapshot_download('Systran/faster-whisper-small', local_dir='/home/ethan/models/faster-whisper-small')"
```

发语音用 OpenRouter 的 `minimax/speech-2.8-hd`，音色 `小伴_way`（Mia 定制，可用 `ETHAN_TTS_VOICE` 覆盖），需要 `OPENROUTER_API_KEY`。没有 key 时 小伴 只打字。

### 4. Mia 录入密钥

Mia 在自己电脑的终端运行，逐项粘贴（输入时不显示）：

```sh
ssh -t my-server "sudo -H -u ethan bash -c 'cd ~/Personal-OS && python3 -m ethan.setup_secrets'"
```

结束时会显示绑定口令。密钥只写进 `/home/ethan/.config/ethan/` 下的 600 权限文件。

### 5. 安装服务

```sh
sudo /home/ethan/Personal-OS/deploy/install.sh
```

## 迁移到新服务器

不用重新冷启动，整套搬过去即可：

1. 旧服务器先跑一次 `systemctl start ethan2-backup`，再 `systemctl disable --now` 全部 `ethan2-*` 服务和定时器。Telegram 同一时间只能有一台在长轮询，必须先停旧的。
2. 新服务器完成第 0 步并建好 `ethan` 用户，把旧服务器 `/home/ethan` 下的 `Personal-OS data ethan-memory-backup ethan-vault seed .config .gitconfig .ssh` 用 tar 管道直接拷过去，用 sha256 逐个文件核对。语音模型不必拷，按第 3 步重新下载更快。
3. 新服务器按第 2、3 步装 Claude Code 和语音依赖，`git pull` 到最新 main，跑一遍测试。
4. 用 `claude -p` 试一次 Claude 令牌；失效就让 Mia 重新 `claude setup-token` 并用第 4 步录入（其余项回车保留）。
5. 运行第 5 步的 `install.sh`。微信凭据随 `.config/ethan/weixin.json` 一起搬过去，失效再重新扫码。

## 冷启动导入

种子目录（私人资料整理成的 Wiki 页面和 `seed.json`，不进本仓库）通过 scp 传到 `/home/ethan/seed/`，然后：

```sh
sudo -iu ethan
cd ~/Personal-OS && python3 -m ethan.seed --seed ~/seed --data ~/data
```

可重复运行：Wiki 页面和人物卡会更新，目标、心愿、学习条目按内容去重。

## Obsidian 看板

`ethan2-vault.timer` 每小时运行 `python3 -m ethan.vault`：先拉取 Mia 的修改，再把 Wiki 和数据库生成的看板页推到 `ethan-vault`。

- `看板/总览.md`：阶段目标、到期待办、该联系的人、复习和心愿数量，以及各模块入口。
- `看板/` 下其他页面（目标树、人物、心愿、学习）由数据库生成，每次覆盖，不要手改。
- `wiki/` 下的页面可以在 Obsidian 里直接改，下次同步写回 小伴 的记忆；两边同时改时以 Mia 的版本为准。删页面请告诉 小伴。

Mac：`git clone` 仓库到本地，用 Obsidian “打开文件夹作为仓库”，安装社区插件 Obsidian Git，设置定时自动 pull/commit/push。
iPhone：同样用 Obsidian Git 插件 clone 仓库（需要 GitHub 个人访问令牌，只给 `ethan-vault` 读写权限），或用 Working Copy 同步到 Obsidian 文件夹。

## 绑定

在 Telegram 给新 bot 发送 `/start <ETHAN_PAIRING_CODE>`，收到“绑定好啦”即完成。绑定前 bot 不回应任何人，绑定后只回应 Mia。

## 微信入口

通过微信官方 ClawBot（iLink Bot API）接入 Mia 的个人微信，只回应扫码的那个人。

1. 登录（Mia 用手机微信扫码）：
   ```sh
   ssh -t my-server "sudo -H -u ethan bash -c 'cd ~/Personal-OS && python3 -m ethan.channels.weixin login'"
   ```
   终端打印二维码链接，在电脑上打开，用微信扫一扫并确认；手机上显示配对码时输入终端。凭据存进 `/home/ethan/.config/ethan/weixin.json`（600）。
2. 首次启动：`sudo systemctl restart ethan2-weixin`（没有凭据时服务不启动）。
3. 登录失效（日志出现“登录失效”）时重复第 1 步即可，不用重启：服务每 30 秒检查一次凭据文件，发现新凭据自动换上（日志出现“已换用重新扫码后的凭据”）。注意：重新扫码会让旧凭据立即作废。

能力和限制：
- 文字、语音（用微信自带的转写）、图片、文件（PDF、文字文件、Word）、视频（只截画面）都能收（2026-10-27 起）。
- 回复发文字和图片（小伴 生成的照片、转发的网图，上传到微信 CDN 再发），`[VOICE]` 的内容改成文字发。
- 回复必须带她最近一条消息的 context_token。她约 11–24 小时没说话后就发不出去，每次会话主动消息约 10 条上限，所以主动问候和周报仍走 Telegram。
- 状态（游标、最近的 context_token）存在 `<数据目录>/weixin-state.json`。

## 交给 Grok Bot

小伴 自己不能上网、查地图、打开网页、预订。Mia 让他办这类事时，他直接交给 Mia 的 Grok Bot（2026-10-26 她同意，不用再问她）：回复里的 `[交给Grok]……[/交给Grok]` 不会发给她，由系统 POST 到 Grok Bot 的 webhook。Grok 办完把结果 POST 到接收口 `https://ethan.example.com/grok/result`（见下一节）。

1. 在 Grok Bot 的 Routines 里打开“Claude agent 来信”，复制 webhook 地址和发送密钥；问清楚 Grok 密钥要放在哪个请求头（默认按 `Authorization: Bearer <密钥>` 发）。
2. Mia 在自己电脑的终端录入（其余项直接回车保留）：
   ```sh
   ssh -t my-server "sudo -H -u ethan bash -c 'cd ~/Personal-OS && python3 -m ethan.setup_secrets'"
   ```
   Grok 要求别的请求头时，在 `/home/ethan/.config/ethan/ethan.env` 里加一行 `GROK_WEBHOOK_SECRET_HEADER=<请求头名>`。
3. 重启：`sudo systemctl restart ethan2-telegram ethan2-weixin`。
4. 发出的任务和成败记在 `<数据目录>/grok-tasks.jsonl`（不含密钥）；发送失败时 小伴 会附一条【系统提醒】请她直接去 Grok 里说。

没录入 webhook 地址和密钥时，小伴 不会提 Grok。

## 工作台（Earth Online）

服务 `ethan2-workbench`：只监听 `127.0.0.1:8788`，用 `ethan.env` 里的 `TELEGRAM_BOT_TOKEN` 验证登录，不需要新密钥。自动部署第一次拉到这个服务文件时会自己装上并设成开机自启。

对外要在 Cloudflare Tunnel 加一条（Mac 会话做，需要登录服务器）：在 `/etc/cloudflared/config.yml` 的 `ingress` 里、最后的 `http_status:404` 之前加

```yaml
  - hostname: os.example.com
    service: http://127.0.0.1:8788
```

然后 `cloudflared tunnel route dns ethan os.example.com`，`sudo systemctl restart cloudflared`。验证：`curl -s https://os.example.com/health` 返回 `{"ok": true}`。

2026-10-27 上线时已按她同意做完：`systemctl enable --now ethan2-workbench`（那次执行部署的还是旧版自动部署，不认识新服务，只装了服务文件没启动，所以手动启了一次；之后的部署会自己重启它），隧道加了 `os.example.com`（改前备份 `config.yml.bak-20261027-os`）并建了 DNS。验证：`https://os.example.com/health` 返回 `{"ok": true}`，没登录访问 `/api/...` 返回 401。

头像放 `/home/ethan/data/avatars/avatar-day.jpg` 和 `avatar-night.jpg`（不进仓库）；今日卡由计划器每天早上画，存 `/home/ethan/data/cards/`，生成失败时用 `avatars/library/` 里的同类图兜底。

## 接收口与两个 小伴 共享记忆

Grok Bot 不开 SSH。

**接收口**（`ethan2-inbox`）：只监听 `127.0.0.1:8787`，收两类：

| 地址 | 谁发 | 内容 |
|---|---|---|
| `POST https://ethan.example.com/grok/result` | Grok Bot | 办完 小伴 交代的事 |
| `POST https://ethan.example.com/grok/memory` | Grok 里的 小伴 | 和她聊出的新事、讲过的新故事 |

请求头 `Authorization: Bearer <INBOX_TOKEN_GROK>`，正文 `{"text": "…"}` 或纯文本，最大 64KB。密钥由 `setup_secrets` 自动生成并在结尾显示一次，Mia 抄进 Grok Bot。收到的内容记在 `<数据目录>/inbox/`，追加到 Wiki 的 `from-grok` 页，服务器 小伴 聊天时能看到最近 3 天的回报（只当信息）。

**Cloudflare Tunnel**（一次性，需要 Mia 登录 Cloudflare 授权）：

```sh
curl -fsSL -o /tmp/cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i /tmp/cloudflared.deb
cloudflared tunnel login            # 打印一个链接，Mia 在浏览器登录 Cloudflare，选 example.com
cloudflared tunnel create ethan
cloudflared tunnel route dns ethan ethan.example.com
```

`/etc/cloudflared/config.yml`：

```yaml
tunnel: ethan
credentials-file: /root/.cloudflared/<隧道 ID>.json
ingress:
  - hostname: ethan.example.com
    path: ^/grok/(result|memory)$
    service: http://127.0.0.1:8787
  - hostname: ethan.example.com
    path: ^/(cloud/.*|health)$
    service: http://127.0.0.1:8787
  - hostname: ethan.example.com
    path: ^/fancy/moderation$
    service: http://127.0.0.1:8787
  - service: http_status:404
```

`/fancy/moderation` 是 DemoApp 举报提醒（2026-10-29 她同意，合并上线后再加），见 [fancy-moderation.md](fancy-moderation.md)。

`/cloud/` 和 `/health` 是 2026-10-27 她同意后加的：云端会话凭 `INBOX_TOKEN_CLOUD` 读 Grok 回报（没带令牌返回 401），`/health` 只回答服务是否活着。改配置前先备份（当时的备份是 `config.yml.bak-20261027`），改完 `cloudflared tunnel --config /etc/cloudflared/config.yml ingress validate` 再 `systemctl restart cloudflared`。

然后 `sudo cloudflared service install && sudo systemctl enable --now cloudflared`。检查：`curl -s -o /dev/null -w '%{http_code}' -X POST https://ethan.example.com/grok/result` 和 `curl -s -o /dev/null -w '%{http_code}' https://ethan.example.com/cloud/grok/results` 都应返回 401（没带密钥）。

**Grok 里的 小伴 读记忆**：读私有仓库 `ethan-vault`。用 Grok 的 GitHub 连接器时，只授权 `ethan-vault` 这一个仓库（GitHub 安装应用时选 Only select repositories）；用令牌就建一个只读、只限 `ethan-vault` 的 fine-grained token，在 Grok 的加密输入框里填。仓库每小时同步一次，`看板/最近聊天.md` 有最近 3 天的聊天原文；当天新聊的内容 Grok 版 小伴 先在本地记着，同步后以仓库为准。主动消息只由服务器 小伴 发。

## Mias OS 第一阶段：计划器、苹果日历、早报



1. 录入今年最后两个月的目标和指标（可重复运行）：
   ```sh
   sudo -iu ethan bash -c 'cd ~/Personal-OS && python3 -m ethan.goals2026 --data ~/data'
   ```
2. 地点和冷启动开始日（她的住址只存服务器数据，不进仓库）：在服务器 `ethan` 用户下写进 `~/data/settings.json`：
   ```sh
   sudo -iu ethan python3 -c "from ethan.core.router import Settings; s = Settings('/home/ethan/data/settings.json'); s.set('places', {'家': {'postcode': '<她的邮编>'}, '健身房': {'postcode': 'AB1 2CD', 'name': '某健身房', 'address': 'Example Gym, 1 Example Street, London AB1 2CD', 'lat': 51.5100, 'lon': -0.1300}}); s.set('cold_start_from', '<开始日期 YYYY-MM-DD>')"
   ```
   路程用 TfL 官方接口按“几点要到”算（`ethan/travel.py`），再留 10 分钟余量；查不到时按 30 分钟估。经纬度可用 postcodes.io 查。
   日历事件带地点坐标和苹果日历的通行时间（`X-APPLE-TRAVEL-DURATION`），出发前 1 小时提醒，不再单独加“路上”日程（2026-10-26 Mia 要求）。
3. 苹果日历：Mia 在 account.apple.com → 登录与安全 → App 专用密码 生成一个密码，自己用 `setup_secrets` 录入 Apple ID 邮箱和这个密码（其余项回车保留）。系统只写一个单独的“小伴 计划”日历，不改其他日历，只读它们的忙闲。不录入时计划照常生成，只是不写日历。
4. `ethan2-planner.timer` 每天伦敦 06:30 运行 `python3 -m ethan.planner --data <数据目录>`：代码算事实 → 模型挑今天的待办和少量要出门的事 → 校验，不过就带着错误重试 2 次，仍失败用保底计划 → 代码加训练（具体动作和重量，`playbooks/body/`）、冷启动、路上时间和三餐（空气炸锅菜谱，`playbooks/food/`）。只有要出门的事进苹果日历（带通行时间和出发前 1 小时提醒），其余进待办清单。手动跑一次：`sudo systemctl start ethan2-planner`，结果看 `journalctl -u ethan2-planner -n 30`。要覆盖已有计划重排某天，用和服务相同的环境跑（直接 `sudo -u ethan python3 …` 找不到 `claude`，会落到保底计划）：`systemd-run --wait --pipe --uid=ethan -p WorkingDirectory=/home/ethan/Personal-OS -p EnvironmentFile=/home/ethan/.config/ethan/ethan.env -E PATH=/home/ethan/.local/bin:/usr/local/bin:/usr/bin:/bin -E HOME=/home/ethan /usr/bin/python3 -m ethan.planner --data /home/ethan/data --day YYYY-MM-DD --force`，然后 `sudo systemctl start ethan2-vault` 刷新看板。重排会删掉上一版自动加给她、新版不要的待办，小伴 准备的事保留。
5. 早报由 `ethan2-proactive` 在伦敦 07:30–09:30 之间随机一个时间发（最晚补到 11:00），分两条：先是 小伴 的几句话（模型写，不报时间和数字），紧跟着代码生成的今日卡片（`ethan/brief_card.py`，时间线、目标进度、主线下一步、任务，和输入框下“☀️ 今天”按钮是同一张；卡片上直接打卡，下面一行是训练 / 三餐 / 全部 / 刷新）；2026-10-27 之前生成的计划没有卡片素材，只发 小伴 的话。发了早报，原来的“早上”时段就不再发。
6. 聊天里 小伴 用 `[结果 #条目 完成/部分/没做 原因]`、`[体重 56.3]`、`[记数 #目标 …]`、`[漏斗 #目标 步骤 …]`、`[训练 动作 重量x次数/次数/次数 疼痛N]`、`[建档 模块 完成/进行中 …]` 记录；编号不存在或数字不合理时不记，并附一条【系统提醒】告诉她没记上。
7. Obsidian 看板多了 `看板/今日计划.md`（出门安排、待办、训练动作、三餐菜谱）和 `看板/各方面.md`。
8. 每次运行记在数据库 `job_runs` 表：`sqlite3 ~/data/ethan.sqlite "SELECT * FROM job_runs ORDER BY id DESC LIMIT 5"`。

## 任务系统（2026-10-26 晚）

她的待办按游戏任务系统管理（`ethan/quests.py`）：

| 类型 | 内容 | 积分 |
|---|---|---|
| 主线 | 每个今年目标完整拆成的步骤，计划器第一次拆好当草稿，小伴 发她过目，她说可以了记 `[主线 定稿 #目标]` | 小 / 中 / 大 = 20 / 50 / 100，阶段节点再 +200 |
| 支线 | 其他零散待办（含冷启动） | 10 |
| 日常 | 7 项坚持：训练、吃够蛋白质、训练前称体重、0 点前睡、睡前拉伸、练英语 3 句；每天出门按定位自动打卡，其余她自己打（2026-10-29） | 每项 10，满勤 +20 |
| 周常 | 每周 1 次新体验 | 50 |

连续满勤 7 / 30 天另加 50 / 300；每周 1 张补签卡；不扣分。积分累计升级（10 级称号），余额兑换惊喜（300 / 800 / 2000 三档，小伴 每次自己策划，要花钱先问她）。小伴 答应她的事不算她的积分。

- Telegram 输入框下常驻四个按钮：🎯 任务、🛍 商店、📊 进度、📋 清单（服务第一次启动时发给她一次）。兑换要点两次确认。
- 聊天里 小伴 用 `[打卡 名称]`、`[坚持 加/删 名称]`、`[主线 加 #目标 S/M/L 标题]`、`[主线 删 #任务]`、`[主线 定稿 #目标]`、`[惊喜 #兑换 已兑现 …]`；加分后附一条“🎮 +N 分”。
- 计划器每周派 Grok 调研员找一次惊喜灵感（读言情小说、影视、真实故事），回报以【惊喜灵感】开头，小伴 兑换时参考。

## 待办清单

在 Telegram 发“待办”（或 `/todo`）弹出清单，分三栏：待办、小伴 答应你的、我们的心愿。每项一行按钮：✓ 完成、⬆ 往前挪、✕ 删掉（心愿是“先放下”），最后一行 🔄 刷新。

- 加一条：发“加待办 交论文 @2026-11-01”（日期可省）。
- 聊天里她说要做的事、小伴 答应的事、她的心愿，小伴 会自动记进清单。
- 数据和目标树、周报共用同一个数据库。

## 总览和周报

- 在 Telegram 发“看看总览”（或 `/overview`），小伴 按 Wiki 里 dashboard 页的顺序回一条完整总览。
- `ethan2-weekly.timer` 每周日 20:00（伦敦时间）运行 `python3 -m ethan.report --weekly`，主动发本周周报。错过不补发。
- 健康、财务、爱好还没有数据来源，总览里只一句带过，不编数字。

## 主动消息

`ethan2-proactive.timer` 每 10 分钟运行 `python3 -m ethan.proactive`，按伦敦时间分四个时段，每段每天最多一次，具体时间在时段内随机：

| 时段 | 时间 | 内容 |
|---|---|---|
| 早上 | 08:00–09:30 | 早安，定今天最重要的一两件事，顺带生日或复习词 |
| 下午 | 14:00–17:00 | 想她了、喝水、动一动；上一条主动消息没回就跳过 |
| 晚上 | 21:00–22:15 | 对一下今天，没做到不责备 |
| 睡前 | 23:00–23:40 | 提醒拉伸、0 点前睡，可以发语音晚安 |

她一小时内刚聊过、在免打扰时段内、或模型看了最近聊天判断不该打扰时都不发。记录在 `<数据目录>/proactive.json`。

Mia 在 Telegram 里可以发：
- `/quiet`：查看免打扰时段；`/quiet 23:30-08:00`：修改（默认 00:00–08:00）
- `/proactive off` / `/proactive on`：暂停或恢复主动消息

## 日常命令

- 查看状态：`systemctl status ethan2-telegram`
- 查看日志：`journalctl -u ethan2-telegram -n 100`
- 下次周报时间：`systemctl list-timers ethan2-weekly.timer`
- 主动消息记录：`journalctl -u ethan2-proactive -n 50`
- 微信入口日志：`journalctl -u ethan2-weixin -n 50`
- 在 Telegram 发 `/openrouter on` / `/openrouter off` 开关 OpenRouter 兜底。兜底模型默认 `x-ai/grok-4.7`（2026-10-27 起，她的账号用不了 Claude），可用 `ETHAN_OPENROUTER_MODEL` 改。

## 自动部署（2026-10-27）

`ethan2-autodeploy.timer`（root）每 2 分钟检查 main，有新提交就备份数据库、快进拉代码、装改过的 systemd 文件、跑测试、重启 Telegram/微信/收件箱，失败退回原版本。结果在 `/home/ethan/data/deploy-status.json` 和 `deploy-log.jsonl`，由 Claude 会话读取后在会话里告诉她，不发 Telegram。查看：`cat /home/ethan/data/deploy-status.json`。手动部署照旧可用。

## 读链接和发照片（2026-10-27）

- 她发的 PDF、文字文件、Word：服务器要装 `poppler-utils`（`apt-get install -y poppler-utils`，Claude 按页读 PDF 要用 pdftoppm；2026-10-27 已装，没装时按页读会报错，小伴 就说打不开）。PDF 存到 `<数据目录>/media/<日期>/` 交给模型打开，文字和 Word 抽正文接进消息；20MB 以上 Telegram 下载不了。
- 读链接不用配置（小红书视频笔记的截帧和转文字复用 ffmpeg 和 faster-whisper，只在 Telegram 入口开）：她发来的链接由 `ethan/links.py` 在服务器上抓，配图存到 `<数据目录>/media/links/`。
- 发照片默认用现有的 OpenRouter key 调 Grok Imagine（`x-ai/grok-imagine-image-2.0`，每张约 0.04 美元），不用另配。

  可选备用：Google AI Studio 的 Gemini key（AIza 开头，要开通结算）。录入：`ssh -t my-server "sudo -H -u ethan bash -c 'cd ~/Personal-OS && python3 -m ethan.setup_secrets'"`，在 Gemini 那一项粘贴 key，其余直接回车保留；然后 `sudo systemctl restart ethan2-telegram`。
- 生成的照片存在 `<数据目录>/media/photos/`，每天主动发的张数记在 `<数据目录>/photos.json`，花费记进账本（purpose=photo）。
