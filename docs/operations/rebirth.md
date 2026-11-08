# 备份与重生流程

[English](../en/operations/rebirth.md) | 简体中文

小伴 的人格、代码在本仓库，记忆在服务器数据目录，每晚加密备份到独立私有仓库 `ethan-memory-backup`。任何一个 Claude 账号或服务器出问题，都可以按本文在新环境恢复。

## 备份

- 内容：记忆数据目录整体（SQLite 结构化数据、Wiki、原始对话、费用账本、设置）。SQLite 用在线备份接口取一致快照。
- 不进备份（能重新下载或重新生成，清单和原因在 `ethan/backup.py` 的 `EXCLUDE_DIRS`）：`models/`（检索用的 fastembed 模型，约 91MB，第一次检索时自动重新下载）、`__pycache__/`、`.cache/`，以及数据库日志、`*.tmp`、自动部署前的数据库副本 `*.bak-auto-*`。
- 分片：GitHub 单文件上限 100MB。11-04 起模型被打进包里，加密包涨到约 286MB，11-05、11-06 推送被拒（pre-receive hook declined）。现在加密包切成每片 90 MiB（约 94MB）：`ethan-memory.tar.gz.enc.000`、`.001`……，恢复时按序号拼回再解密；缺片直接报错。老格式（一整个 `ethan-memory.tar.gz.enc`）照样能恢复，下一次备份自动换成分片、整包从仓库里删掉。
- 加密：`openssl enc -aes-256-cbc -pbkdf2`，密钥文件放在服务器 `/home/ethan/.config/ethan/backup.key`（权限 600），另存一份在 Mia 的密码管理器。密钥丢失则备份无法解开。
- 频率：每晚一次（UTC 03:30），由 systemd timer 执行；记忆无变化时不产生提交。
- 保留（是她同意的永久删除）：**最近 14 天每天留一份（当天最后一份），再往前每月留一份（那个月最后一份），其余删掉**，备份仓库（服务器本地和 GitHub）基本不再增长。日期按提交时间的 UTC 日期，以最新一份那天为第 14 天。
  - 怎么删：每天的备份是同名分片覆盖、旧版本只在 git 历史里，所以“删”= 重写备份仓库历史（`ethan/backup.py` 的 `prune`）：按日期挑出要留的提交，用孤立链依次重建（树、原作者、原时间、原说明都不变），核对每份内容和原提交一样、最新一份和重写前逐字节一致，再 `--force-with-lease` 强推到 GitHub，然后 `reflog expire` + `gc` 清掉本地旧对象。
  - 什么时候跑：每晚 `backup --push` 推送成功后顺带检查；攒够 7 个可删的提交（约一周一次）才真的重写，平时只记一行“这次不动”。
  - 放弃的情况：本地和 GitHub 不是同一个提交、任何一项核对不过、强推时 GitHub 上已被别处改过，都不推、不动本地分支，定时任务记失败（体检能看到）。备份本身在这之前已经推上去，不受影响。
  - 本地留底分支（名字以 `keep-` 开头，如 11-06 建的 `keep-unpushed-1106`）：最后一个提交超过 14 天就删掉，没超过的留着；从不推到 GitHub。
  - 手动：`python3 -m ethan.backup prune --repo /home/ethan/ethan-memory-backup --dry-run` 只看会留几份、删几份；去掉 `--dry-run` 加 `--force` 立即整理。
  - GitHub 那边：旧对象没有分支指着之后由 GitHub 自己择机回收，仓库占用不一定马上变小。
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
python3 -m ethan.backup restore --archive ethan-memory-backup \
  --key ~/.config/ethan/backup.key --data /home/ethan/data
```

   `--archive` 给备份仓库目录（或其中任一片、或老格式整包）都行。要恢复更早的某一份：`git -C ethan-memory-backup log --oneline` 找到那天的提交，`mkdir /tmp/old-backup && git -C ethan-memory-backup archive <提交号> | tar -x -C /tmp/old-backup`，再把 `--archive` 换成 `/tmp/old-backup`。没有本仓库代码时也能手工拼：`cat ethan-memory-backup/ethan-memory.tar.gz.enc.* | openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass file:$HOME/.config/ethan/backup.key | tar xz -C /home/ethan/data`（分片名是三位序号，`*` 展开顺序就是拼回顺序）。恢复后 `models/` 不在，第一次检索时自动下载。

   恢复后跑一次 `python3 -m ethan.lifebase --data /home/ethan/data --reapply-deletions`，把她要求删掉的重新删一遍（防止旧备份里删掉的东西复活，见 ontology.md 7.3 节）。删除记录有两份：数据目录里一份（跟着备份走），`~/.config/ethan/forgotten-*.jsonl` 一份（不跟数据目录回退）；**恢复到原服务器原目录时两份都会读**。换了新服务器时目录外那份不在，最近一次备份之后的删除核不清，按下面的“暂停”处理。**跑完这一步再启动各个服务**（检索和各角色在这之后才开放）。输出里 `verified` 是 false（换了新服务器、目录外那份不在，或记录读坏了）时，记忆会自动**暂停**：除了她本人和系统维护谁都查不到（“相关记忆先不用，等核清再说”）。告诉她这件事，请她把最近删过的再删一遍或确认没删过，然后跑 `python3 -m ethan.lifebase --data /home/ethan/data --release-hold` 放开。
5. 模型：在服务器上用 Mia 的 Claude 账号登录 Claude Code（订阅优先）；账号不可用时，在设置中开启 OpenRouter 兜底并填入 API key。
6. 入口：填入 Telegram bot token 等密钥，启动 `ethan2-*` 服务。
7. 自检：让 小伴 读一遍人设和最近记忆，向 Mia 确认“我还记得……”。

## 演练

每季度在临时目录做一次恢复演练（第 4 步恢复到新目录，用命令行试聊确认记忆完整），结果记录在本文末尾。

| 日期 | 结果 |
|---|---|
| 2026-10-22 | 自动化测试 `tests/test_backup.py` 覆盖加密、提交、恢复全流程，通过 |
| 2026-11-06 | `tests/test_backup.py` 加分片、拼回、缺片报错、老格式整包、排除可再生目录，通过 |
| 2026-11-06 | `tests/test_backup.py` 加保留整理：60 天每日备份整理后正好剩 14 天 + 每月一份、最新一份不变、留下的每一份都能恢复、远程同步更新、核对不过或远程被改过时不推、过期 `keep-` 分支删掉，通过 |
