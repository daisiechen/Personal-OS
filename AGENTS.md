# Notes for AI agents · 给 AI agent 的说明

- First time setting this repo up for a user: read SETUP-FOR-AGENTS.md (English) or SETUP-FOR-AGENTS.zh-CN.md (中文) and follow it step by step.
- Reply in the user's language, in plain words, one question at a time. The UI is Chinese-only for now; say so to non-Chinese-speaking users.
- Secrets, passwords and tokens never go into chat or the repo: the user runs `python3 -m ethan.setup_secrets` on the server and types them in.
- The user's personal content (goals, body data, chats and memory) stays in their own data directory and is never committed.
- After changing code run `python3 -m unittest discover -s tests`; done means all green.
- Payments, messaging other people on the user's behalf, and permanently deleting data all need the user's own confirmation.
- Use the term names in docs/glossary.md.

---

- 第一次接手这个仓库、要帮用户装起来时，读 SETUP-FOR-AGENTS.zh-CN.md（英文版 SETUP-FOR-AGENTS.md），按里面的步骤做。
- 回复用户用他的语言，大白话，一次只问一个问题。
- 密钥、密码、令牌不进聊天、不进仓库：让用户自己在服务器上运行 `python3 -m ethan.setup_secrets` 录入。
- 用户的个人内容（目标、身体数据、聊天和记忆）只放在他自己的数据目录里，不提交到仓库。
- 改了代码要跑测试：`python3 -m unittest discover -s tests`，全过才算完成。
- 付款、替用户给别人发消息、永久删除数据，都要用户本人确认。
- 术语按 docs/glossary.md 统一。
