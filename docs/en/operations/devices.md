# Data from your Mac and iPhone

English | [简体中文](../../operations/devices.md)

> Translated from the Chinese original. If they disagree, the Chinese version wins. Terms: [glossary](../../glossary.md).

Once these two are connected, the territory baselines "work hours", "go outside every day" and "go out for something other than work" stop showing "waiting for data", the matching rows under Settings → Connections turn green, and the "go outside every day" achievement starts counting streaks.
Both use the key written into `~/.claude/settings.json` on your Mac when you installed the session check-in hook (`ETHAN_HOOK_TOKEN`) — nothing new to enter. Keys never go into chat, screenshots or the repo.

## Mac work hours

How it's counted: minutes with keyboard or mouse activity on the Mac, plus the time you spend sending messages to coding agents; an agent running by itself doesn't count. Later additions are under "Workaholic index and evening curfew" below.
Every 5 minutes the agent checks for input activity; any activity marks that 5-minute slot. Offline slots are buffered in `~/.ethan-worktime.json` and sent later.
Up to 8 hours a day is green, 8–10 yellow, over 10 red. Code: `ethan/worktime.py`.

Install (once, from the repo directory on your Mac):

```bash
python3 -m ethan.worktime --install   # installs a launchd job: every 5 minutes, starts at login
python3 -m ethan.worktime --once      # try it right now
```

To stop: `launchctl unload ~/Library/LaunchAgents/me.mia.ethan-worktime.plist`.
Only "which 5-minute slots had activity" is sent — never keystrokes or app names.

## iPhone location (leave / arrive home)

Only two events are reported: "left home" and "arrived home". No destinations, no routes. Code: `ethan/places_log.py`.
- The first time you leave home each day, the daily "go outside" is checked automatically (the habit switches itself on the first time an event arrives).
- A trip of at least an hour that isn't training (leaving within an hour of a planned training departure counts as training) counts as "went out for something other than work".

Setup (Shortcuts app → Automation, two automations, about 5 minutes):

1. Get the key onto your iPhone: run `python3 -m ethan.worktime --copy-token` on the Mac. The key goes to the clipboard without being shown; paste it on the iPhone via Universal Clipboard. Afterwards copy something else to overwrite it.
2. New personal automation → "Leave" → your home location → "Run Immediately" (turn off "Ask Before Running").
3. Action "Get Contents of URL":
   - URL: `https://ethan.example.com/cloud/place`
   - Method: POST
   - Header: `Authorization` = `Bearer ` followed by the pasted key
   - Request body: JSON, one text field `event` = `leave`
4. A second automation for "Arrive" → home, same action with `event` = `arrive`.
5. Test it: tap "Run" in the automation. Within a few minutes the "phone location" row in the workbench settings turns green.

iPhone location automations sometimes fire a few minutes late or miss once; baselines are counted per day, so it hardly matters.

## Workaholic index and evening curfew

Rules are documented at the top of `ethan/workaholic.py`. Work time has four sources: Mac input activity, prompts you send to Claude Code (Mac and cloud), time the Claude / Codex apps are open on your iPhone, and time the workbench is open and in use. Chatting with Buddy on Telegram doesn't count.
Output only counts for career and academic work. At 9 pm you get a reminder, at 10 pm a nudge, at 11 pm a stop. Tell Buddy "I need to push tonight, until X" to get an exception.

Details of the work-time calculation:
- **Server-side Claude sessions don't count**: those are almost entirely Buddy's own background calls (sorting mail, writing the diary) and messages from development sessions.
- **The workbench only counts when it was clicked, scrolled or typed in during the last 3 minutes** — leaving it open on screen doesn't count.
- **A day starts at 5 am**: midnight to 5 am belongs to the previous evening.
- **The workaholic index has a healthy range of 20–49**, not "lower is better": below 20 slack, 20–49 healthy, 50–69 high, 70+ overdoing it.

**The 11 pm check for `codex` in the terminal**: if you have a shell alias named `codex`, zsh can't define a function with the same name. The installer records the alias, removes it, defines `function codex` with the check, and runs your original alias when allowed.

**The curfew hook lets scheduled tasks and patrols through**: the cloud session's curfew hook passes two kinds of messages without blocking: scheduled-task wake-ups (queued remote notifications) and patrol messages containing “定时巡检”. So the patrol keeps running after 11 pm (see [deploy.md](deploy.md), "System checkup and patrol"). Your own new prompts after 11 pm are still blocked. Cloud sessions opened from a parent directory don't read the repo's hooks; install it once in the cloud environment's setup script, see [cloud-curfew.md](cloud-curfew.md).

**What the reminders look like**: on the Mac, a full-screen notice with the first sentence as a large title and the rest in smaller text, centered. On the iPhone, the Shortcut checks whether `message` has a value (below).

### Mac (in the repo directory on your Mac, `git pull` first)

```bash
python3 -m ethan.worktime --install                  # reinstall: full-screen notice after 11 pm (no screen lock), plus the 11 pm check for terminal codex
python3 -m ethan.setup_hook --where mac --ssh my-server  # reinstall: Claude Code gets the curfew hook, new prompts after 11 pm are blocked
```

### iPhone (Shortcuts → Automation, two each for Claude and Codex, about 5 minutes)

Same key as "iPhone location" above (`python3 -m ethan.worktime --copy-token` on the Mac, paste via Universal Clipboard).

1. New personal automation → "App" → Claude → "Is Opened" → Run Immediately (turn off "Ask Before Running"). Actions:
   - "Get Contents of URL": URL `https://ethan.example.com/cloud/activity`, POST, header `Authorization` = `Bearer <key>`, JSON with two text fields `event` = `open`, `app` = `Claude`;
   - "Get Dictionary Value": key `message` (only has text when it's time to stop; empty otherwise);
   - "If" Dictionary Value "has any value": inside it, "Show Alert" (with the dictionary value) and "Go to Home Screen" — both indented between "If" and "End If". (Check `message`, not `stop`; the `stop` condition is easy to misconfigure.)
2. Another one: App → Claude → "Is Closed" → Run Immediately, with only "Get Contents of URL" and `event` = `close`.
3. Repeat both for the Codex (or ChatGPT) app with `app` = `Codex`.
4. Test: open and close the Claude app once; "worked X hours today" on the workbench home goes up a little.
