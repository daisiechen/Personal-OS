# Personal OS · Build your own Earth Online!

English | [简体中文](README.zh-CN.md)

**Play your life like a game, with an AI companion that remembers you and checks in on its own. Self-hosted: your data never leaves your machine.**

<!-- First-screen slot: demo screenshot / GIF (workbench home + the morning brief in chat). Put it in docs/media/ and replace the line below. -->
<p align="center"><em>Demo screenshot / GIF coming soon</em></p>

## One-command install

<!-- First-screen slot: replace with a single installer command once there is one -->
```sh
git clone https://github.com/your-name/Personal-OS.git && cd Personal-OS
claude "Follow SETUP-FOR-AGENTS.md and set this up for me"
```

Not on Claude Code? Open the folder in any coding agent that can read files and run commands (Codex, Qwen Code, Trae, Cursor, ...) and say the same thing.

## How it compares

<!-- First-screen slot: comparison table -->
| | Personal OS | Gamified habit apps | AI companion apps | To-do / notes apps |
|---|---|---|---|---|
| Self-hosted, you own the data | ✓ | rarely | ✗ | rarely |
| AI plans your day and reminds you on time | ✓ | ✗ | ✗ | partly (you plan) |
| Long-term memory, messages you first | ✓ | ✗ | ✓ | ✗ |
| Seasons, quests, territories, points, achievements | ✓ | ✓ | ✗ | ✗ |
| Lives in WeChat / Telegram | ✓ | ✗ | partly | ✗ |
| Open source | ✓ (AGPL-3.0) | some | ✗ | some |

## What it is

Personal OS turns your life into a long-running game. Each **season** (about three months) you pick a few **quests** — goals with a deadline and a clear finish line. You guard seven **territories** (health, career, money, social, learning, academic, life experiences) with simple baselines, do **dailies** to earn points, unlock **achievements**, and spend points in the **shop** on surprises.

You play it with **Buddy**, an AI companion with a life of its own. You choose its name and personality. Buddy chats with you on WeChat or Telegram, builds your plan every morning, nudges you when it's time, and does the legwork — research, price checks, drafts — before handing things over. A web **workbench** shows your progress, lets you check things off and open surprises.

This is the public edition of a system the author built for herself and uses every day. It is feature-complete; her personal content has been replaced with examples.

## Set it up: hand it to your agent

Give this repo to the coding agent you already use and say:

> Follow SETUP-FOR-AGENTS.md and set this up for me.

It will ask a handful of questions, one at a time — where to run it, which model, which chat app, what your AI is called, what you want to finish this season — then install and customize everything. If you already keep a goals spreadsheet, hand it over; the agent splits it into quests, dailies and to-dos.

You only need two things:
- a computer that stays on, or the cheapest cloud server you can find (2 vCPU / 2 GB is plenty);
- an API key for a model (any OpenAI-compatible API such as DeepSeek, or a Claude subscription via Claude Code).

## Language

The **UI is Chinese-only for now** — the workbench and all chat messages. The docs come in English and Chinese. UI internationalization (i18n) is on the roadmap. Buddy itself replies in whatever language you write to it, but buttons, cards and system messages are still in Chinese.

## What's inside

| Part | Where |
|---|---|
| The companion: persona, memory, chat, proactive messages | `ethan/core/`, `persona/` |
| The game: seasons, quests, questlines, dailies, points, shop, achievements | `ethan/quests.py`, `ethan/achievements.py`, `ethan/territory.py` |
| Daily planning, training and meals | `ethan/planner.py`, `playbooks/` |
| Web workbench | `ethan/workbench/` |
| WeChat and Telegram channels | `ethan/channels/` |
| Scheduled jobs and deployment | `deploy/` |

`ethan` is the internal package name; it has nothing to do with what your AI is called.

Docs: [docs/README.md](docs/README.md). Terms and their Chinese UI names: [docs/glossary.md](docs/glossary.md).

## Roadmap

- UI i18n, starting with English.
- A one-line installer script.
- More play packs: territory templates, personas, achievements, training and meal playbooks.

## License

AGPL-3.0 (see LICENSE). Use, modify and redistribute freely; if you run a modified version as a service for other people, you must publish your source too.

## Contributing

Questions and ideas are welcome in Issues, in English or Chinese. We especially welcome "play packs": new achievements, territory templates, personas, training and meal playbooks, surprise ideas.
