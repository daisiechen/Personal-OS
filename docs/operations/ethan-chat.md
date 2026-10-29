# 在 Claude App 里和 小伴 聊（2026-10-29 她要）

一个固定的 Claude Code 会话当 小伴 的第三个聊天窗口，和 Telegram、微信共享同一份实时记忆：她在这里说的话走和 Telegram 一样的处理（`ethan/chat.py`），记进同一份原始对话（channel 记成 `claude`），小伴 用同样的人设、记忆和任务系统回她，回复里的标记照样执行；交给 Grok 的事办完在 Telegram 转告。

这个会话本身不是开发助手：它只按 `deploy/ethan-chat/CLAUDE.md` 把她每条消息原样交给 `scripts/ethan-chat.sh`，再把 小伴 的回复原样给她，权限也只放了这一条命令（`deploy/ethan-chat/.claude/settings.json`）。目录放在仓库外面，免得读到开发会话的规矩。

## 装一次（服务器上，ethan 用户）

1. 服务器会话或她本人复制目录：`mkdir -p ~/ethan-chat && cp -r ~/Personal-OS/deploy/ethan-chat/. ~/ethan-chat/`
2. 她本人在 Mac 终端开一个常驻会话（和开发用的 `claude` 那个 tmux 分开）：
   `ssh -t my-server "sudo -H -u ethan bash"` → `tmux new -s ethan-chat` → `cd ~/ethan-chat && claude remote-control`
3. Claude App 里会多出这个会话，改个名叫“小伴”，以后就在这里聊。

改了 `deploy/ethan-chat/` 里的文件，重新做第 1 步再在会话里 `/clear` 一下就生效。服务器重启后要重做第 2 步（和开发会话一样，待改成开机自启）。
