# 她的 Mac 和 iPhone 报数据（2026-10-28）

两样东西接上以后，领地健康线里“本人工作时长”“每天出门”“出门做非工作的事”就不再是“等记录”，
设置页“连接”里对应两行变绿，成就“每天出门”开始算连续天数。
钥匙都用装会话报到钩子时写进 Mac `~/.claude/settings.json` 的那把（ETHAN_HOOK_TOKEN），不用另外录。
钥匙不进聊天、截图和仓库。

## Mac 工作时长

算法（她 2026-10-27 定）：Mac 键鼠活跃时间加上她在 Claude 里发消息的时间，agent 自己在跑不算。
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
