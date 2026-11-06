# SETUP-FOR-AGENTS: build your user their own Buddy

English | [简体中文](SETUP-FOR-AGENTS.zh-CN.md)

You are the user's AI agent. This repo is a companion AI (default name Buddy — 小伴 in the Chinese UI — and the user will rename it) plus a "life as a game" workbench. Your job is to install it on the user's own machine and make it theirs. Work through the steps in order; after each step run its **Check** and only move on when it passes.

Term names follow [docs/glossary.md](docs/glossary.md).

## Rules

- Never ask the user to paste secrets (model keys, bot tokens, any token) into the chat, and never commit them. The user runs `python3 -m ethan.setup_secrets` on the server and types them in themselves.
- Talk to the user in their language, in plain words, one question at a time. If they say "you decide", use the defaults given here.
- The UI (workbench, chat messages, buttons) is Chinese-only for now. Tell non-Chinese-speaking users this up front; Buddy will still chat in their language. UI i18n is on the roadmap — do not start translating the code as part of setup.
- After any code change run `python3 -m unittest discover -s tests`. Done means all green.
- The user's personal content (goals, body data, persona details) lives only on their own server and data directory. Never commit it to a public repo.

## Step 1: Ask three things

1. **Where it runs.** A server is optional — any computer that stays on works (Linux is easiest, Windows via WSL; on a Mac swap the systemd timers for launchd).
   Trade-offs of running at home: while the computer sleeps, Buddy won't message first and there's no morning plan. Opening the workbench from a phone, or receiving inbound data (iPhone location, bank emails), needs a free Cloudflare Tunnel.
   WeChat-only with an always-on computer works fine locally. For zero hassle, rent the cheapest cloud server (2 vCPU / 2 GB).
2. **Which model.** Default: DeepSeek (cheap, reachable from mainland China). Any OpenAI-compatible API works. A Claude subscription via Claude Code works too.
3. **Which chat app.** Default: WeChat (official plugin; text, voice and images both ways. Limitation: no push — Buddy can only reply after the user speaks). Telegram is the alternative where it's reachable, and it supports proactive messages.

## Step 1.5: Cold start from the user's goals spreadsheet, if they have one

Ask for the spreadsheet (Excel, Feishu, Notion, a screenshot — anything). Split it with the rules below, show the user the result, then write it in. No need to go item by item.

| In the spreadsheet | Becomes | Rule |
|---|---|---|
| The things they most want done in the next three months, with deadlines | Quests (season goals), at most 5 | Each has a clear "done" (a number or an outcome); mark the most important as the main quest |
| Each quest | Questline steps | Don't split it yourself: the planner breaks each new quest into 6–20 steps the next morning for the user to review |
| Things to keep doing every day or week (training, early nights, reading) | Dailies / weeklies | 3–7 dailies and 1–2 weeklies; more than that and people give up |
| Lines they don't want to cross (weight, spending, sleep, keeping in touch) | Territory baselines | 1–3 per territory, phrased as "at least N per week" / "at most N per month" |
| Loose to-dos | Side tasks | Add as-is; Buddy guesses the territory from the title |
| Research, price comparisons, drafts | Buddy preps it | Buddy does it first and hands over the result |
| Long-term wishes without a deadline | Candidate quests / wishes | Keep out of this season, park in the candidate pool |

Write it in with `python3 -m ethan.goals_import --data <data dir> <your goals.json>`. The format is in `examples/goals.json`; fields are documented at the top of `ethan/goals_import.py`.

## Step 2: Install

1. On the server create an `ethan` user, put the repo at `/home/ethan/Personal-OS`, and run `deploy/install.sh`.
2. Have the user run `python3 -m ethan.setup_secrets` and enter only what Step 1 needs; press Enter to skip the rest. The template is `deploy/ethan.env.example`, which explains every entry.
3. Connect the chat app following [docs/en/operations/deploy.md](docs/en/operations/deploy.md) (scan a WeChat QR code, or send a Telegram pairing code).

**Check:** the user sends "hi" in the chat app and Buddy answers; `systemctl list-timers | grep ethan` lists the scheduled jobs.

## Step 3: Cold start (these must be the user's own — ask one by one)

| What | Ask the user | Where it goes |
|---|---|---|
| Who their AI is | A name (always ask — don't keep the sample name unless they want it); what kind of companion: partner, best friend, mentor, buddy? What voice? | `persona/ethan.md` (persona), `persona/script-guide.md` (its own daily life) |
| This season's quests | The 3–5 things they most want done in three months, and what "done" means for each | Let them tell Buddy in chat; the planner breaks them into steps for review. Or write them following `ethan/goals2026.py` |
| Dailies and weeklies | 3–7 small things every day, 1–2 every week | `DEFAULT_HABITS` in `ethan/quests.py`, or tell Buddy "add a habit: ..." |
| Territory baselines | What "passing" looks like per territory (e.g. train 3×/week, spend under X/month) | `ethan/territory.py` |
| Body and food | Height, weight, goal, available equipment, foods to avoid | `playbooks/body/`, `playbooks/food/` |
| Location and time zone | Home city, places they go often | `ETHAN_TZ` (e.g. `America/New_York`) and `ETHAN_WEATHER_LATLON` in `ethan.env`; replace `Europe/London` in `deploy/systemd/*.timer` with the same zone; frequent places: tell Buddy |
| Starting point for money, social, learning | Don't ask now — Buddy follows the cold-start flow in `ethan/domains.py` and talks through each area for 10–15 minutes at a good moment | Automatic |

**Check:** the workbench (`ethan/workbench/`) home page shows the user's quests, dailies and today's plan.

## Step 4: Change the world skin (optional — ask first)

The default skin is "Earth Online": life is a game; play seasons, run quests, guard territories, earn points for surprises. To change it:

| Change | Where |
|---|---|
| Game name, tagline, the day and night avatars | Home section of `ethan/workbench/static/app.js` |
| How many territories and their names | `TERRITORIES` in `ethan/territory.py` |
| World map | `ethan/workbench/static/map-day.jpg`, `map-night.jpg` (an image model can draw them) |
| Level titles | `TITLES` in `ethan/quests.py` |
| Achievements and hidden achievements | `ethan/achievements.py` (never reveal hidden conditions — give only the riddle) |
| Shop surprise tiers | `DEFAULT_REWARDS` in `ethan/quests.py` |
| Surprises unlocked by achievements | `ethan/surprises.py` (no spoilers either) |
| Today card art style | `STYLE`, `SCENES` in `ethan/cards.py` |

## Step 5: Alternatives and optional plug-ins (add only what the user needs)

| Part | Default | Alternative / if skipped | Where |
|---|---|---|---|
| Main model | Chosen in Step 1 | Any OpenAI-compatible API: DeepSeek, Qwen, Kimi, Zhipu, ... | `ETHAN_LLM_BASE_URL` (DeepSeek: `https://api.deepseek.com`), `ETHAN_LLM_API_KEY`, `ETHAN_LLM_MODEL` (DeepSeek: `deepseek-chat`) in `ethan.env`; leave all three empty to use a Claude Code subscription |
| Image generation (Today card, photos) | Jimeng / Grok Imagine / Gemini | Skipped: no Today card image, no photos | `ethan/photos.py`, `ethan/cards.py` |
| Voice replies | MiniMax | Skipped: text only | `ethan/core/tts.py` |
| Speech-to-text | Local faster-whisper | Nothing to change | `ethan/channels/media.py` |
| Web errands | Grok Bot | Skipped: Buddy says plainly it can't do it | `ethan/grok.py` |
| Calendar | Apple Calendar | Skipped: plans still work | `ethan/icloud.py` |
| Spending | Bank emails, Monzo | Skipped is fine: forward spending notifications to Buddy and it logs them | `ethan/finance.py`, [docs/en/operations/finance.md](docs/en/operations/finance.md) |
| Going out, work hours | iPhone Shortcuts, a small Mac agent | Skipped: those check-ins show "waiting for data" | [docs/en/operations/devices.md](docs/en/operations/devices.md) |

## Step 6: Hand over

Tell the user three things: where to chat (the chat app), where to look (the workbench URL), and where to look when something breaks (the "System checkup" in the workbench settings). Maintenance is automatic after that: auto-deploy every 2 minutes, and an encrypted memory backup every night ([docs/en/operations/rebirth.md](docs/en/operations/rebirth.md)).
