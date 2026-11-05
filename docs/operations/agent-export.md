# 导出别的 agent 的记忆和聊天（ontology 1.8）

为什么做：防封号，换 agent 也接得上（design/agent-network.md 第 3 节）；她在别的 agent 里说的话算她的原话，要进 小伴 的记忆（design/ontology-plan.md 1.8，数据源 S20）。

## 她要做的（只一次）

在她的 Mac 上、Personal-OS 目录里跑：

```sh
python3 -m ethan.agentexport --install-launchd
```

会打印生成的定时设置（`~/Library/LaunchAgents/com.ethan.agentexport.plist`）并装上：每周日 20:13 自动跑一次 `--codex --send`，日志在 `~/.config/ethan/agentexport.log`。不想要了：`launchctl unload ~/Library/LaunchAgents/com.ethan.agentexport.plist`。

想先看看会交多少：`python3 -m ethan.agentexport --codex --dry-run`（不存档、不发）。

## 各个 agent

| agent | 在哪 | 什么格式 | 多久一次 | 怎么导 |
|---|---|---|---|---|
| Codex | `~/.codex/AGENTS.md`、`~/.codex/memories/`（有的话）、`~/.codex/sessions/**/rollout-*.jsonl`（和 `archived_sessions/`） | 会话是 JSONL，一行一个事件；新版 `{"type":"response_item","payload":{"type":"message","role":"user",...}}`，旧版 `{"type":"message","role":"user",...}`，两种都认，认不出的行跳过 | 每周日 20:13 自动 | `python3 -m ethan.agentexport --codex --send` |
| Grok Bot App 里的聊天 | 没有本地文件；她在 <https://accounts.x.ai/data> 点 “Download account data”，xAI 把下载链接发到她邮箱 | zip，里面是 JSON（格式不固定，宽松解析） | 每月 1 次（工作台待办提醒） | 服务器收邮件时自动接手，见下面“Grok Bot App（服务器自动）” |
| WorkBuddy、Trae 等 | 待查 | 待查 | — | 还没做，下一步 |

## 每次跑做什么（Codex）

1. **原样存档**：新增或改过的文件（AGENTS.md、memories、会话记录；不碰 `auth.json`、`config.toml` 这类有钥匙的）打成 `~/.config/ethan/agent-archive/codex-日期-时间.tar.gz`，目录只有她能读。有密钥文件 `~/.config/ethan/agent-archive.key` 就用和记忆备份（`ethan/backup.py`）一样的 openssl 方式加密成 `.tar.gz.enc`。不进本仓库。
2. **整段对话交给 小伴**（她 11-04：“难道别人跟我的对话不是我的记忆么！！！”）：她（role=user）和 Codex 两边都交，每行【她】/【Codex】，开头〔Codex 会话 日期〕。她说的去掉 Codex 自动塞进去的块（`<environment_context>`、`<user_instructions>`、`# AGENTS.md instructions` 开头的）、不到 8 个字的点头、纯代码 / 命令 / 路径，长的粘贴截到 2000 字；Codex 说的代码块换成“［代码略］”、纯代码不交、截到 500 字；都去掉邮箱电话（同 `docquotes.redact`）。经访谈通道交（`format: "transcript"`，会话名“Codex 会话（日期）”，场合“她在 Codex 里说的（agent 记忆导出）”）。服务器整理时她说的算原话，Codex 的建议和结论记成 agent 转述（只当资料）。以前按“她说的第几段”记的进度自动换算，不重交。
3. **进度**：每个会话文件按 路径+大小+修改时间 记指纹到 `~/.config/ethan/agentexport-state.json`，一个会话全交成功才记；会话续写了只交新增的几段。
4. **钥匙**：环境变量 `ETHAN_CLOUD_TOKEN`；定时跑的时候没有终端的环境变量，就读 `~/.claude/settings.json` 里报到钩子那把（`python3 -m ethan.setup_hook --where mac` 装的）。都没有就只生成交接文件到 `~/.config/ethan/agentexport-out/`、不发。

## Grok Bot App（服务器自动，`ethan/grokexport.py`）

她 11-04：“都发到邮箱里为什么你不能自动读？还要我拖一遍？”——所以不用她拖文件。

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
- WorkBuddy、Trae 的数据在哪、什么格式。
- Grok 导出的真实格式还没见过实物：第一次导入后看一眼服务器日志里认出几个会话、交了几段，认不全再补解析。
