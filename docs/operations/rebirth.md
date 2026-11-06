# 备份与重生流程

[English](../en/operations/rebirth.md) | 简体中文

小伴 的人格、代码在本仓库，记忆在服务器数据目录，每晚加密备份到独立私有仓库 `ethan-memory-backup`。任何一个 Claude 账号或服务器出问题，都可以按本文在新环境恢复。

## 备份

- 内容：记忆数据目录整体（SQLite 结构化数据、Wiki、原始对话、费用账本、设置）。SQLite 用在线备份接口取一致快照。
- 加密：`openssl enc -aes-256-cbc -pbkdf2`，密钥文件放在服务器 `/home/ethan/.config/ethan/backup.key`（权限 600），另存一份在 Mia 的密码管理器。密钥丢失则备份无法解开。
- 频率：每晚一次，由 systemd timer 执行；记忆无变化时不产生提交。
- 命令：

```sh
python3 -m ethan.backup backup --data /home/ethan/data --repo /home/ethan/ethan-memory-backup \
  --key /home/ethan/.config/ethan/backup.key --push
```

## 重生步骤

1. 准备新服务器（或原服务器），新建用户 `ethan`。
2. clone 本仓库和 `ethan-memory-backup`。
3. 从密码管理器取出备份密钥，写入 `~/.config/ethan/backup.key`，`chmod 600`。
4. 恢复记忆到一个空目录：

```sh
python3 -m ethan.backup restore --archive ethan-memory-backup/ethan-memory.tar.gz.enc \
  --key ~/.config/ethan/backup.key --data /home/ethan/data
```

   恢复后跑一次 `python3 -m ethan.lifebase --data /home/ethan/data --reapply-deletions`，把她要求删掉的重新删一遍（防止旧备份里删掉的东西复活，见 ontology.md 7.3 节）。删除记录有两份：数据目录里一份（跟着备份走），`~/.config/ethan/forgotten-*.jsonl` 一份（不跟数据目录回退）；**恢复到原服务器原目录时两份都会读**。换了新服务器时目录外那份不在，最近一次备份之后的删除核不清，按下面的“暂停”处理。**跑完这一步再启动各个服务**（检索和各角色在这之后才开放）。输出里 `verified` 是 false（换了新服务器、目录外那份不在，或记录读坏了）时，记忆会自动**暂停**：除了她本人和系统维护谁都查不到（“相关记忆先不用，等核清再说”）。告诉她这件事，请她把最近删过的再删一遍或确认没删过，然后跑 `python3 -m ethan.lifebase --data /home/ethan/data --release-hold` 放开。
5. 模型：在服务器上用 Mia 的 Claude 账号登录 Claude Code（订阅优先）；账号不可用时，在设置中开启 OpenRouter 兜底并填入 API key。
6. 入口：填入 Telegram bot token 等密钥，启动 `ethan2-*` 服务。
7. 自检：让 小伴 读一遍人设和最近记忆，向 Mia 确认“我还记得……”。

## 演练

每季度在临时目录做一次恢复演练（第 4 步恢复到新目录，用命令行试聊确认记忆完整），结果记录在本文末尾。

| 日期 | 结果 |
|---|---|
| 2026-10-22 | 自动化测试 `tests/test_backup.py` 覆盖加密、提交、恢复全流程，通过 |
