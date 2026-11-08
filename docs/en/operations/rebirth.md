# Backup and rebirth

English | [简体中文](../../operations/rebirth.md)

> Translated from the Chinese original. If they disagree, the Chinese version wins. Terms: [glossary](../../glossary.md).

Buddy's persona and code live in this repo; its memory lives in the server's data directory and is encrypted and backed up every night to a separate private repo, `ethan-memory-backup`. If a model account or the server dies, follow this page to bring Buddy back somewhere else.

## Backup

- What: the whole memory data directory (SQLite structured data, wiki, raw conversations, cost ledger, settings). SQLite is copied with the online backup API for a consistent snapshot.
- Left out (can be re-downloaded or regenerated; the list and reasons are `EXCLUDE_DIRS` in `ethan/backup.py`): `models/` (the fastembed search model, ~91MB, re-downloaded on first search), `__pycache__/`, `.cache/`, plus database journals, `*.tmp` and the pre-deploy database copies `*.bak-auto-*`.
- Split: GitHub rejects files over 100MB, so the encrypted archive is cut into 90 MiB parts, `ethan-memory.tar.gz.enc.000`, `.001`, …; restore joins them in order and refuses if one is missing. The old single-file format (`ethan-memory.tar.gz.enc`) still restores; the next backup replaces it with parts.
- Encryption: `openssl enc -aes-256-cbc -pbkdf2`. The key file is `/home/ethan/.config/ethan/backup.key` on the server (mode 600), with a copy in your password manager. **Lose the key and the backup can't be opened.**
- Schedule: nightly via a systemd timer; no commit when memory hasn't changed.
- Retention: keep one backup per day for the last 14 days (the day's last one) and one per month before that (the month's last one); the rest are deleted so the backup repo stops growing. Old versions only live in git history, so this rewrites the backup repo's history (`prune` in `ethan/backup.py`): rebuild the kept snapshots with their original dates and messages, check the newest one is byte-for-byte unchanged, `--force-with-lease` push, then `reflog expire` + `gc`. It runs after a successful nightly push once at least 7 commits can be dropped (about weekly); any failed check aborts without pushing and marks the job failed. Local `keep-*` branches are deleted after 14 days and never pushed. Preview with `python3 -m ethan.backup prune --repo /home/ethan/ethan-memory-backup --dry-run`.
- Command:

```sh
python3 -m ethan.backup backup --data /home/ethan/data --repo /home/ethan/ethan-memory-backup \
  --key /home/ethan/.config/ethan/backup.key --push
```

## Rebirth

1. Prepare a new server (or the old one) and create the `ethan` user.
2. Clone this repo and `ethan-memory-backup`.
3. Take the backup key from your password manager, write it to `~/.config/ethan/backup.key`, `chmod 600`.
4. Restore memory into an empty directory:

```sh
python3 -m ethan.backup restore --archive ethan-memory-backup \
  --key ~/.config/ethan/backup.key --data /home/ethan/data
```

   `--archive` takes the backup repo directory, any one part, or an old single-file archive. Without this repo's code you can join by hand: `cat ethan-memory-backup/ethan-memory.tar.gz.enc.* | openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass file:$HOME/.config/ethan/backup.key | tar xz -C /home/ethan/data` (parts have three-digit numbers, so the glob order is the join order). `models/` isn't in the backup; it downloads on first search.

   Then run `python3 -m ethan.lifebase --data /home/ethan/data --reapply-deletions` once, so anything you asked Buddy to forget is deleted again (otherwise deletions made after the backup would come back to life). Deletions are recorded twice: once in the data directory (travels with the backup) and once in `~/.config/ethan/forgotten-*.jsonl` (outside it, so it doesn't roll back). **Restoring to the same directory on the same server reads both.** On a new server the outside copy is missing, so deletions since the last backup can't be verified — see "hold" below. **Start the services only after this step** (search and the other roles only open up afterwards).
   If the output says `verified: false` (new server without the outside copy, or the record couldn't be read), memory goes on **hold**: nobody but you and system maintenance can query it. Re-delete anything you removed recently (or confirm you didn't), then release it with `python3 -m ethan.lifebase --data /home/ethan/data --release-hold`.
5. Model: log in to Claude Code with your Claude account on the server (subscription preferred), or turn on the OpenRouter fallback in settings and enter an API key. Or use any OpenAI-compatible model.
6. Channels: enter the Telegram bot token and other secrets, start the `ethan2-*` services.
7. Self-check: have Buddy read its persona and recent memory, and confirm with you: "I still remember …".

## Drills

Once a quarter, restore into a temporary directory (step 4 into a fresh directory, then chat from the command line to confirm memory is intact) and record the result here.

| Date | Result |
|---|---|
| 2026-10-22 | Automated test `tests/test_backup.py` covers encrypt, commit and restore end to end: pass |
