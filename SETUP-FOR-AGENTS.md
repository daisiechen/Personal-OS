# SETUP-FOR-AGENTS：替用户搭一个自己的 小伴

你是用户的 AI agent。这个仓库是一个陪伴型 AI（默认叫小伴，用户可以改名）加一个“把人生当游戏”的工作台。你的任务是在用户自己的服务器上把它装好，并按用户的情况改成他自己的。按下面的顺序做，每一步做完都按“验证”检查，过了再往下。

## 规则

- 密钥（模型钥匙、聊天机器人凭据、各种 token）不要让用户发在聊天里，也不要写进仓库。让用户自己在服务器上运行 `python3 -m ethan.setup_secrets` 录入。
- 一次只问用户一个问题，用大白话；用户说“你定”就按本文给的默认值。
- 改完任何代码都跑 `python3 -m unittest discover -s tests`，全过才算完成。
- 用户的个人内容（目标、身体数据、人设细节）只放在他自己的服务器和数据目录里，不提交到公开仓库。

## 第 1 步：问清三件事

1. 在哪跑：不一定要买服务器，一台一直开着的电脑就行（Linux 最省事，Windows 用 WSL；Mac 要把定时任务换成 launchd）。
   在自己电脑上跑的代价：电脑关机或睡眠时 小伴 不会主动找人、早上不排计划；想在手机上打开工作台、接 iPhone 定位或银行邮件这类“从外面打进来”的数据，要开一条免费的 Cloudflare Tunnel。
   只用微信聊天、电脑常开的话，本地完全够用；想省心就买一台最便宜的云服务器（2 核 2 GB）。
2. 用哪个模型。默认 DeepSeek（国内能用、便宜）。能用 Claude 订阅的也可以用。
3. 用哪个聊天入口。默认微信（官方插件，能收发文字、语音、图片；限制：不能主动推送，小伴 主动找他要等他先说话）。能翻墙的可以选 Telegram（能主动推送）。

## 第 1.5 步：用户有目标表格的话，直接用它冷启动

让用户把目标表格（Excel、飞书、Notion、截图都行）给你，你按下面的规则拆，拆完给用户看一眼再写进系统，不用一项项问。

| 表格里的 | 拆成 | 规则 |
|---|---|---|
| 这三个月最想完成、有截止日的事 | 副本（赛季目标），最多 5 个 | 每个写清怎么算完成（一个数字或一个结果）；最重要的一个标成主副本 |
| 每个副本 | 主线步骤 | 不用你拆：写进副本后，计划器第二天早上会自动拆成 6–20 步给用户过目 |
| 每天、每周要坚持的事（训练、早睡、读书） | 日常 / 周常 | 每天的 3–7 件，每周的 1–2 件，太多会让人放弃 |
| 想守住的底线（体重、花销、睡眠、联系朋友） | 领地的健康线 | 每块领地 1–3 条，写成“每周至少 / 每月不超过”的形式 |
| 零散的待办 | 支线 | 原样放进去，小伴 会按标题猜它属于哪块领地 |
| 要查资料、比价、起草的事 | 小伴 替你准备 | 小伴 会自己做好再交给用户 |
| 长期愿望（没有截止日） | 候选副本 / 心愿 | 先不进这个赛季，放候选池 |

写进去用 `python3 -m ethan.goals_import --data <数据目录> <你整理好的 goals.json>`（格式见 `examples/goals.json`，字段说明在 `ethan/goals_import.py` 开头）。

## 第 2 步：装起来

1. 服务器上建 `ethan` 用户，把仓库放到 `/home/ethan/Personal-OS`，运行 `deploy/install.sh`。
2. 让用户自己运行 `python3 -m ethan.setup_secrets`，只录第 1 步选的那几项，其余回车跳过。模板在 `deploy/ethan.env.example`，每一项是什么写在里面。
3. 按 `docs/operations/deploy.md` 绑定聊天入口（微信扫码，或 Telegram 配对码）。

验证：用户在聊天入口发“你好”，小伴 能回；`systemctl list-timers | grep ethan` 能看到定时任务。

## 第 3 步：冷启动（这些必须是用户自己的，逐项问）

| 要填的 | 问用户什么 | 填到哪 |
|---|---|---|
| 你的 AI 叫什么、是谁 | 给它起个名字（这一步必须问，不要沿用示例名字“小伴”，除非用户就想叫这个）；想要一个什么样的陪伴者：男友、闺蜜、导师、搭子？说话什么风格 | `persona/ethan.md`（人设）、`persona/script-guide.md`（他自己的日常生活） |
| 这个赛季的副本 | 接下来三个月最想完成的 3–5 件事，每件怎么算完成 | 让用户直接跟 小伴 聊，计划器会拆成主线步骤给他过目；或参照 `ethan/goals2026.py` 写 |
| 日常和周常 | 每天想坚持的 3–7 件小事，每周想做到的 1–2 件 | `ethan/quests.py` 的 `DEFAULT_HABITS`，或让用户跟 小伴 说“加一项坚持……” |
| 领地的健康线 | 每块领地什么算“及格”（比如每周练 3 次、每月花不超过多少） | `ethan/territory.py` |
| 身体和饮食 | 身高体重、目标、能用的器械、忌口 | `playbooks/body/`、`playbooks/food/` |
| 地点和时区 | 住在哪个城市、常去的地方 | `ethan.env` 里的 `ETHAN_TZ`（如 Asia/Shanghai）和 `ETHAN_WEATHER_LATLON`；`deploy/systemd/*.timer` 里的 `Europe/London` 换成同一个时区；常去的地点让用户跟 小伴 说 |
| 财务、社交、学习的起点 | 不用现在问：小伴 会按 `ethan/domains.py` 的冷启动流程，挑时间带用户每块聊 10–15 分钟 | 自动 |

验证：工作台网页（`ethan/workbench/`）首页能看到用户的副本、日常和今天的计划。

## 第 4 步：换世界观（可选，问用户要不要）

默认外壳是“地球 Online”：人生是一场游戏，按赛季打副本、守领地、攒积分换惊喜。用户想换的话：

| 换什么 | 改哪里 |
|---|---|
| 游戏名、口号、白天黑夜两个形象 | `ethan/workbench/static/app.js` 首页部分 |
| 领地几块、叫什么 | `ethan/territory.py` 的 `TERRITORIES` |
| 世界地图 | `ethan/workbench/static/map-day.jpg`、`map-night.jpg`（可以让生图模型画） |
| 等级称号 | `ethan/quests.py` 的 `TITLES` |
| 成就和隐藏成就 | `ethan/achievements.py`（隐藏成就的条件不要告诉用户，只给谜语提示） |
| 商店惊喜档位 | `ethan/quests.py` 的 `DEFAULT_REWARDS` |
| 成就解锁的惊喜玩法 | `ethan/surprises.py`（同样别剧透） |
| 今日卡的画风 | `ethan/cards.py` 的 `STYLE`、`SCENES` |

## 第 5 步：平替和可选插件（按用户需要逐个接）

| 部件 | 默认 | 平替 / 不接会怎样 | 在哪 |
|---|---|---|---|
| 主力模型 | 第 1 步选的 | 兼容 OpenAI 接口的都能换：DeepSeek、通义、Kimi、智谱 | `ethan.env` 里的 `ETHAN_LLM_BASE_URL`（DeepSeek 填 `https://api.deepseek.com`）、`ETHAN_LLM_API_KEY`、`ETHAN_LLM_MODEL`（DeepSeek 填 `deepseek-chat`）；三项都不填就用 Claude Code 订阅 |
| 生图（今日卡、照片） | 即梦 / Grok Imagine / Gemini | 不接就没有今日卡和照片 | `ethan/photos.py`、`ethan/cards.py` |
| 语音回复 | MiniMax | 不接就只发文字 | `ethan/core/tts.py` |
| 听语音 | 本地 faster-whisper | 不用改 | `ethan/channels/media.py` |
| 上网跑腿 | Grok Bot | 不接时 小伴 会直说做不了 | `ethan/grok.py` |
| 日历 | 苹果日历 | 不接计划照样排 | `ethan/icloud.py` |
| 记账 | 银行邮件、Monzo | 不接也行：把消费通知转给 小伴，他会记 | `ethan/finance.py`、`docs/operations/finance.md` |
| 出门、工作时长 | iPhone 快捷指令、Mac 小程序 | 不接，相关打卡显示“等数据” | `docs/operations/devices.md` |

## 第 6 步：交付给用户

告诉用户三件事：去哪聊（聊天入口）、去哪看（工作台网址）、出问题看哪（工作台设置页的“系统体检”）。以后的维护是自动的：每 2 分钟自动部署、每天加密备份记忆（`docs/operations/rebirth.md`）。
