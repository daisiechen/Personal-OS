# 导出别的 agent 的记忆和聊天（ontology 1.8）

[English](../en/operations/agent-export.md) | 简体中文

为什么做：防封号，换 agent 也接得上（design/agent-network.md 第 3 节）；她在别的 agent 里说的话算她的原话，要进 小伴 的记忆（design/ontology-plan.md 1.8，数据源 S20）。

## 她要做的（只一次）

在她的 Mac 上、Personal-OS 目录里跑：

```sh
python3 -m ethan.agentexport --install-launchd
```

会打印生成的定时设置（`~/Library/LaunchAgents/com.ethan.agentexport.plist`）并装上：每周日 20:13 自动跑一次 `--codex --claude --send`（Codex 和 Claude Code 一起导；11-05 起加了 Claude Code，之前装过的要 `git pull` 后重跑一次这条命令），日志在 `~/.config/ethan/agentexport.log`。不想要了：`launchctl unload ~/Library/LaunchAgents/com.ethan.agentexport.plist`。

想先看看会交多少：`python3 -m ethan.agentexport --codex --dry-run`（不存档、不发；Claude Code 默认一起看，`--no-claude` 只看 Codex）。

## 各个 agent

| agent | 在哪 | 什么格式 | 多久一次 | 怎么导 |
|---|---|---|---|---|
| Codex | `~/.codex/AGENTS.md`、`~/.codex/memories/`（有的话）、`~/.codex/sessions/**/rollout-*.jsonl`（和 `archived_sessions/`） | 会话是 JSONL，一行一个事件；新版 `{"type":"response_item","payload":{"type":"message","role":"user",...}}`，旧版 `{"type":"message","role":"user",...}`，两种都认，认不出的行跳过 | 每周日 20:13 自动 | `python3 -m ethan.agentexport --codex --send` |
| Grok Bot App 里的聊天 | 没有本地文件；她在 <https://accounts.x.ai/data> 点 “Download account data”，xAI 把下载链接发到她邮箱 | zip，里面是 JSON（格式不固定，宽松解析） | 每月 1 次（工作台待办提醒） | 服务器收邮件时自动接手，见下面“Grok Bot App（服务器自动）” |
| Claude Code | `~/.claude/projects/*/memory/**/*.md`（自动记忆）、`~/.claude/agent-memory/**/*.md`（子 agent 记忆）、`~/.claude/CLAUDE.md`；各项目（从 `~/.claude/projects/*/` 会话记录的 `cwd` 找）的 `CLAUDE.md`、`CLAUDE.local.md`、`AGENTS.md`、`.claude/agent-memory(-local)/` | Markdown；自动记忆可能带前置字段 `type: user / feedback / project / reference` | 和 Codex 同一个定时任务，每周日 20:13 | `python3 -m ethan.agentexport --claude --send`（`--claude` 默认开） |
| Grok App、ChatGPT、Gemini、豆包等（没有导出功能的） | 在那个 AI 自己的记忆里 | 她贴一段固定的“导出提示词”，它按固定格式列出来 | 想起来就做一次 | 见下面“其他 AI 怎么导” |
| WorkBuddy、Trae 等 | 待查 | 待查 | — | 还没做；有聊天界面的先用“其他 AI 怎么导” |

## 每次跑做什么（Codex）

1. **原样存档**：新增或改过的文件（AGENTS.md、memories、会话记录；不碰 `auth.json`、`config.toml` 这类有钥匙的）打成 `~/.config/ethan/agent-archive/codex-日期-时间.tar.gz`，目录只有她能读。有密钥文件 `~/.config/ethan/agent-archive.key` 就用和记忆备份（`ethan/backup.py`）一样的 openssl 方式加密成 `.tar.gz.enc`。不进本仓库。
2. **整段对话交给 小伴**：她（role=user）和 Codex 两边都交，每行【她】/【Codex】，开头〔Codex 会话 日期〕。她说的去掉 Codex 自动塞进去的块（`<environment_context>`、`<user_instructions>`、`# AGENTS.md instructions` 开头的）、不到 8 个字的点头、纯代码 / 命令 / 路径，长的粘贴截到 2000 字；Codex 说的代码块换成“［代码略］”、纯代码不交、截到 500 字；都去掉邮箱电话（同 `docquotes.redact`）。经访谈通道交（`format: "transcript"`，会话名“Codex 会话（日期）”，场合“她在 Codex 里说的（agent 记忆导出）”）。服务器整理时她说的算原话，Codex 的建议和结论记成 agent 转述（只当资料）。以前按“她说的第几段”记的进度自动换算，不重交。
3. **进度**：每个会话文件按 路径+大小+修改时间 记指纹到 `~/.config/ethan/agentexport-state.json`，一个会话全交成功才记；会话续写了只交新增的几段。
4. **钥匙**：环境变量 `ETHAN_CLOUD_TOKEN`；定时跑的时候没有终端的环境变量，就读 `~/.claude/settings.json` 里报到钩子那把（`python3 -m ethan.setup_hook --where mac` 装的）。都没有就只生成交接文件到 `~/.config/ethan/agentexport-out/`、不发。

## 每次跑做什么（Claude Code，借鉴清单第 2 条）

为什么：她在 Mac 上纠正 Claude 的事、Claude 记下的她的偏好只留在那台机器上，换电脑、封号就丢了（research/borrow-2026-11.md 第 12 条、第六节第 2 条）。

1. **读哪些**：见上表。不碰 `settings.json`、`.credentials.json` 这类有钥匙的，也不读 Claude Code 的会话记录（那是第二步的事，还没做）。
2. **只交关于她的**：去掉前置字段、代码块、行内代码、链接地址；每一行要提到她（她 / Mia / 用户 / the user / she / her……）才交，命令、路径、代码、不到 8 个字的不交——项目说明里的代码规范、验证命令都不交。自动记忆前置字段 `type` 是 `user` 或 `feedback` 的整份都算关于她。单行截到 500 字，去掉邮箱电话。
3. **算转述，不算她的原话**：每行标【Claude Code】（没有【她】行），开头〔Claude Code 记忆 项目/文件 日期〕，经访谈通道交（`format: "transcript"`，`agent: "Claude Code"`，会话名“Claude Code 记忆（日期）”，场合“Claude Code 的自动记忆和项目说明里关于她的（agent 转述，不是她的原话）”）。服务器整理时只能记成 agent_relay 的假设（`lifebase.write_said_facts`），私密、只当资料，“我”页“全部记忆”里标“转述（猜测）”，她点像我 / 不像我。
4. **去重**：每一行按内容记指纹（`agentexport-state.json` 的 `claude_seen`），一个文件的新行全交成功才记；同一行出现在几个文件里只交一次；文件改了只交新增的行。
5. **存档**：`~/.claude` 里新增或改过的记忆文件原样打包成 `~/.config/ethan/agent-archive/claude-日期-时间.tar.gz`（有密钥就加密）；项目里的 CLAUDE.md 本来在各自仓库里，不另存。

## 其他 AI 怎么导（Grok App、ChatGPT、Gemini、豆包……，借鉴清单第 1 条）

为什么：这些 AI 没有导出功能，她在里面攒的“关于我”现在进不来。做法借 Claude 的“导入记忆”：给她一段固定的提示词，贴给那个 AI，让它按固定格式把记得的关于她的事列出来，她再把回答转给 小伴。

**她要做的**：
1. 打开那个 AI（最好开在它有记忆的那个账号里），把下面的提示词整段贴进去（中文的 AI 用中文版，英文界面用英文版都行）。
2. 把它的回答**原样**复制，发给 小伴：Telegram 直接粘贴发送；或者工作台“我”页 →“待你定”→ 最下面“贴别的 AI 记得的关于你”，贴进去点“导入”。Telegram 一条最多约 4000 字，太长的会被拆成几条、后面几条认不出，长的用工作台贴。
3. 去“我”页“待你定”，一条条点对 / 不对。

**系统怎么处理**（`ethan/interviews.py` 的 `parse_memory_export`、`receive_memory_export`；聊天入口 `ethan/core/agent.py` 的 `小伴.memory_export`；工作台 `me.import_ai`；访谈接口 `/cloud/interview` 也认这个格式）：
- **怎么认**：有一行抬头 `【AI 记忆导出】来自：某某`（或 `[AI MEMORY EXPORT] from: X`），并且至少一条 `- [她说过|推测][类别] 一句话（时间）`（英文 `- [said|inferred][category] one sentence (time)`）。认的时候宽松：全角括号、Markdown 的 `\[`、加粗、编号都行。
- **不当她的原话**：原文存一条原始记录，来源 `agent`（agent 转述），开头写“agent 转述，不是她的原话”，去掉邮箱电话；聊天记录里只记一行占位“她转来一份某某的记忆导出”，所以每晚的聊天整理不会把它当成她说的。小伴 不调模型，回一句固定的话（新记几条、跳过几条）。
- **每条**：一条假设（kind = hypothesis，source_kind = agent_relay，私密，可信度：说她说过的 0.3、它推测的 0.2），写成“某某记得她说过：……”或“某某推测：……”，语境写哪个 AI、她说过还是它推测、大概时间；有具体到年的时间就记成开始时间。类别对到十个方面（认不出放“偏好”）。同时在“待你定”放一题“某某记得的关于你：对吗？”：对 = 她确认（锁住），不对 = 作废。
- **去重**：同一个 AI 同一句话按指纹（表 `memx_seen`）不导第二次；和已有的说法（包括她点过“不对”的）差不多的跳过。一次最多 80 条。

**提示词（中文版）**，整段复制：

```text
请把你记得的、关于我这个人的所有事情列出来，我要把它导入我自己的个人记忆系统。只列关于我本人的（经历、工作和学习、处境、住在哪个城市、习惯、偏好、性格、看法、身体和健康、重要的人和关系、目标），不要列你自己的设置，也不要列我让你做过的具体任务内容。

严格按下面的格式输出，不要加别的解释：

【AI 记忆导出】来自：（写你的产品名，比如 ChatGPT、Grok、Gemini、豆包）
- [她说过][类别] 一句话写一件事。（大概时间）
- [推测][类别] 一句话写一件事。（大概时间）

规则：
1. 每条一行、只写一件事，一句话说完，用“她”指我。
2. 方括号第一格：我亲口告诉过你的写“她说过”；你自己从聊天里推断、总结出来的写“推测”。拿不准的算“推测”。不要编，不记得的不要写。
3. 方括号第二格从这些里选一个：价值观、性格、思维、经历、表达、偏好、身体、关系、处境、习惯。
4. 括号里写这件事大概是什么时候的（比如 2025-03、2024 年、不确定）。
5. 不要写电话、邮箱、门牌地址、证件号、密码。
6. 能想起来的都列上，越全越好；最后不要写总结。
```

**Prompt (English)**, copy the whole block:

```text
Please list everything you remember about me as a person. I am importing it into my own personal memory system. Only include things about me (background, work and study, situation, which city I live in, habits, preferences, personality, opinions, body and health, important people and relationships, goals). Do not include your own settings or the details of tasks I asked you to do.

Output strictly in this format, with no other commentary:

[AI MEMORY EXPORT] from: (your product name, e.g. ChatGPT, Grok, Gemini)
- [said][category] One sentence about one thing. (approximate time)
- [inferred][category] One sentence about one thing. (approximate time)

Rules:
1. One item per line, one fact per item, one sentence, referring to me as "she".
2. First bracket: "said" if I told you directly; "inferred" if you deduced or summarised it yourself. If unsure, use "inferred". Do not make things up.
3. Second bracket, pick one: values, personality, thinking, background, communication, preferences, health, relationships, situation, habits.
4. In parentheses, when this was roughly true (e.g. 2025-03, 2024, unknown).
5. No phone numbers, emails, street addresses, ID numbers or passwords.
6. List as much as you can remember; no summary at the end.
```

## Grok Bot App（服务器自动，`ethan/grokexport.py`）

所以不用她拖文件。

**她要做的**：每月点一下。每月第一次每晚整理时，工作台待办里出现一条“去 https://accounts.x.ai/data 点 Download account data（几月；点完就不用管了，邮件到了 小伴 自动导）”。这个月已经放过、她删过这个月这条、最近 20 天刚导过，都不放。

**邮件到了以后**（`ethan/mail.py` 分类时先认）：
1. **认邮件**：发件人是 x.ai / xAI，主题或正文开头有 data export / download / archive / 数据 / 导出 之类，正文里有 **x.ai 域名下的 https 链接**（别的域名、http、`x.ai.evil.com` 都不认；像下载的 download / export / archive 排前面，只有 data 的排后面，页脚的隐私政策不要；最多 3 个）。只有网页版的信，`imapmail.parse` 把 href 里的 x.ai 链接提到正文最前面，免得去标签时丢了。这封在工作台“邮件和留言”里是“知道一下：xAI 的数据导出：小伴 在自动下载、导进记忆，不用你管”，**不算要她处理的事**，照常归档。
2. **下载**（后台，`python3 -m ethan.grokexport --data <目录> --jobs`；收邮件的任务跑完发现有排着的就起它）：urllib 直接下，**不带她的任何登录凭据**；最多 500MB、超时 120 秒。结果：
   - 拿到 zip / JSON → 导入（下一步），这封的说明改成“小伴 导好了（交了几段你说的话）”；
   - 拿到网页（登录页）/ 401 / 403 → 不硬来：这封改成**要她看**（归档走了的放回收件箱），说明写“xAI 的导出要你登录才能下载：点这个链接 … 下载后，把文件原样发给 小伴（Telegram 直接发 zip 就行），他自动导进记忆”；
   - 太大 → 要她看，“下载后解压出里面的 JSON 发给 小伴”；
   - 网络错误 → 半小时后再试，3 次不成要她看。
3. **她自己发文件也一样**：Telegram / 微信发来的 zip 存进 `media/`（收下后马上后台 `--scan`，文件那边每 30 分钟也顺带看一次）；是 Grok 的导出（zip 里文件名有 grok，或 JSON 里有 grok 字样）就导入，之前“要你登录”的那条自动收起；别的压缩包、普通 JSON 不碰。服务器会话里也可以直接 `python3 -m ethan.grokexport --data /home/ethan/data --file 导出.zip`。Telegram 机器人只能收 20MB 以内的文件。
4. **导入**：zip 只在内存里读 `.json` / `.jsonl`（解开后最多 300MB），不解到磁盘。宽松解析：遍历所有对象，含 messages / responses / conversation / chat_messages / turns 这类数组、数组里一半以上能认出“谁说的 + 文字”的，算一个会话（消息可以包一层 response / message；谁说的看 sender / role / author，文字看 message / content / text，时间认 ISO、秒、毫秒、`{"$date": …}`）。sender 是 human / user 的算她。两边都交（整段对话）：每行【她】/【Grok】，开头〔Grok Bot App 会话 日期「标题」〕，Grok 的话不截短（单条超 2000 字截断），她的话筛法和 Codex 一样（`agentexport.clean`），都去邮箱电话；**直接在服务器上写访谈记录**（`interviews.receive`，`format: "transcript"`，不走 HTTP）：会话名“Grok Bot App 会话（日期）”，场合“她在 Grok Bot App 里说的（agent 记忆导出）”，收到就在后台整理：她说的算原话，Grok 说的记成 agent 转述（只当资料）。两边每条消息都记指纹，续聊后再导只交新增的。
5. **存档**：原始包原样存 `<数据目录>/agent-archive/grok-日期-时间.zip`（目录 700、文件 600，不进本仓库）；服务器上有 `~/.config/ethan/agent-archive.key` 或记忆备份的 `backup.key` 就用同样的 openssl 方式加密成 `.enc`。
6. **去重**：同一个包（内容哈希）不导第二次；每条她说的话记指纹（会话 id + 消息 id 或时间 + 文字，表 `grok_seen`），续聊后再导只交新增的；一个会话全交成功才记。
7. **来源健康**：S20 “Grok Bot App 导出”，45 天没有新的才报断档（每月一次，晚一两周不报）。

表：`grok_jobs`（邮件里排着的下载）、`grok_files`（看过的包）、`grok_seen`（交过的话）、`grok_meta`（每月提醒、最近一次导入）。

## 还没做（TODO）

- 加密包推到独立私有仓库（和 ethan-vault 一样的做法）：要她建仓库、在 Mac 上放密钥文件，到时候给她最小的一步。现在只在 Mac 本地。
- WorkBuddy、Trae 的数据在哪、什么格式（有聊天界面的先用“其他 AI 怎么导”）。
- Claude Code 的会话记录（`~/.claude/projects/*/*.jsonl`）里她说的话：和 Codex 会话一样交整段对话，还没做。
- 导出提示词各家 AI 实际吐出来的格式还没见过实物：第一次导入后看认出几条，认不全再放宽解析。
- Grok 导出的真实格式还没见过实物：第一次导入后看一眼服务器日志里认出几个会话、交了几段，认不全再补解析。
