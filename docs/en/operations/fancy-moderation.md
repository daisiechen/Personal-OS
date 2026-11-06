# Example: moderation alerts from your own app

English | [简体中文](../../operations/fancy-moderation.md)

> Translated from the Chinese original. If they disagree, the Chinese version wins. Terms: [glossary](../../glossary.md).

This is an example of letting another service of yours push alerts to you through Buddy. "DemoApp" stands for an app you run whose user reports must be reviewed within 24 hours. **You do the review; Buddy only reminds you.**
DemoApp's worker pushes a message containing only counts and an admin link to Buddy's inbox endpoint, and Buddy forwards it to you on Telegram right away. Buddy never sees report content and never acts on it. Code: `ethan/fancy_moderation.py`, route in `ethan/inbox.py`.

## API

- `POST https://ethan.example.com/fancy/moderation`
- Header `Authorization: Bearer <INBOX_TOKEN_FANCY>`, JSON body, 64 KB max.
- Body (DemoApp-side contract; all fields required, unknown fields ignored):

```json
{"kind": "morning|evening|urgent", "sentAt": "2026-10-29T08:00:00Z",
 "open": 3, "autoLimited": 1, "overdue": 0, "dueWithin6h": 0, "oldestHoursLeft": 5.2,
 "urgentOpen": 0, "reason": "safety|harassment|under_18|null", "count": 0,
 "adminUrl": "https://demoapp.example/..."}
```

- Validation: counts must be non-negative integers (`oldestHoursLeft` may be fractional, negative or null). `adminUrl` must be https, on `demoapp.example` or a subdomain, without credentials, at most 2048 characters. Invalid → 400, wrong token → 401, body too large → 413.
- Response: sent → `{"ok": true, "sent": true}`; duplicate or nothing to review → `{"ok": true, "sent": false}`; Telegram not paired → 503, send failed → 502 (neither counts as sent, so DemoApp may retry).

## What you receive

- morning: "DemoApp has 3 reports to review (1 auto-limited); the oldest has 5 hours left. → admin link" (mentions how many are already past 24 hours; nothing is sent when there's nothing to review)
- evening: "DemoApp has 2 reports close to 24 hours (1 overdue). → link" (only overdue ones: an overdue reminder; neither: nothing sent)
- urgent: "DemoApp received an urgent report (personal safety / harassment or hate / possibly under 18), please look soon. → link"; "N reports" when count > 1.
- De-duplication: repeats of the same kind within 10 minutes only send the first. Urgent alerts within those 10 minutes are merged by count; a new message goes out only if the count is higher than what you were already told.

Operational messages (deploys, errors) still never go to Telegram; this one is your own to-do reminder, so it does.

## Kept out of memory

- Sent directly through the Telegram Bot API (like the weekly report), bypassing Buddy's brain: not in the raw chat log, memory, wiki, `ethan-vault` or Buddy's chat context, and not written to `inbox/` either.
- The only record is one line per event in `/home/ethan/data/fancy-moderation.jsonl`: time, kind, counts, outcome (sent / duplicate / nothing / failed) — no links, no content. De-duplication state is in `fancy-moderation-state.json` in the same directory.

## The key

A separate `INBOX_TOKEN_FANCY` that only opens `/fancy/moderation`. If it leaks, the worst someone can do is make Buddy send you these reminders; they can't reach memory or chats.
To revoke it, clear that entry in `~/.config/ethan/ethan.env` on the server and rerun `setup_secrets` (it generates a new one), then replace it as in steps 3–4 below.

## One-time setup

1. From your computer, rerun `ssh -t my-server "sudo -H -u ethan bash -c 'cd ~/Personal-OS && python3 -m ethan.setup_secrets'"`. Press Enter everywhere to keep existing values (including the backup key; Enter on the DemoApp key generates one). At the end it shows the DemoApp moderation key.
2. Restart the inbox so it picks up the key: `ssh my-server "systemctl restart ethan2-inbox"`.
3. Let the path through Cloudflare Tunnel: add the `^/fancy/moderation$` rule to `ingress` as in the `config.yml` example in [deploy.md](deploy.md). Back up first, run `ingress validate`, then `systemctl restart cloudflared`. Check: `curl -s -o /dev/null -w '%{http_code}' -X POST https://ethan.example.com/fancy/moderation` returns 401.
4. Put the value from step 1 into DemoApp's deploy config as `ETHAN_MODERATION_TOKEN` (in DemoApp's secret settings). **Don't paste it into a chat.**
