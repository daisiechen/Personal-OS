# 导出她的微信聊天（全部历史），交给 小伴（ontology 1.9）

为什么做：她 11-04 问“那我要导出所有历史会话怎么办？”——要**全部历史**，不止开源工具 `read` 的每会话 200 条。
交给 小伴 的是**整段对话**（她 11-04：“难道别人跟我的对话不是我的记忆么！！！”）：她说的算原话，别人说的关于她的记成“别人说的”（写明是谁说的），只关于别人的事交给人物卡（design/ontology-plan.md 1.9，数据源 S19）。

怎么做的：钥匙用开源工具 [DaneelOlivaw1/macos-wechat-export](https://github.com/DaneelOlivaw1/macos-wechat-export)
取（只给微信的**临时副本**重签名、用 LLDB 启动副本取钥，原版微信和 SIP 都不动）；读聊天用本仓库的 `ethan/wechatexport.py`，
照那个工具的读法（SQLCipher 4、raw key、只读）自己读全部历史。

> **注意**：那个工具实测的是 Apple Silicon + **macOS 15.6.1** + 微信 4.1.15。她的 Mac 是 **macOS 26.6**，不在它的实测范围。
> 哪一步失败了，把那一步终端里打出来的报错原样发回开发会话（报错里没有钥匙；**不要**发 keys.json 的内容）。

## 一、从零开始的全部命令

在 Mac 上打开“终端”（启动台 → 其他 → 终端），一条一条复制粘贴、回车，等它跑完再贴下一条。
每条都自己带了 `cd` 或绝对路径，不用管“现在在哪个目录”。

### 1. 装 Homebrew 和工具

已经装过 Homebrew 的跳过第一条（`brew --version` 有输出就是装过）。

```sh
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

```sh
grep -q 'brew shellenv' ~/.zprofile 2>/dev/null || echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile; eval "$(/opt/homebrew/bin/brew shellenv)"
```

```sh
xcode-select -p || xcode-select --install
```

（弹出安装“命令行开发者工具”的窗口就点安装，装完再继续。）

```sh
brew install sqlcipher python@3.14 jq
```

```sh
python3.14 --version && sqlcipher -version
```

### 2. 下载取钥匙的工具（固定在审过的版本）

```sh
mkdir -p ~/tools && git clone https://github.com/DaneelOlivaw1/macos-wechat-export.git ~/tools/macos-wechat-export
```

```sh
git -C ~/tools/macos-wechat-export checkout 88ca1bef06279f3bc9fa27a379b417f71b6e88a5 && git -C ~/tools/macos-wechat-export rev-parse HEAD
```

（`88ca1bef…` 是 2026-11-04 开发会话读过源码的那个提交；最后打出来的应该就是这串。）

```sh
cd ~/tools/macos-wechat-export && sh build.sh && sh test.sh && python3.14 test_db.py
```

`build.sh` / `test.sh` 是它发消息用的界面部分，我们用不到；这两步报错可以先跳过，只要 `python3.14 test_db.py` 能过。

### 3. 选她的微信账号

```sh
cd ~/tools/macos-wechat-export && python3.14 disk_inventory.py | jq '.accounts[] | {account_index, database_count}'
```

```sh
ls -d ~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/*/db_storage
```

系统弹窗问“终端想访问其他 App 的数据”时点**允许**。第二条列出的就是每个账号的目录，只有一个就是它；有好几个时，
选 `database_count` 最大、修改时间最近的那个（拿不准把这两条的输出发回来，**不要猜**）。
把下面命令里的路径换成那一行，记进一个只有她能读的小文件，以后的命令都从这里读：

```sh
mkdir -p ~/.config/ethan && chmod 700 ~/.config/ethan && printf "WX_DB='%s'\nWX_KEYS='%s'\n" "$(ls -d ~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/*/db_storage | head -1)" "$HOME/tools/macos-wechat-export/.wechat-local/keys.json" > ~/.config/ethan/wechat-export.env && chmod 600 ~/.config/ethan/wechat-export.env && cat ~/.config/ethan/wechat-export.env
```

（这条默认取第一个账号；有好几个账号时，打开 `~/.config/ethan/wechat-export.env` 把 `WX_DB=` 那行改成对的路径。）

### 4. 退出微信，取钥匙

先把要发的草稿发完，然后在微信里按 **⌘Q** 退出（不是关窗口）。确认退干净了：

```sh
pgrep -x WeChat && echo "微信还开着，再按一次 ⌘Q" || echo "已退出"
```

```sh
source ~/.config/ethan/wechat-export.env && cd ~/tools/macos-wechat-export && python3.14 bootstrap_keys.py --db-root "$WX_DB" --state-dir ~/tools/macos-wechat-export/.wechat-local --acknowledge-debug-copy
```

会弹出一个**临时的微信窗口**：点“进入微信”，手机上要确认就确认。不要在这个临时窗口里聊天。
等终端里出现 `keys_saved_privately`，它会自己关掉临时副本、重新打开原版微信（`original_launch_requested` 只是请求打开，
原版没开就自己点开）。出现 `bootstrap_error` 就停下，把那一行发回来。

> 这一步脚本拒绝覆盖旧的钥匙：要重来，把 `--state-dir` 换成新的目录（比如 `~/tools/macos-wechat-export/.wechat-local-2`），
> 并把 `~/.config/ethan/wechat-export.env` 里的 `WX_KEYS` 改成新目录下的 `keys.json`。

### 5. 核对钥匙能打开所有库（只读）

```sh
source ~/.config/ethan/wechat-export.env && cd ~/tools/macos-wechat-export && python3.14 wechat_db.py --db-root "$WX_DB" --key-file "$WX_KEYS" verify | jq '{verified_count, failed: [.databases[] | select(.verified | not) | .database]}'
```

`failed` 是空的 `[]` 就对了。不是空的，把这段输出发回来。

### 6. 找她自己的 sender id

在微信里给**文件传输助手**发一条独特的文字：`小伴身份核对1104`。然后：

```sh
source ~/.config/ethan/wechat-export.env && cd ~/tools/macos-wechat-export && python3.14 wechat_db.py --db-root "$WX_DB" --key-file "$WX_KEYS" read --chat-id filehelper --limit 10 | jq -r '.messages[] | select((.text // "") | contains("小伴身份核对1104")) | .sender_id'
```

打出来的那串（一般是 `wxid_` 开头）就是她的 sender id。记进小文件（把 `替换成上面那串` 换掉）：

```sh
echo "WX_SELF='替换成上面那串'" >> ~/.config/ethan/wechat-export.env && cat ~/.config/ethan/wechat-export.env
```

没打出东西：等十几秒再跑一次（微信写库有延迟）；还是没有，把 `| jq -r ...` 去掉跑一次，看最后几条是不是那句话。

### 7. 先看一眼有多少（不写不交）

代码合进 main 之前，先把 Mac 上的 Personal-OS 切到这个分支（合进 main 之后把第一条换成 `git checkout main && git pull`）：

```sh
cd "$(find ~ -maxdepth 4 -type d -name Personal-OS 2>/dev/null | head -1)" && git checkout main && git pull origin main
```

```sh
source ~/.config/ethan/wechat-export.env && cd "$(find ~ -maxdepth 4 -type d -name Personal-OS 2>/dev/null | head -1)" && python3.14 -m ethan.wechatexport --db-root "$WX_DB" --keys "$WX_KEYS" --self-id "$WX_SELF" --dry-run
```

会打出：会话数（`chats`）、新消息总数（`new`）、她说的条数（`her`）、每个会话的条数（`per_chat`，`send` 是会不会交给 小伴）。
`her` 是 0 或者结果里有“--self-id … 找不到”，说明第 6 步的 id 不对，别往下走。

### 8. 导出全部历史并交给 小伴

```sh
source ~/.config/ethan/wechat-export.env && cd "$(find ~ -maxdepth 4 -type d -name Personal-OS 2>/dev/null | head -1)" && python3.14 -m ethan.wechatexport --db-root "$WX_DB" --keys "$WX_KEYS" --self-id "$WX_SELF" --send
```

- 每个会话写到 `~/wechat-export/<会话名>.jsonl`（目录只有她能读）。
- 一次最多交 30 批（每批约 1.2 万字），免得服务器一晚整理不完；结果里有“剩下的下次接着交”就是还没交完，**隔天**再跑一次同一条命令，
  直到没有这句话（每周日的自动任务也会接着交）。
- 只想交某几个会话：末尾加 `--chats "小王,读书会"`；群默认只交她说过话的，全都要加 `--all`；也想导文件传输助手加 `--filehelper`。
- 只导出不交：把 `--send` 换成 `--export`。
- `--send` 时顺带把私聊统计交给服务器建人物卡（工作台“关系”页，design/relations.md）；某些人不想建卡：末尾加 `--people-skip "张三,某某"`（名字里含就跳过），装每周自动跑时也带上。

### 9. 装每周日自动跑

```sh
source ~/.config/ethan/wechat-export.env && cd "$(find ~ -maxdepth 4 -type d -name Personal-OS 2>/dev/null | head -1)" && python3.14 -m ethan.wechatexport --db-root "$WX_DB" --keys "$WX_KEYS" --self-id "$WX_SELF" --install-launchd
```

每周日 20:43 增量导出 + 交，日志 `~/.config/ethan/wechatexport.log`。马上试跑一次、看日志：

```sh
launchctl start com.ethan.wechatexport; sleep 60; tail -30 ~/.config/ethan/wechatexport.log
```

日志里有 `Operation not permitted`：系统设置 → 隐私与安全性 → 完全磁盘访问权限 → 点 “+”，按 ⌘⇧G 输入
`/opt/homebrew/opt/python@3.14/Frameworks/Python.framework/Versions/3.14/Resources/Python.app` 加进去，打开开关，再跑上面那条。
不想要了：`launchctl unload ~/Library/LaunchAgents/com.ethan.wechatexport.plist`。

## 二、它做了什么（给以后的 agent）

| 项 | 做法 |
|---|---|
| 只读 | 每个库连同 `-wal` / `-shm` 复制到临时目录（复制前后大小、修改时间不变才算数，微信开着也行），只开副本：优先 python 的 `sqlcipher3`，没有就用 brew 的 `sqlcipher -readonly`（钥匙走标准输入，不进命令行），都设 `PRAGMA cipher_compatibility=4`、`PRAGMA query_only=1`；读完删副本。微信目录不写一个字节（测试 `test_readonly_and_temp_cleaned`）。 |
| 钥匙 | 工具生成的 `keys.json`：`{"keys": {"message/message_0.db": {"enc_key": "64 位十六进制"}}}`；也认按盐值（库文件头 16 字节）、绝对路径登记的。每个库必须正好一把；文件必须是 600。 |
| 会话 | `session/session.db` 的 `SessionTable.username`；名字从 `contact/contact.db` 的 `contact`（备注 > 昵称 > 微信号）。去掉公众号 `gh_`、服务号和系统号（`weixin`、`brandsessionholder` 等）、折叠入口；文件传输助手要 `--filehelper`。 |
| 消息 | `message/message_<n>.db` 里的 `Msg_<md5(username)>`：`local_id`、`server_id`、`local_type`（低 32 位种类、高 32 位子类）、`create_time`、`real_sender_id`（`Name2Id` 的 rowid）、`message_content`（`28 b5 2f fd` 开头是 zstd，用 python3.14 自带的解）。群里别人的正文前带 `<username>:\n`。文字照写；图片、语音、视频、表情、位置、名片、通话、链接、文件、红包、转账等写 `[图片]` 这类占位；引用回复取回复的那句。 |
| 文件 | `~/wechat-export/<会话名>.jsonl`，一行一条 `{"time", "who", "is_her", "text", "type", "sender", "db", "local_id", "server_id"}`；同名会话加微信号哈希。 |
| 增量 | `~/.config/ethan/wechatexport-state.json`：每个会话每个库最后的 `local_id`、库的盐值、最后的 `create_time`（库被重建了按时间接着导）、已交到第几行。 |
| 交给 小伴 | 整段对话，两边都交：一天一段，开头〔日期〕，一行一句标说话人【她】/【备注名或群成员名】；单条超过 2000 字截断，图片语音这类只有占位的不交；电话邮箱住址用 `docquotes.redact` 去掉。会话名“微信 · <会话名>”，场合“她的微信聊天（导出）”，经 `cloud.CloudClient.interview` 交（`format: "transcript"`），一批最多 12000 字，同一会话按顺序；一批成功才前进，失败的下次接着交。私聊都交；群只交她说过话的群、她说过话的那几天（`--chats` 只交点名的，`--all` 所有群所有天）。服务器收到就在后台一次整理完（不等每晚）。钥匙读 `ETHAN_CLOUD_TOKEN` 或 `~/.claude/settings.json`（同 agentexport）；没钥匙只生成交接文件到 `~/.config/ethan/wechatexport-out/`（用 `python3 -m ethan.cloud interview --file` 交，服务器自己认出是整段对话）。 |

## 三、还没做 / 待核实

- macOS 26.6 上取钥匙能不能成（那个工具没测过）。
- 群成员显示名只用联系人表的备注 / 昵称，没读群昵称（`chat_room.ext_buffer`），不在联系人表里的成员显示微信号。
- “人”和“关系”从这里补（1.9 的验收第 3 题）要等第一次交完、服务器整理后再看。
