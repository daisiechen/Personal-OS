# Spending: Monzo and Chinese bank cards

English | [简体中文](../../operations/finance.md)

> Translated from the Chinese original. If they disagree, the Chinese version wins. Terms: [glossary](../../glossary.md).

Once connected, the Money territory's "spending this week" and "daily spending" use real transactions. After you set a monthly budget (the ✎ next to the floor on the Money page — RMB only, effective now or next month), the territory light is judged against that budget.
Keys live only on the server — never in chat, screenshots or the repo. Code: `ethan/finance.py`.

The built-in integrations are the author's: Monzo (UK) and two Chinese banks, China Merchants Bank (CMB) and Bank of Communications (BoCom). For other banks, add a parser next to `record_cmb` in `ethan/finance.py`, or simply forward spending notifications to Buddy in chat.

## Bank emails: logged automatically by the mail job (current method)

When the server fetches mail (every 10 minutes, see `ethan/mail.py`), it recognises CMB and BoCom spending notifications (sender contains cmbchina / 95555 / bankcomm / 95559 or the bank's name) and hands them straight to the ledger. Nothing to forward.

- **CMB debit card "account change" emails and BoCom notifications**: one email per transaction, parsed in the same format as the old SMS; duplicates are logged once.
- **CMB credit card daily digest emails**: one email lists the previous day's transactions; each becomes its own entry. Foreign currency is kept in the original currency (converted later). The email uses Beijing time and is converted to your local date. The same transaction (time, amount, currency, merchant) is never logged twice.
- Anything unrecognised is archived but not counted as money.
- The email itself is still sorted and archived by the normal mail rules.
- Code: `bank_email` in `ethan/mail.py`, `record_cmb` / `record_cmb_digest` in `ethan/finance.py`.

## Chinese bank cards via iPhone Shortcuts (older method, kept as a backup)

Use this if the server can't read the mailbox your bank writes to.

iPhone Shortcuts automations can only fire on **incoming SMS or email** — not on app push notifications or WeChat messages. CMB debit cards can switch to email alerts (sender 95555@message.cmbchina.com); spending and refunds share a subject and are told apart by the body. Where a bank sends SMS (BoCom: 95559), use SMS.

1. Add the mailbox that receives bank email to Apple Mail.
2. Shortcuts → Automation → "+" → Email; Sender: 95555@message.cmbchina.com; choose "Run Immediately" and turn off "Notify When Run".
3. Add the action "Get Contents of URL": URL `https://ethan.example.com/cloud/spend`, method POST, header `Authorization` with value `Bearer ` plus the key (the same key as the leave / arrive automations in [devices.md](devices.md)).
4. Request body JSON: `text` = the blue variable "Shortcut Input" → "Body"; add `sender` = "Shortcut Input" → "Sender". It must be the variable, not typed text.
5. After your next purchase, the "Chinese bank card" row in the workbench settings turns "OK". Unrecognised content is archived, not counted. For SMS, pick "Message" in step 2 and enter the sender number.

## Monzo (UK spending, about 10 minutes)

Monzo doesn't let third parties read accounts directly; you create a developer client for yourself only.

1. On a computer open https://developers.monzo.com and log in with Monzo (confirm by email or push on your phone).
2. "Clients" → "New OAuth Client":
   - Name: your AI's name
   - Logo URL: leave empty
   - Redirect URLs: `https://ethan.example.com/cloud/monzo/callback`
   - Description: `personal`
   - Confidentiality: **Confidential**
   - Submit. You'll get a Client ID (starts with `oauth2client_`) and a Client secret.
3. Enter them on the server (from your computer's terminal):
   `ssh -t my-server "sudo -H -u ethan bash -c 'cd ~/Personal-OS && python3 -m ethan.setup_secrets'"`
   Press Enter on everything else; paste at the two Monzo prompts (client ID and client secret). Then
   `ssh my-server "sudo systemctl restart ethan2-inbox ethan2-workbench"`
4. Workbench → Settings → "Monzo" row → authorize. Log in on Monzo's page and follow the link in the email back.
5. When the page says Monzo is connected, **immediately open the Monzo app and tap "Allow access"**. Within 5 minutes of approval the system pulls your full history (after 5 minutes Monzo only allows the last 90 days).
6. From then on it syncs hourly; the Monzo row in settings shows the last sync time. Monzo asks you to re-approve in the app roughly every 90 days, and Buddy will remind you.
