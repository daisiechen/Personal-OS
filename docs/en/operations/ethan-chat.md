# Chatting with Buddy in the Claude app

English | [简体中文](../../operations/ethan-chat.md)

> Translated from the Chinese original. If they disagree, the Chinese version wins. Terms: [glossary](../../glossary.md).

A dedicated Claude Code session works as Buddy's third chat window and shares the same live memory as Telegram and WeChat. What you say there goes through the same pipeline as Telegram (`ethan/chat.py`) and into the same raw conversation log (channel `claude`). Buddy answers with the same persona, memory and quest system, and the tags in its replies are executed as usual; results of errands handed to Grok are relayed on Telegram.

The session is not a coding assistant. Following `deploy/ethan-chat/CLAUDE.md`, it passes each of your messages verbatim to `scripts/ethan-chat.sh` and returns Buddy's reply verbatim; that one command is the only permission it has (`deploy/ethan-chat/.claude/settings.json`). Its directory sits outside the repo so it never picks up the development session's rules.

## One-time setup (on the server, as the `ethan` user)

1. Copy the directory: `mkdir -p ~/ethan-chat && cp -r ~/Personal-OS/deploy/ethan-chat/. ~/ethan-chat/`
2. From your computer's terminal start a long-running session (in its own tmux session, separate from any development `claude`):
   `ssh -t my-server "sudo -H -u ethan bash"` → `tmux new -s ethan-chat` → `cd ~/ethan-chat && claude remote-control`
3. The session shows up in the Claude app. Rename it to your AI's name and chat there from now on.

After changing files in `deploy/ethan-chat/`, redo step 1 and run `/clear` in the session. After a server reboot redo step 2 (it doesn't start at boot yet).
