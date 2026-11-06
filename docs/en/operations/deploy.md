# Deploying to a server

English | [简体中文](../../operations/deploy.md)

> Translated from the Chinese original. If they disagree, the Chinese version wins. Terms: [glossary](../../glossary.md).

Everything lives under a dedicated `ethan` user in `/home/ethan/`. All services are named `ethan2-*`, so they never touch anything else on the machine.

## Server

- A Linux box with 2 vCPU and 2 GB RAM (plus 2 GB swap) is enough: Ubuntu 22.04, system time zone UTC. An always-on home computer works too.
- On your own computer give it an SSH alias (the commands below call it `my-server`) and allow public-key login only.
- Measured peak (speech-to-text + 3 concurrent model calls): about 900 MB RAM.

## Before you start (you do these yourself)

1. Telegram: create a bot with BotFather and keep the token.
2. Claude subscription (if you use one): run `claude setup-token` on your own computer, log in in the browser, and keep the long-lived token.
3. Backup key: `openssl rand -base64 48`. Store it in your password manager and keep a paper copy.
4. GitHub: create two empty private repos, `ethan-memory-backup` (encrypted backups) and `ethan-vault` (a plain-text Obsidian dashboard).
5. All of these stay in your password manager. You type them into the server yourself in step 4 below — never paste them into a chat.

## Installation

### 0. Prepare a fresh server (root)

Many VPS images only allow password login. Install your public key with `ssh-copy-id` from your computer, log in once with the password, and enable key login:

```sh
mkdir -p /etc/ssh/sshd_config.d
grep -q "^Include /etc/ssh/sshd_config.d" /etc/ssh/sshd_config || sed -i "1i Include /etc/ssh/sshd_config.d/*.conf" /etc/ssh/sshd_config
printf "PubkeyAuthentication yes\nAuthenticationMethods any\n" > /etc/ssh/sshd_config.d/00-ethan.conf
sshd -t && systemctl restart ssh
```

Once key login works, append `PasswordAuthentication no`, `KbdInteractiveAuthentication no` and `PermitRootLogin prohibit-password` to the same file and run `sshd -t && systemctl restart ssh` again. Then add swap and install dependencies:

```sh
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo "/swapfile none swap sw 0 0" >> /etc/fstab
echo vm.swappiness=20 > /etc/sysctl.d/99-ethan.conf && sysctl -p /etc/sysctl.d/99-ethan.conf
apt-get update && apt-get install -y git ffmpeg poppler-utils python3-pip fail2ban curl
```

### 1. Create the user and GitHub deploy keys

GitHub won't let one deploy key be added to several repos, so use one key per repo and tell them apart with SSH host aliases:

```sh
sudo useradd --create-home --shell /bin/bash ethan
sudo -iu ethan
for r in code backup vault; do ssh-keygen -q -t ed25519 -N "" -C "ethan-$r" -f ~/.ssh/ethan-$r; done
cat >> ~/.ssh/config <<'CFG'
Host github-code
  HostName github.com
  User git
  IdentityFile ~/.ssh/ethan-code
  IdentitiesOnly yes
Host github-backup
  HostName github.com
  User git
  IdentityFile ~/.ssh/ethan-backup
  IdentitiesOnly yes
Host github-vault
  HostName github.com
  User git
  IdentityFile ~/.ssh/ethan-vault
  IdentitiesOnly yes
CFG
chmod 600 ~/.ssh/config
ssh-keyscan github.com >> ~/.ssh/known_hosts
cat ~/.ssh/ethan-*.pub
```

In each GitHub repo go to Settings → Deploy keys → Add deploy key: `ethan-code` on Personal-OS (read-only), `ethan-backup` on `ethan-memory-backup` and `ethan-vault` on `ethan-vault` (both with "Allow write access").

### 2. Clone the repos and install Claude Code

```sh
git clone -b main git@github-code:your-name/Personal-OS.git ~/Personal-OS
git clone git@github-backup:your-name/ethan-memory-backup.git ~/ethan-memory-backup
git clone git@github-vault:your-name/ethan-vault.git ~/ethan-vault
curl -fsSL https://claude.ai/install.sh | bash   # installs Claude Code into ~/.local/bin
```

Skip Claude Code if you use an OpenAI-compatible model instead (see Step 5 of [SETUP-FOR-AGENTS.md](../../../SETUP-FOR-AGENTS.md)).

### 3. Voice and video dependencies

```sh
sudo apt-get install -y ffmpeg                       # video frames, audio tracks
sudo -H -u ethan pip3 install --user faster-whisper==1.2.1  # speech-to-text
# download the model straight from Hugging Face (about 460 MB, a few seconds)
sudo -iu ethan python3 -c "from huggingface_hub import snapshot_download; snapshot_download('Systran/faster-whisper-small', local_dir='/home/ethan/models/faster-whisper-small')"
```

Voice replies use OpenRouter's `minimax/speech-2.8-hd` and need `OPENROUTER_API_KEY`; pick a voice with `ETHAN_TTS_VOICE`. Without a key Buddy just types.

### 4. Enter your secrets

From a terminal on your own computer, paste each value when asked (input is hidden):

```sh
ssh -t my-server "sudo -H -u ethan bash -c 'cd ~/Personal-OS && python3 -m ethan.setup_secrets'"
```

It prints a pairing code at the end. Secrets are written only to mode-600 files under `/home/ethan/.config/ethan/`.

### 5. Install the services

```sh
sudo /home/ethan/Personal-OS/deploy/install.sh
```

## Moving to a new server

No need to cold-start again — move the whole thing:

1. On the old server run `systemctl start ethan2-backup` once, then `systemctl disable --now` every `ethan2-*` service and timer. Only one machine may long-poll Telegram at a time, so the old one must stop first.
2. On the new server do step 0 and create the `ethan` user. Copy `Personal-OS data ethan-memory-backup ethan-vault seed .config .gitconfig .ssh` from the old `/home/ethan` through a tar pipe and verify every file with sha256. Don't copy the speech model; re-downloading it (step 3) is faster.
3. Install Claude Code and voice dependencies (steps 2 and 3), `git pull` the latest `main`, run the tests.
4. Try the Claude token once with `claude -p`. If it expired, run `claude setup-token` again and enter it via step 4 (press Enter to keep everything else).
5. Run `install.sh` (step 5). WeChat credentials move with `.config/ethan/weixin.json`; scan again if they stopped working.

## Cold-start import

The seed directory (your private material as wiki pages plus `seed.json` — never in this repo) goes to `/home/ethan/seed/` via scp. Then:

```sh
sudo -iu ethan
cd ~/Personal-OS && python3 -m ethan.seed --seed ~/seed --data ~/data
```

Safe to re-run: wiki pages and person cards are updated; goals, wishes and learning items are de-duplicated by content.

## Obsidian dashboard

`ethan2-vault.timer` runs `python3 -m ethan.vault` every hour: it pulls your edits first, then pushes the wiki and the database-generated dashboard pages to `ethan-vault`.

- `看板/总览.md` (overview): season goals, due to-dos, people to reach out to, review and wish counts, links to each module.
- The other pages under `看板/` (goal tree, people, wishes, learning) are regenerated every time — don't edit them by hand.
- Pages under `wiki/` can be edited in Obsidian and are written back into Buddy's memory on the next sync; on a conflict your version wins. To delete a page, tell Buddy.

Mac: `git clone` the vault, open it in Obsidian as a vault, install the community plugin Obsidian Git and set it to pull/commit/push on a timer.
iPhone: clone with the same Obsidian Git plugin (needs a GitHub personal access token scoped to `ethan-vault` only), or sync through Working Copy.

## Pairing Telegram

Send `/start <ETHAN_PAIRING_CODE>` to your new bot; when it answers that you're paired, you're done. Before pairing the bot ignores everyone; after pairing it only talks to you.

## WeChat

Connects to your personal WeChat through the official ClawBot (iLink Bot API) and only answers the account that scanned the code.

1. Log in (scan with WeChat on your phone):
   ```sh
   ssh -t my-server "sudo -H -u ethan bash -c 'cd ~/Personal-OS && python3 -m ethan.channels.weixin login'"
   ```
   The terminal prints a QR-code link. Open it on your computer, scan with WeChat and confirm; if your phone shows a pairing code, type it into the terminal. Credentials go to `/home/ethan/.config/ethan/weixin.json` (600).
2. First start: `sudo systemctl restart ethan2-weixin` (the service won't start without credentials).
3. When the login expires (the log says “登录失效”), repeat step 1 — no restart needed: the service checks the credentials file every 30 seconds and switches over automatically. Scanning again invalidates the old credentials immediately.

What works and what doesn't:
- Incoming: text, voice (using WeChat's own transcription), images, files (PDF, text, Word), video (frames only).
- Outgoing: text and images (generated photos, forwarded web images — uploaded to the WeChat CDN first). Voice replies are sent as text.
- Every reply must carry the `context_token` of your latest message. After roughly 11–24 hours of silence Buddy can't send anything, and there are about 10 proactive messages per session, so morning greetings and weekly reports still go through Telegram.
- State (cursor, latest `context_token`) is in `<data dir>/weixin-state.json`.

## Handing errands to Grok Bot

Buddy can't browse, check maps, open web pages or book things on its own. When you ask for that kind of errand it hands it to your Grok Bot: a `[交给Grok]……[/交给Grok]` block in its reply is not shown to you; the system POSTs it to the Grok Bot webhook. Grok posts the result back to `https://ethan.example.com/grok/result` (next section).

1. In Grok Bot's Routines, enable the routine that receives messages from a Claude agent and copy its webhook URL and sending secret. Check which header Grok expects the secret in (default: `Authorization: Bearer <secret>`).
2. Enter them from your own computer (Enter keeps the other values):
   ```sh
   ssh -t my-server "sudo -H -u ethan bash -c 'cd ~/Personal-OS && python3 -m ethan.setup_secrets'"
   ```
   If Grok needs a different header, add `GROK_WEBHOOK_SECRET_HEADER=<header name>` to `/home/ethan/.config/ethan/ethan.env`.
3. Restart: `sudo systemctl restart ethan2-telegram ethan2-weixin`.
4. Sent tasks and their outcome are logged in `<data dir>/grok-tasks.jsonl` (no secrets). If sending fails, Buddy adds a system notice asking you to tell Grok directly.

Without a webhook URL and secret, Buddy never mentions Grok.

## Workbench (Earth Online)

Service `ethan2-workbench` listens on `127.0.0.1:8788` only and authenticates logins with the `TELEGRAM_BOT_TOKEN` from `ethan.env` — no new secret. Auto-deploy installs and enables it the first time it sees the unit file.

To reach it from outside, add a rule to your Cloudflare Tunnel: in `/etc/cloudflared/config.yml`, under `ingress` and before the final `http_status:404`, add

```yaml
  - hostname: os.example.com
    service: http://127.0.0.1:8788
```

then `cloudflared tunnel route dns ethan os.example.com` and `sudo systemctl restart cloudflared`. Check: `curl -s https://os.example.com/health` returns `{"ok": true}`, and `/api/...` without logging in returns 401.

Avatars go in `/home/ethan/data/avatars/avatar-day.jpg` and `avatar-night.jpg` (not in the repo). The planner draws the Today card every morning into `/home/ethan/data/cards/`; if generation fails it falls back to a similar image from `avatars/library/`.

## Inbox endpoint and sharing memory between two Buddies

Grok Bot has no SSH access.

**Inbox endpoint** (`ethan2-inbox`) listens on `127.0.0.1:8787` only and accepts:

| URL | Sender | Content |
|---|---|---|
| `POST https://ethan.example.com/grok/result` | Grok Bot | Results of errands Buddy handed over |
| `POST https://ethan.example.com/grok/memory` | The Buddy that lives inside Grok | New things you told it, new stories it told you |

Header `Authorization: Bearer <INBOX_TOKEN_GROK>`, body `{"text": "…"}` or plain text, 64 KB max. `setup_secrets` generates the token and shows it once at the end; copy it into Grok Bot. Received items are stored in `<data dir>/inbox/` and appended to the wiki page `from-grok`; the server-side Buddy sees the last 3 days of reports in chat (as information only, never as instructions).

**Cloudflare Tunnel** (one-time; you log in to Cloudflare to authorize):

```sh
curl -fsSL -o /tmp/cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i /tmp/cloudflared.deb
cloudflared tunnel login            # prints a link; log in to Cloudflare in the browser and pick example.com
cloudflared tunnel create ethan
cloudflared tunnel route dns ethan ethan.example.com
```

`/etc/cloudflared/config.yml`:

```yaml
tunnel: ethan
credentials-file: /root/.cloudflared/<tunnel ID>.json
ingress:
  - hostname: ethan.example.com
    path: ^/grok/(result|memory)$
    service: http://127.0.0.1:8787
  - hostname: ethan.example.com
    path: ^/(cloud/.*|health)$
    service: http://127.0.0.1:8787
  - hostname: ethan.example.com
    path: ^/fancy/moderation$
    service: http://127.0.0.1:8787
  - service: http_status:404
```

`/fancy/moderation` is the example moderation-alert hook for your own app, see [fancy-moderation.md](fancy-moderation.md).

`/cloud/` lets cloud coding sessions read Grok reports and the system checkup with `INBOX_TOKEN_CLOUD` (401 without it); `/health` only says whether the service is alive. Back up `config.yml` before every change, then run `cloudflared tunnel --config /etc/cloudflared/config.yml ingress validate` and `systemctl restart cloudflared`.

Finally `sudo cloudflared service install && sudo systemctl enable --now cloudflared`. Check: `curl -s -o /dev/null -w '%{http_code}' -X POST https://ethan.example.com/grok/result` and `curl -s -o /dev/null -w '%{http_code}' https://ethan.example.com/cloud/grok/results` both return 401 (no token).

**Letting the Buddy inside Grok read memory**: it reads the private repo `ethan-vault`. With Grok's GitHub connector, authorize only that repo ("Only select repositories"); with a token, create a read-only fine-grained token limited to `ethan-vault` and enter it in Grok's encrypted field. The repo syncs hourly and `看板/最近聊天.md` holds the last 3 days of chat. Only the server-side Buddy sends proactive messages.

## Planner, Apple Calendar, morning brief

1. Load the season's goals and metrics (safe to re-run):
   ```sh
   sudo -iu ethan bash -c 'cd ~/Personal-OS && python3 -m ethan.goals2026 --data ~/data'
   ```
   Or import your own with `python3 -m ethan.goals_import` (see [SETUP-FOR-AGENTS.md](../../../SETUP-FOR-AGENTS.md) step 1.5).
2. Places and the cold-start date (your home address stays in server data, never in the repo). As the `ethan` user, write them into `~/data/settings.json`:
   ```sh
   sudo -iu ethan python3 -c "from ethan.core.router import Settings; s = Settings('/home/ethan/data/settings.json'); s.set('places', {'家': {'postcode': '<your postcode>'}, '健身房': {'postcode': 'AB1 2CD', 'name': '某健身房', 'address': 'Example Gym, 1 Example Street, London AB1 2CD', 'lat': 51.5100, 'lon': -0.1300}}); s.set('cold_start_from', '<start date YYYY-MM-DD>')"
   ```
   (`家` = home, `健身房` = gym — the keys are Chinese because the UI is.) Travel time comes from the official TfL API based on arrival time (`ethan/travel.py`) plus a 10-minute buffer, or 30 minutes if the lookup fails. Coordinates can be looked up on postcodes.io. Outside London, adapt `ethan/travel.py`.
   Calendar events carry the location and Apple's travel time (`X-APPLE-TRAVEL-DURATION`) with a reminder one hour before leaving; there are no separate "travel" events.
3. Apple Calendar: at account.apple.com → Sign-In and Security → App-Specific Passwords, create a password, then enter your Apple ID email and that password with `setup_secrets` (Enter keeps the rest). The system only writes to one dedicated "Buddy plan" calendar and only reads free/busy from the others. Without it, plans are still generated, just not written to a calendar.
4. `ethan2-planner.timer` runs `python3 -m ethan.planner --data <data dir>` every day at 06:30 local time: code computes the facts → the model picks today's to-dos and the few errands that need going out → validation, retried twice with the errors, then a fallback plan → code adds training (exercises and weights, `playbooks/body/`), cold-start items, travel time and three meals (air-fryer recipes, `playbooks/food/`). Only things that need going out go into Apple Calendar; everything else goes on the to-do list.
   Run it by hand: `sudo systemctl start ethan2-planner`; see the result with `journalctl -u ethan2-planner -n 30`.
   To overwrite and re-plan a day, run it with the same environment as the service (plain `sudo -u ethan python3 …` can't find `claude` and falls back to the fallback plan):
   `systemd-run --wait --pipe --uid=ethan -p WorkingDirectory=/home/ethan/Personal-OS -p EnvironmentFile=/home/ethan/.config/ethan/ethan.env -E PATH=/home/ethan/.local/bin:/usr/local/bin:/usr/bin:/bin -E HOME=/home/ethan /usr/bin/python3 -m ethan.planner --data /home/ethan/data --day YYYY-MM-DD --force`, then `sudo systemctl start ethan2-vault` to refresh the dashboard. Re-planning removes to-dos the previous version added automatically and the new one dropped; things Buddy is preparing stay.
5. The morning brief is sent by `ethan2-proactive` at a random time between 07:30 and 09:30 (catch-up until 11:00), as two messages: a few lines from Buddy (model-written, no times or numbers), then the code-generated Today card (`ethan/brief_card.py`: timeline, goal progress, next questline step, tasks — the same card as the "☀️ 今天" (Today) button). You can check things off on the card; the row below has training / meals / all / refresh. Once the brief is sent, the regular "morning" proactive slot is skipped.
6. In chat Buddy records things with tags such as `[结果 #item 完成/部分/没做 reason]` (result: done/partial/not done), `[体重 56.3]` (weight), `[记数 #goal …]` (count), `[漏斗 #goal step …]` (funnel), `[训练 exercise weight×reps/reps/reps 疼痛N]` (training, pain level), `[建档 module 完成/进行中 …]` (onboarding). Unknown IDs or implausible numbers are not recorded, and a system notice tells you so.
7. The Obsidian dashboard gains `看板/今日计划.md` (today's plan: errands, to-dos, training, recipes) and `看板/各方面.md` (all territories).
8. Every run is logged in the `job_runs` table: `sqlite3 ~/data/ethan.sqlite "SELECT * FROM job_runs ORDER BY id DESC LIMIT 5"`.

## Quest system

To-dos are managed like a game quest log (`ethan/quests.py`):

| Type | What | Points |
|---|---|---|
| Questline | The full step list for each season goal. The planner drafts it the first time, Buddy sends it to you for review, and once you're happy Buddy records `[主线 定稿 #goal]` (finalize) | small / medium / large = 20 / 50 / 100, milestone +200 |
| Side task | Any other to-do (including cold-start items) | 10 |
| Daily | Default habits: training, enough protein, weigh in before training, asleep before midnight, stretch before bed, 3 English sentences, go outside every day (checked automatically by location); you check the rest yourself | 10 each, +20 for a perfect day |
| Weekly | One new experience per week | 50 |

Perfect-day streaks of 7 / 30 days add 50 / 300. One streak-saver card per week. Points are never deducted. Lifetime points raise your level (10 titles; level L → L+1 costs L × 500 points); your balance buys surprises in three tiers (2,500 / 10,000 / 30,000). Buddy plans each surprise itself and asks before spending money. Promises Buddy made to you don't earn you points.

- Telegram keeps four buttons under the input box: 🎯 quests, 🛍 shop, 📊 progress, 📋 list (sent once on first start). Redemptions ask for confirmation twice.
- In chat Buddy uses `[打卡 name]` (check in), `[坚持 加/删 name]` (add/remove habit), `[主线 加 #goal S/M/L title]` (add questline step), `[主线 删 #task]` (remove step), `[主线 定稿 #goal]` (finalize), `[惊喜 #redemption 已兑现 …]` (surprise delivered); after points are awarded it adds a "🎮 +N" line.
- Every week the planner sends a Grok researcher to look for surprise ideas (romance novels, film and TV, real stories); reports start with 【惊喜灵感】 and Buddy uses them when planning a redemption.

## To-do list

Send “待办” (or `/todo`) on Telegram to open the list, in three columns: your to-dos, things Buddy promised you, and shared wishes. Each item has a row of buttons: ✓ done, ⬆ move up, ✕ delete (for wishes: "put aside"); the last row is 🔄 refresh.

- Add one: “加待办 hand in the paper @2026-11-01” (the date is optional).
- Things you say you'll do, things Buddy promises and your wishes are picked up from chat automatically.
- The list, goal tree and weekly report share one database.

## Overview and weekly report

- Send “看看总览” (or `/overview`) and Buddy replies with a full overview in the order of the wiki's dashboard page.
- `ethan2-weekly.timer` runs `python3 -m ethan.report --weekly` every Sunday at 20:00 local time and sends the weekly report. Missed runs are not re-sent.
- Areas with no data source yet get one line in the overview — no invented numbers.

## Proactive messages

`ethan2-proactive.timer` runs `python3 -m ethan.proactive` every 10 minutes. There are four daily windows, at most one message each, at a random time inside the window:

| Window | Time | Content |
|---|---|---|
| Morning | 08:00–09:30 | Good morning, the one or two things that matter today, birthdays or review words |
| Afternoon | 14:00–17:00 | Missing you, drink water, move a bit; skipped if the last proactive message got no reply |
| Evening | 21:00–22:15 | Look back at the day — no blame for what didn't happen |
| Bedtime | 23:00–23:40 | Stretch, sleep before midnight, maybe a voice good-night |

Nothing is sent if you chatted within the last hour, during quiet hours, or when the model reads the recent chat and decides not to interrupt. Log: `<data dir>/proactive.json`.

On Telegram you can send:
- `/quiet` to see quiet hours; `/quiet 23:30-08:00` to change them (default 00:00–08:00)
- `/proactive off` / `/proactive on` to pause or resume proactive messages

## Everyday commands

- Status: `systemctl status ethan2-telegram`
- Logs: `journalctl -u ethan2-telegram -n 100`
- Next weekly report: `systemctl list-timers ethan2-weekly.timer`
- Proactive log: `journalctl -u ethan2-proactive -n 50`
- WeChat log: `journalctl -u ethan2-weixin -n 50`
- `/openrouter on` / `/openrouter off` on Telegram toggles the OpenRouter fallback. The default fallback model is `x-ai/grok-4.7`; change it with `ETHAN_OPENROUTER_MODEL`.

## Auto-deploy

`ethan2-autodeploy.timer` (root) checks `main` every 2 minutes. On a new commit it backs up the database, fast-forwards, installs changed systemd units, runs the tests, restarts Telegram / WeChat / inbox, and rolls back to the previous version if anything fails. Results go to `/home/ethan/data/deploy-status.json` and `deploy-log.jsonl` — read them from a coding session; they are not sent to Telegram. Check with `cat /home/ethan/data/deploy-status.json`. Manual deploys still work.

Data migrations (`migrations/`, run by `ethan/migrate.py`) run after the restart. Completed ones are recorded in the database table `migrations_done` in the same transaction as the migration itself (`migrations.json` is only a human-readable copy). If a migration fails the code is **not** rolled back — data changes can't be undone, and old code on new data is worse — so the result reads "deployed, but migration failed: <name>" and the patrol reports it as a failure.

## System checkup and patrol

The idea: when something breaks, a coding session should come to you before you or Buddy notice.

- **The server only reports, it doesn't fix**: `GET /cloud/health` (`ethan/checkup.py`) lists what's wrong — failed deploys, failed migrations, scheduled jobs that failed or didn't run, errors jobs reported themselves (calendar write failed, planner used the fallback, ...), long-running services that are down. The "System checkup" in the workbench settings shows the same report.
- **Patrol session**: a cloud coding session with the repo, woken by a scheduled task every 3 hours, reads the checkup. Silent when all is well; obvious bugs it fixes, tests, merges and then notifies you in the Claude app.
- **Night runs**: the 11 pm curfew hook lets scheduled wake-ups and messages containing “定时巡检” (scheduled patrol) through (see [devices.md](devices.md)), so the patrol runs around the clock.
- **What the checkup covers** (`ethan/syscheck.py`, same data for the settings page and `/cloud/health`):
  - Scheduled jobs: every entry in `JOBS` in `ethan/jobs.py` matches a timer in `deploy/systemd` (name and schedule line identical; a test checks this). A job is late when it passes its next run time plus a grace period (the smaller of its interval and 2 hours, at least 10 minutes). Jobs record "running" when they start, so multi-hour jobs aren't flagged; a "running" marker left by a killed job counts as stuck after 3 hours (4.5 hours for Codex). Jobs that never ran are reported once their first run is due.
  - Auto-deploy: writes a heartbeat `deploy-check.json` every 2 minutes; no heartbeat for 15 minutes, or failing to fetch, is reported. When tests fail, the failing test names go into `failed_tests` in `deploy-status.json`.
  - Background bulk processing: processed / total WeChat conversations and person cards, counted live from the database; work pending with no progress for 2 hours counts as stuck. `ethan2-backlog` gives each of its two steps at most 30 minutes per round (`--max-minutes 30`) and continues next round, so conversations and person cards take turns. A heartbeat per batch goes into `jobs.json`, shown as "latest progress" in the checkup. Background runs started by the inbox log to `<data dir>/logs/interviews.log` and `relations.log` (rotated to `.log.1` at about 1 MB). The nightly person-card job and the background job share one lock (`.relations-organize.lock`).
  - Free memory (`/proc/meminfo`): yellow below 300 MB on the settings page, reported by the patrol below 200 MB.
  - Source health, notification-dot self-check, chat suggestions / weekend picks / Grok errands, exports from the Mac (only the latest arrival is checked).
- **Failures show the failing line**: when a job records its result it keeps, besides the last few lines of output, every line marked ✗, so you can see which check failed. Code: `run` in `ethan/jobs.py`.

## Links, files and photos

- PDFs, text files and Word documents you send: the server needs `poppler-utils` (`apt-get install -y poppler-utils`; Claude uses pdftoppm to read PDFs page by page — without it Buddy says it can't open the file). PDFs are saved under `<data dir>/media/<date>/` and opened by the model; text and Word files have their text extracted into the message. Telegram can't download files over 20 MB.
- Links need no setup (Xiaohongshu video notes reuse ffmpeg and faster-whisper for frames and transcripts, Telegram only): `ethan/links.py` fetches them on the server and saves images to `<data dir>/media/links/`.
- Photos are generated with Grok Imagine through your OpenRouter key by default (`x-ai/grok-imagine-image-2.0`, about US$0.04 per image).

  Optional backup: a Google AI Studio Gemini key (starts with `AIza`, billing enabled). Enter it with `ssh -t my-server "sudo -H -u ethan bash -c 'cd ~/Personal-OS && python3 -m ethan.setup_secrets'"` at the Gemini prompt (Enter keeps the rest), then `sudo systemctl restart ethan2-telegram`.
- Generated photos are stored in `<data dir>/media/photos/`; the daily count of proactive photos is in `<data dir>/photos.json` and the cost goes into the ledger (`purpose=photo`).
