# 她的 Mac 和 iPhone 报数据

[English](../en/operations/devices.md) | 简体中文

两样东西接上以后，领地健康线里“本人工作时长”“每天出门”“出门做非工作的事”就不再是“等记录”，
设置页“连接”里对应两行变绿，成就“每天出门”开始算连续天数。
钥匙都用装会话报到钩子时写进 Mac `~/.claude/settings.json` 的那把（ETHAN_HOOK_TOKEN），不用另外录。
钥匙不进聊天、截图和仓库。

## Mac 工作时长

算法：Mac 键鼠活跃时间加上她在 Claude 里发消息的时间，agent 自己在跑不算。后来加的来源和 11-01 的修正见下文“工作狂指数和晚上收工”。
每 5 分钟看一次键鼠，这 5 分钟里动过就记一格；断网时先攒在 `~/.ethan-worktime.json`，下次一起补发。
每天不超过 8 小时为绿，8 到 10 小时黄，超过 10 小时红。代码见 `ethan/worktime.py`。

装（在 Mac 上的仓库目录里跑一次，她自己或 Mac 上的 Claude 会话都可以）：

```bash
python3 -m ethan.worktime --install   # 装成 launchd 任务，每 5 分钟记一次，开机自启
python3 -m ethan.worktime --once      # 想马上试一次
```

不想记了就 `launchctl unload ~/Library/LaunchAgents/me.mia.ethan-worktime.plist`。
只发“哪个 5 分钟有活动”，不发按了什么键、开了什么应用。

## iPhone 定位（出门 / 到家）

只报“离开家”“到达家”这两件事，不报去了哪，不追踪路线。代码见 `ethan/places_log.py`。
- 当天第一次离开家，自动打卡日常“每天出门”（第一次收到时这个日常自动打开）。
- 一趟出去满 1 小时、又不是去训练的（计划里训练出发时间前后 1 小时内出门的算训练），记一次“出门做非工作的事”。

设置（iPhone“快捷指令” App → 自动化，做两条；她本人操作，约 5 分钟）：

1. 先把钥匙送到 iPhone：Mac 上跑 `python3 -m ethan.worktime --copy-token`，钥匙进剪贴板但不显示；
   靠“通用剪贴板”在 iPhone 上粘贴。粘完随便复制点别的，把剪贴板盖掉。
2. 新建个人自动化 →“离开”→ 选家的位置 → 选“立即运行”（关掉“运行前询问”）。
3. 动作选“获取 URL 内容”：
   - URL：`https://ethan.example.com/cloud/place`
   - 方法：POST
   - 头部：`Authorization` = `Bearer ` 后面接粘贴的钥匙
   - 请求体：JSON，加一个文本字段 `event` = `leave`
4. 再建一条“到达”→ 家，同样的动作，`event` 换成 `arrive`。
5. 试一下：在自动化里点“运行”，几分钟内工作台设置页“手机定位”这一行变绿就是通了。

iPhone 的位置自动化偶尔会晚几分钟，或漏一次，健康线按天算，影响不大。

## 工作狂指数和晚上收工

算法和规则见 `ethan/workaholic.py` 开头。工作时长现在有四个来源：Mac 键鼠、她给 Claude Code 发的话（Mac 和云端都算）、
iPhone 上开着 Claude / Codex App 的时间、工作台开着在前台的时间。在 Telegram 跟 小伴 聊天不算。
产出只算事业和学术。晚上 9 点提醒、10 点劝、11 点停；跟 小伴 说“今晚要赶，到几点”放行。

**工作时长算法修正**：她问“今天工作 9.6 小时咋算的？算错了吧？”查出好多格不是她在干活，改成：
- **服务器上的 Claude 会话不算**：那里几乎全是 小伴 系统自己在后台调 Claude（分邮件、写日记）和开发会话发给服务器会话的消息；第一句是“Mia：”的系统调用在哪都不算。10-31 起误记的那些格已由迁移 `20261101_drop_fake_claude_slots` 删掉。
- **工作台只在最近 3 分钟有点击、滑动、打字时才算在用**：屏幕一直开着停在工作台不算（之前开着在前台就算）。
- **一天从早上 5 点算起**：凌晨 0–5 点算前一晚，“昨晚的 2 小时”不进今天。
- **工作狂指数看健康区间 20–49**（不是越低越好，定一个健康区间）：20 以下偏松、20–49 健康、50–69 偏高、70 以上过度。首页写“健康 20–49”。

**终端 codex 的 11 点检查和她的别名**：她给 codex 设过别名，装检查时 zsh 报 parse error（不能用别名定义同名函数）。现在先记下别名原来的内容、取消别名、再用 `function codex` 定义检查；放行时照她原来的别名运行。

**收工钩子放行定时任务和巡检（她授权：“授权，你搞吧”）**：云端开发会话的收工钩子遇到两类消息直接放行，不挡：定时任务唤醒会话的消息（排队送进来的远程通知）和写着“定时巡检”的巡检消息。所以 11 点以后巡检照样跑，巡检恢复全天每 3 小时一次（见 [deploy.md](deploy.md)“系统巡检”）。她自己 11 点后发的新指令照旧挡回。云端会话从上层目录开时读不到仓库里的钩子，要在云端环境的启动脚本里装一次，见 [cloud-curfew.md](cloud-curfew.md)。

**提醒的样子**：Mac 全屏提醒第一句做大标题、其余换行小字、宽度限在屏幕中间（她截图：一行太长显示不全）。iPhone 快捷指令看 `message` 有没有值（见下）。

### Mac（在 Mac 上的仓库目录里，先 `git pull` 拿到新代码）

```bash
python3 -m ethan.worktime --install                  # 重装一次：记录脚本会 11 点后弹全屏提醒（不锁屏），终端里的 codex 也加上 11 点检查
python3 -m ethan.setup_hook --where mac --ssh my-server  # 重装一次：Claude Code 加上收工钩子，11 点后新指令挡回
```

### iPhone（快捷指令 → 自动化，Claude 和 Codex 各两条，约 5 分钟）

钥匙同上面“iPhone 定位”那把（Mac 上 `python3 -m ethan.worktime --copy-token`，用通用剪贴板粘）。

1. 新建个人自动化 →“App”→ 选 Claude → 勾“已打开” → 立即运行（关掉“运行前询问”）。动作：
   - “获取 URL 内容”：URL `https://ethan.example.com/cloud/activity`，POST，头部 `Authorization` = `Bearer 钥匙`，
     JSON 两个文本字段 `event` = `open`、`app` = `Claude`；
   - “获取词典值”：键 `message`（只有该停的时候才有文字，平时是空的）；
   - “如果”词典值 有任何值：里面放“显示提醒”（内容用这个词典值）和“前往主屏幕”，两个都要缩进在“如果”和“结束如果”之间。
     （的 Grok 白天也弹“11 点了”：条件用 `stop` 判断容易配错，改成看 `message`。）
2. 再建一条：App → Claude → 勾“已关闭”→ 立即运行，动作只要“获取 URL 内容”，`event` 换成 `close`。
3. Codex（或 ChatGPT）App 照做两条，`app` 写 `Codex`。
4. 试一下：打开、关掉一次 Claude App，工作台首页“今天工作 X 小时”会多一点。
