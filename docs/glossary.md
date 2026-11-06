# Glossary · 术语表

[English docs](README.md) · [中文文档](README.zh-CN.md)

Fixed English names for the project's terms, so the English docs, issues and future i18n strings all say the same thing.
The UI is Chinese-only for now; the right-hand column is what you will see on screen.

英文文档、Issue 和以后的界面翻译统一用下面的译名。界面目前只有中文，右列就是界面上的字。

## The companion · 陪伴

| English | 中文 | Notes · 说明 |
|---|---|---|
| Buddy | 小伴 | The AI companion's default name. You rename it during setup; code and service names keep the internal name `ethan`. 默认名字，安装时由用户改名；代码和服务里的 `ethan` 是内部名字。 |
| persona | 人设 | `persona/ethan.md` (who Buddy is) and `persona/script-guide.md` (Buddy's own daily life). |
| memory | 记忆 | Everything Buddy remembers about you, stored only in your data directory. 只放在你自己的数据目录。 |
| ontology | ontology（“我”的记忆体系） | The structured model of you behind the memory: facts, people, rhythms, preferences, with sources and confidence. Kept as the English word in both languages. 中英文都写 ontology。 |
| verbatim quote | 原话 | Something you said yourself. Only verbatim quotes count as evidence about you; what an agent paraphrased is "agent paraphrase" (agent 转述). |
| second brain | 第二大脑 | The wiki / Obsidian vault Buddy keeps in sync. |
| person card | 人物卡 | One card per person in your life (relationship, recent news, open loops). |
| interview handoff | 访谈 / 交给记忆 | Sending a conversation or document into Buddy's memory for processing. |
| proactive message | 主动消息 | Buddy messages you first (morning, afternoon, evening, bedtime). |
| quiet hours | 免打扰 | No proactive messages during this window. |
| morning brief | 早报 | First message of the day plus the Today card. |
| Today card | 今日卡 | Timeline, goal progress, next steps and tasks for the day; also an illustrated image card. |
| prep | 小伴替你准备 / 准备员 | Research, price comparison, drafts that Buddy prepares before handing over. |

## The game · 游戏

| English | 中文 | Notes · 说明 |
|---|---|---|
| Earth Online | 地球 Online | The default game skin: life as an MMO. |
| workbench | 工作台 | The web dashboard (`ethan/workbench/`). |
| season | 赛季 | A roughly three-month play period with its own goals. |
| quest | 副本 | A season goal with a deadline and a clear "done" condition. At most five per season. |
| main quest | 主副本 | The most important quest of the season. |
| questline | 主线 | The ordered steps a quest is broken into (6–20 steps, drafted by the planner). |
| side task | 支线 | Any other one-off to-do. |
| daily / weekly | 日常 / 周常 | Recurring habits (3–7 dailies, 1–2 weeklies). |
| territory | 领地 | One area of life you want to keep healthy. Defaults: Health 健康, Career 事业, Money 财务, Social 社交, Learning 学习, Academic 学术, Life experiences 生活体验. |
| baseline | 健康线 | A territory's pass line, e.g. "train at least 3 times a week". |
| wilderness | 荒原 | Inbox of to-dos that are not sorted into a territory yet. |
| points | 积分 | Earned by finishing quests, steps and habits; spent in the shop. |
| level / title | 等级 / 称号 | Levels come from lifetime points; each level band has a title. |
| shop | 商店 | Where points are redeemed for surprises. |
| surprise | 惊喜 | A reward Buddy plans for you. Not spoiled in advance. |
| achievement / hidden achievement | 成就 / 隐藏成就 | Bronze, silver and gold tiers; hidden ones only show a riddle. |
| streak-saver card | 补签卡 | One per week; fills a missed daily. |
| wish | 心愿 | A long-term wish without a deadline; may become a candidate quest. |
| candidate quest | 候选副本 | Parked for a future season. |
| day self / night self | 白天的我 / 夜晚的我 | The two avatars on the home screen. |

## Operations · 运维

| English | 中文 | Notes · 说明 |
|---|---|---|
| data directory | 数据目录 | `/home/ethan/data` by default. Never committed. |
| cold start | 冷启动 | First-time setup of your own season, habits and baselines. |
| planner | 计划器 | Builds each day's plan at 06:30 local time. |
| system checkup | 系统体检 | Health summary in the workbench settings and at `/cloud/health`. |
| patrol | 巡检 | A scheduled session that reads the checkup and reports or fixes problems. |
| auto-deploy | 自动部署 | Pulls `main` every 2 minutes, runs tests, restarts, rolls back on failure. |
| migration | 数据改动（migration） | A one-off script that changes your data, run once after deploy. |
| rebirth | 重生 | Restoring Buddy from the encrypted memory backup on a new machine. |
| inbox endpoint | 接收口 | `ethan2-inbox`, the HTTP endpoint other services post to. |
| curfew hook | 收工钩子 | Blocks new coding-agent prompts late at night. |
| workaholic index | 工作狂指数 | 0–100; 20–49 is the healthy range. |
| needs your call | 待你定 | Items the system cannot decide for you. |
