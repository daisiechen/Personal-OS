# Exporting your WeChat history to Buddy

English | [简体中文](../../operations/wechat-export.md)

> Translated from the Chinese original. If they disagree, the Chinese version wins. Terms: [glossary](../../glossary.md).

Why: to give Buddy your **entire** WeChat history, not just the latest 200 messages per chat that open-source readers usually return.
Buddy gets **whole conversations**: what you said counts as verbatim quotes; what others said about you is stored as "someone else said" (with who said it); things that are only about other people go to person cards.

How: the database key is extracted with the open-source tool [DaneelOlivaw1/macos-wechat-export](https://github.com/DaneelOlivaw1/macos-wechat-export)
(it re-signs only a **temporary copy** of WeChat and launches that copy under LLDB to read the key; the original WeChat and SIP are untouched). Chats are read by this repo's `ethan/wechatexport.py`, following the same method (SQLCipher 4, raw key, read-only), for the full history.

> **Note**: that tool was tested on Apple Silicon + **macOS 15.6.1** + WeChat 4.1.15. Other macOS versions are untested.
> If a step fails, send the exact error from the terminal to your coding agent (errors don't contain the key; **never** share the contents of `keys.json`).

## 1. Every command, from scratch

On your Mac open Terminal and paste the commands one at a time, waiting for each to finish.
Each one carries its own `cd` or absolute path, so it doesn't matter which directory you're in.

### 1. Install Homebrew and tools

Skip the first command if Homebrew is already installed (`brew --version` prints something).

```sh
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

```sh
grep -q 'brew shellenv' ~/.zprofile 2>/dev/null || echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile; eval "$(/opt/homebrew/bin/brew shellenv)"
```

```sh
xcode-select -p || xcode-select --install
```

(If a window offers to install the Command Line Developer Tools, install them, then continue.)

```sh
brew install sqlcipher python@3.14 jq
```

```sh
python3.14 --version && sqlcipher -version
```

### 2. Download the key tool (pinned to a reviewed commit)

```sh
mkdir -p ~/tools && git clone https://github.com/DaneelOlivaw1/macos-wechat-export.git ~/tools/macos-wechat-export
```

```sh
git -C ~/tools/macos-wechat-export checkout 88ca1bef06279f3bc9fa27a379b417f71b6e88a5 && git -C ~/tools/macos-wechat-export rev-parse HEAD
```

(`88ca1bef…` is the commit whose source was reviewed; the last line printed should be exactly that hash.)

```sh
cd ~/tools/macos-wechat-export && sh build.sh && sh test.sh && python3.14 test_db.py
```

`build.sh` / `test.sh` build the tool's message-sending UI, which we don't use; errors there can be ignored as long as `python3.14 test_db.py` passes.

### 3. Pick your WeChat account

```sh
cd ~/tools/macos-wechat-export && python3.14 disk_inventory.py | jq '.accounts[] | {account_index, database_count}'
```

```sh
ls -d ~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/*/db_storage
```

If macOS asks whether Terminal may access data from other apps, click **Allow**. The second command lists one directory per account; if there's only one, that's it. With several, pick the one with the largest `database_count` and the most recent modification time (if unsure, send both outputs to your agent — **don't guess**).
Save the path in a small file only you can read; later commands read it from there:

```sh
mkdir -p ~/.config/ethan && chmod 700 ~/.config/ethan && printf "WX_DB='%s'\nWX_KEYS='%s'\n" "$(ls -d ~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/*/db_storage | head -1)" "$HOME/tools/macos-wechat-export/.wechat-local/keys.json" > ~/.config/ethan/wechat-export.env && chmod 600 ~/.config/ethan/wechat-export.env && cat ~/.config/ethan/wechat-export.env
```

(This takes the first account. With several accounts, edit the `WX_DB=` line in `~/.config/ethan/wechat-export.env`.)

### 4. Quit WeChat and extract the key

Send any drafts, then quit WeChat with **⌘Q** (not just closing the window). Make sure it's gone:

```sh
pgrep -x WeChat && echo "WeChat is still running, press ⌘Q again" || echo "quit"
```

```sh
source ~/.config/ethan/wechat-export.env && cd ~/tools/macos-wechat-export && python3.14 bootstrap_keys.py --db-root "$WX_DB" --state-dir ~/tools/macos-wechat-export/.wechat-local --acknowledge-debug-copy
```

A **temporary WeChat window** opens: click "Enter WeChat" and confirm on your phone if asked. Don't chat in that temporary window.
When the terminal prints `keys_saved_privately`, it closes the temporary copy and reopens the original WeChat (`original_launch_requested` only means it asked; open WeChat yourself if it didn't appear). If you see `bootstrap_error`, stop and send that line to your agent.

> The script refuses to overwrite existing keys. To redo it, use a new `--state-dir` (e.g. `~/tools/macos-wechat-export/.wechat-local-2`) and point `WX_KEYS` in `~/.config/ethan/wechat-export.env` at the new `keys.json`.

### 5. Verify the keys open every database (read-only)

```sh
source ~/.config/ethan/wechat-export.env && cd ~/tools/macos-wechat-export && python3.14 wechat_db.py --db-root "$WX_DB" --key-file "$WX_KEYS" verify | jq '{verified_count, failed: [.databases[] | select(.verified | not) | .database]}'
```

`failed` should be an empty `[]`. If not, send the output to your agent.

### 6. Find your own sender id

In WeChat, send a unique message to **File Transfer**: `BuddyIdCheck1104`. Then:

```sh
source ~/.config/ethan/wechat-export.env && cd ~/tools/macos-wechat-export && python3.14 wechat_db.py --db-root "$WX_DB" --key-file "$WX_KEYS" read --chat-id filehelper --limit 10 | jq -r '.messages[] | select((.text // "") | contains("BuddyIdCheck1104")) | .sender_id'
```

The string it prints (usually starting with `wxid_`) is your sender id. Save it (replace `PASTE_IT_HERE`):

```sh
echo "WX_SELF='PASTE_IT_HERE'" >> ~/.config/ethan/wechat-export.env && cat ~/.config/ethan/wechat-export.env
```

Nothing printed? Wait a few seconds and retry (WeChat writes with a delay). Still nothing: drop the `| jq -r ...` part and check whether the last messages include yours.

### 7. Dry run (writes and sends nothing)

Update your local Personal-OS checkout first:

```sh
cd "$(find ~ -maxdepth 4 -type d -name Personal-OS 2>/dev/null | head -1)" && git checkout main && git pull origin main
```

```sh
source ~/.config/ethan/wechat-export.env && cd "$(find ~ -maxdepth 4 -type d -name Personal-OS 2>/dev/null | head -1)" && python3.14 -m ethan.wechatexport --db-root "$WX_DB" --keys "$WX_KEYS" --self-id "$WX_SELF" --dry-run
```

It prints the number of chats (`chats`), new messages (`new`), messages from you (`her`), and per-chat counts (`per_chat`; `send` says whether that chat would be handed to Buddy).
If `her` is 0 or the output says the `--self-id` can't be found, the id from step 6 is wrong — stop here.
There is also a `labels` line: "读到 N 个标签、M 个联系人有标签" (read N tags, M contacts have tags) means your WeChat contact tags were found and the Relations page will group people by them; "没读到微信标签" (no tags read) doesn't affect the export — the page keeps grouping by importance.

### 8. Export the full history and hand it to Buddy

```sh
source ~/.config/ethan/wechat-export.env && cd "$(find ~ -maxdepth 4 -type d -name Personal-OS 2>/dev/null | head -1)" && python3.14 -m ethan.wechatexport --db-root "$WX_DB" --keys "$WX_KEYS" --self-id "$WX_SELF" --send
```

- Each chat is written to `~/wechat-export/<chat name>.jsonl` (directory readable only by you).
- At most 30 batches (about 12,000 characters each) per run, so the server can finish processing overnight. If the output says the rest will be sent next time, run the same command again **the next day** until it no longer says so (the daily job also continues).
- Only some chats: add `--chats "Alex,Book club"`. Group chats are only sent if you spoke in them; add `--all` for everything; add `--filehelper` to include File Transfer.
- Export without sending: replace `--send` with `--export`.
- `--send` also sends per-person stats for one-on-one chats, used to build person cards (workbench "Relations" page). To skip some people: add `--people-skip "Name1,Name2"` (substring match) — include it when installing the daily job too.
- The stats also carry the tags you gave each contact in WeChat (tag names only, contact details removed); the Relations page groups people by them.

### 9. Run it automatically every day

```sh
source ~/.config/ethan/wechat-export.env && cd "$(find ~ -maxdepth 4 -type d -name Personal-OS 2>/dev/null | head -1)" && python3.14 -m ethan.wechatexport --db-root "$WX_DB" --keys "$WX_KEYS" --self-id "$WX_SELF" --install-launchd
```

Runs every day at 22:43 (incremental export + send; if the Mac is asleep it catches up on wake), logging to `~/.config/ethan/wechatexport.log`. Try it now and read the log:

```sh
launchctl start com.ethan.wechatexport; sleep 60; tail -30 ~/.config/ethan/wechatexport.log
```

If the log says `Operation not permitted`: System Settings → Privacy & Security → Full Disk Access → "+", press ⌘⇧G and enter
`/opt/homebrew/opt/python@3.14/Frameworks/Python.framework/Versions/3.14/Resources/Python.app`, switch it on, and run the command above again.
To remove it: `launchctl unload ~/Library/LaunchAgents/com.ethan.wechatexport.plist`.

## 2. How it works (for future agents)

| Item | Approach |
|---|---|
| Read-only | Each database plus its `-wal` / `-shm` is copied to a temp directory (counted only if size and mtime are unchanged before and after, so WeChat may stay open), and only the copy is opened: preferably with python's `sqlcipher3`, else brew's `sqlcipher -readonly` (key passed on stdin, never on the command line), both with `PRAGMA cipher_compatibility=4` and `PRAGMA query_only=1`; copies are deleted afterwards. Not one byte is written to the WeChat directory (test `test_readonly_and_temp_cleaned`). |
| Keys | The tool's `keys.json`: `{"keys": {"message/message_0.db": {"enc_key": "64 hex chars"}}}`; entries keyed by salt (first 16 bytes of the file) or absolute path are accepted too. Exactly one key per database; the file must be mode 600. |
| Chats | `SessionTable.username` in `session/session.db`; names from `contact` in `contact/contact.db` (remark > nickname > WeChat id). Official accounts `gh_`, service and system accounts (`weixin`, `brandsessionholder`, ...) and folded entries are dropped; File Transfer needs `--filehelper`. |
| Messages | `Msg_<md5(username)>` in `message/message_<n>.db`: `local_id`, `server_id`, `local_type` (low 32 bits kind, high 32 bits subtype), `create_time`, `real_sender_id` (rowid in `Name2Id`), `message_content` (starting with `28 b5 2f fd` means zstd, decoded with python3.14's built-in module). In groups, other people's text is prefixed with `<username>:\n`. Text is kept as is; images, voice, video, stickers, location, contact cards, calls, links, files, red packets, transfers etc. become placeholders such as `[图片]` (image); quote replies keep the reply text. |
| WeChat tags | `contact/contact.db`: tag names in `contact_label` (`label_id_`, `label_name_`, `sort_order_`); a contact's tags are top-level protobuf field 30 of `contact.extra_buffer`, a comma-separated list of tag ids (e.g. `1,5`). Source: the open-source r266-tech/wx-cli and pandorafuture/wx-cli read it this way. Table and column names are probed from candidates (3.x-style `ContactLabel` / `LabelIDList` columns too); if nothing matches it reports "not read", raises nothing, and leaves `labels` out of the stats (the server keeps what it had). |
| Files | `~/wechat-export/<chat name>.jsonl`, one message per line: `{"time", "who", "is_her", "text", "type", "sender", "db", "local_id", "server_id"}`; chats with the same name get a hash of the WeChat id appended. |
| Incremental | `~/.config/ethan/wechatexport-state.json`: per chat and database, the last `local_id`, the database salt, the last `create_time` (to continue by time if a database was rebuilt), and how many lines were delivered. |
| Handing to Buddy | Whole conversations, both sides: one block per day headed 〔date〕, one line per message labelled 【她】 (you) / 【remark name or group member name】. Messages over 2,000 characters are truncated; placeholder-only messages (images, voice) are skipped; phone numbers, emails and addresses are removed with `docquotes.redact`. Chat name "微信 · <chat name>" (WeChat · chat), sent via `cloud.CloudClient.interview` (`format: "transcript"`), at most 12,000 characters per batch, in order within a chat; progress advances only when a batch succeeds. All one-on-one chats are sent; groups only where you spoke, and only those days (`--chats` sends only named chats, `--all` all groups and days). The server processes it in the background right away. The key comes from `ETHAN_CLOUD_TOKEN` or `~/.claude/settings.json` (as in agent-export); without a key only handoff files are written to `~/.config/ethan/wechatexport-out/` (send them with `python3 -m ethan.cloud interview --file`; the server recognises transcripts). |

## 3. Not done / to verify

- Whether key extraction works on macOS versions the tool hasn't been tested on.
- The tag location (`extra_buffer` field 30) comes from open-source tools; to be confirmed by the `labels` line of the first `--dry-run`.
- Group member display names come only from remarks / nicknames in the contact table, not group nicknames (`chat_room.ext_buffer`); members not in the contact table show their WeChat id.
- Filling in "people" and "relationships" from this data needs the first full delivery and a server pass before it can be evaluated.
