# Exporting memory and chats from other agents

English | [简体中文](../../operations/agent-export.md)

> Translated from the Chinese original. If they disagree, the Chinese version wins. Terms: [glossary](../../glossary.md).

Why: so that losing an account, or switching agents, doesn't lose context — and because what you say to other agents is still you talking. Those words belong in Buddy's memory as your verbatim quotes.

## What you do (once)

On your Mac, in the Personal-OS directory:

```sh
python3 -m ethan.agentexport --install-launchd
```

It prints and installs the schedule (`~/Library/LaunchAgents/com.ethan.agentexport.plist`): every Sunday at 20:13 it runs `--codex --send`, logging to `~/.config/ethan/agentexport.log`. To remove it: `launchctl unload ~/Library/LaunchAgents/com.ethan.agentexport.plist`.

To preview how much would be sent: `python3 -m ethan.agentexport --codex --dry-run` (no archive, nothing sent).

## Agents

| Agent | Where | Format | How often | How |
|---|---|---|---|---|
| Codex | `~/.codex/AGENTS.md`, `~/.codex/memories/` (if present), `~/.codex/sessions/**/rollout-*.jsonl` (and `archived_sessions/`) | Sessions are JSONL, one event per line. New: `{"type":"response_item","payload":{"type":"message","role":"user",...}}`; old: `{"type":"message","role":"user",...}`. Both are read; unknown lines are skipped | Sundays 20:13, automatic | `python3 -m ethan.agentexport --codex --send` |
| Grok Bot app chats | No local files; at <https://accounts.x.ai/data> click "Download account data" and xAI emails you a download link | zip of JSON (format not fixed, parsed loosely) | Monthly (a workbench to-do reminds you) | Picked up automatically when the server reads mail — see below |
| WorkBuddy, Trae, etc. | Unknown yet | Unknown yet | — | Not done yet |

## What each Codex run does

1. **Archives the originals**: new or changed files (AGENTS.md, memories, session logs — never `auth.json`, `config.toml` or anything else with keys) are packed into `~/.config/ethan/agent-archive/codex-<date>-<time>.tar.gz`, in a directory only you can read. If `~/.config/ethan/agent-archive.key` exists, it's encrypted to `.tar.gz.enc` the same way as the memory backup (`ethan/backup.py`). Never in this repo.
2. **Hands whole conversations to Buddy**: both sides, one line per turn marked 【她】 (you) / 【Codex】, headed 〔Codex 会话 date〕 (Codex session). From your side it strips blocks Codex injects (`<environment_context>`, `<user_instructions>`, lines starting `# AGENTS.md instructions`), acknowledgements under 8 characters, pure code / commands / paths, and truncates long pastes at 2,000 characters. From Codex's side code blocks become "[code omitted]", pure code is dropped, and turns are cut at 500 characters. Emails and phone numbers are removed (same as `docquotes.redact`). It goes through the interview channel (`format: "transcript"`). When the server processes it, what you said counts as verbatim quotes; Codex's suggestions and conclusions are stored as agent paraphrase (reference only).
3. **Progress**: each session file is fingerprinted by path + size + mtime in `~/.config/ethan/agentexport-state.json`, recorded only after the whole session was delivered; when a session continues, only new turns are sent.
4. **Key**: environment variable `ETHAN_CLOUD_TOKEN`; scheduled runs have no terminal environment, so they read the check-in hook key from `~/.claude/settings.json` (installed by `python3 -m ethan.setup_hook --where mac`). With neither, it only writes handoff files to `~/.config/ethan/agentexport-out/` and sends nothing.

## Grok Bot app (automatic on the server, `ethan/grokexport.py`)

You shouldn't have to drag files around when the export already lands in your inbox.

**What you do**: one click a month. The first nightly run each month adds a workbench to-do: go to https://accounts.x.ai/data and click Download account data — the email will be handled automatically. It's skipped if it was already added this month, you deleted this month's item, or an import happened in the last 20 days.

**When the email arrives** (`ethan/mail.py` checks for it first):
1. **Recognise**: sender is x.ai / xAI, subject or opening mentions data export / download / archive, and the body has **an https link on the x.ai domain** (other domains, http, or look-alikes such as `x.ai.evil.com` are rejected; download / export / archive links rank first, data-only links last, privacy-policy footers ignored; at most 3). For HTML-only emails, `imapmail.parse` moves the x.ai links from `href`s to the top of the text so they survive tag stripping. In the workbench mail list it shows as FYI ("Buddy is downloading and importing this, nothing for you to do"), **not as something you must handle**, and is archived normally.
2. **Download** (in the background, `python3 -m ethan.grokexport --data <dir> --jobs`, started by the mail job when downloads are queued): plain urllib, **without any of your login credentials**; 500 MB max, 120-second timeout. Outcomes:
   - zip / JSON → import (next step); the email note becomes "imported (N passages of what you said)";
   - an HTML page (login) / 401 / 403 → no forcing: the email becomes **needs you** (moved back to the inbox if archived) with "xAI requires login to download: open this link, then send the file to Buddy as-is (on Telegram just send the zip)";
   - too large → needs you: "download it, unzip, and send the JSON inside";
   - network error → retry in half an hour, needs you after 3 failures.
3. **Sending the file yourself works too**: zips sent on Telegram / WeChat are saved to `media/` (scanned in the background right away, and every 30 minutes). If it's a Grok export (file names inside contain "grok", or the JSON mentions grok) it's imported and any earlier "needs login" item is cleared; other archives and plain JSON are left alone. On the server: `python3 -m ethan.grokexport --data /home/ethan/data --file export.zip`. Telegram bots can only receive files up to 20 MB.
4. **Import**: reads `.json` / `.jsonl` from the zip in memory only (at most 300 MB uncompressed), never extracting to disk. Loose parsing: walks every object; an array named messages / responses / conversation / chat_messages / turns where more than half the items have a recognisable speaker and text counts as a conversation (messages may be wrapped in response / message; speaker from sender / role / author, text from message / content / text; time as ISO, seconds, milliseconds or `{"$date": …}`). Sender human / user is you. Whole conversations, both sides: lines marked 【她】 (you) / 【Grok】, headed 〔Grok Bot App 会话 date "title"〕; Grok's turns aren't shortened (single messages over 2,000 characters are truncated); your turns are filtered like Codex (`agentexport.clean`); emails and phone numbers removed. It's written **directly on the server** as an interview record (`interviews.receive`, `format: "transcript"`, no HTTP) and processed in the background: your words are verbatim quotes, Grok's are agent paraphrase. Every message is fingerprinted, so re-imports after continued chats only send new turns.
5. **Archive**: the original package is kept as `<data dir>/agent-archive/grok-<date>-<time>.zip` (directory 700, file 600, never in this repo), encrypted to `.enc` the same way if `~/.config/ethan/agent-archive.key` or the backup `backup.key` exists.
6. **De-duplication**: the same package (content hash) is never imported twice; each of your messages is fingerprinted (conversation id + message id or time + text, table `grok_seen`); a conversation is recorded only after it was delivered in full.
7. **Source health**: "Grok Bot app export" is only reported as a gap after 45 days without a new one.

Tables: `grok_jobs` (downloads queued from mail), `grok_files` (packages seen), `grok_seen` (messages delivered), `grok_meta` (monthly reminder, last import).

## Not done yet

- Pushing the encrypted archives to a separate private repo (like `ethan-vault`); for now they stay on the Mac.
- Where WorkBuddy and Trae keep their data, and in what format.
- The real Grok export format hasn't been seen yet; after the first import, check the server log for how many conversations were recognised and extend the parser if needed.
