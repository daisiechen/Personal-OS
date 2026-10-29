# DemoApp 举报提醒（2026-10-29 她同意）

DemoApp（她的你的 App）的举报要在 24 小时内复核，**复核由她本人负责，小伴 只负责提醒**。
DemoApp 的 worker 把只含数字和后台链接的消息推给 小伴 的接收口，小伴 收到后马上在 Telegram 告诉她。
小伴 看不到举报内容，也不做任何处理。代码：`ethan/fancy_moderation.py`，路由在 `ethan/inbox.py`。

## 接口

- `POST https://ethan.example.com/fancy/moderation`
- 请求头 `Authorization: Bearer <INBOX_TOKEN_FANCY>`，正文 JSON，最大 64KB。
- 正文（DemoApp 侧契约，字段都必填，未知字段忽略）：

```json
{"kind": "morning|evening|urgent", "sentAt": "2026-10-29T08:00:00Z",
 "open": 3, "autoLimited": 1, "overdue": 0, "dueWithin6h": 0, "oldestHoursLeft": 5.2,
 "urgentOpen": 0, "reason": "safety|harassment|under_18|null", "count": 0,
 "adminUrl": "https://demoapp.example/..."}
```

- 校验：数字必须是非负整数（`oldestHoursLeft` 可以是小数、负数或 null）；`adminUrl` 只收 https、主机是 `demoapp.example` 或它的子域、不带用户名密码，最长 2048 字；不合格返回 400，钥匙不对 401，正文过大 413。
- 回复：发出去 `{"ok": true, "sent": true}`；重复或没有要看的 `{"ok": true, "sent": false}`；Telegram 没绑定 503、发送失败 502（这两种不算发过，DemoApp 可以重试）。

## 她收到的消息

- morning：“DemoApp 有 3 条举报待看（1 条已自动限制），最早一条还剩 5 小时。→ 后台链接”（有超时的就说几条已经超过 24 小时；没有待看的不发）
- evening：“DemoApp 还有 2 条举报快到 24 小时（1 条已超时）。→ 链接”（只有超时的就提醒已超时；都没有不发）
- urgent：“DemoApp 收到一条紧急举报（人身安全 / 骚扰或仇恨 / 疑似未满 18 岁），请尽快看。→ 链接”，count 大于 1 时写“N 条”。
- 去重：同一种 10 分钟内重复到达只发第一条；urgent 在这 10 分钟里按 count 合并，count 比已经提醒过的大才再发一条报新的条数（count 按 DemoApp 这一批紧急举报的条数理解）。

运维类消息（部署、报错）照旧不发 Telegram；这是她的待办提醒，她同意发。

## 不进记忆

- 消息直接用 Telegram Bot API 发（同周报的发法），不经 小伴 的大脑：不进聊天原文、记忆、Wiki、ethan-vault 和 小伴 的聊天上下文；也不写收件箱 `inbox/`（Grok 回报转告、第二大脑只读那里），和【Claude 会话】那类回报一样被隔开。
- 只在服务器 `/home/ethan/data/fancy-moderation.jsonl` 记一行：时间、kind、数字、结果（sent / duplicate / nothing / failed），不记链接和任何内容。去重状态在同目录 `fancy-moderation-state.json`。

## 钥匙

单独一把 `INBOX_TOKEN_FANCY`，只能进 `/fancy/moderation`。漏了，别人最多能让 小伴 给她发这类提醒，碰不到记忆和聊天；
作废就在服务器上清空 `~/.config/ethan/ethan.env` 里这一项，再重跑 setup_secrets（会生成新的），然后照下面第 3、4 步换掉。

## 她要做的（一次性，合并上线后）

1. 在 Mac 终端登上服务器重跑一次：`ssh -t my-server "sudo -H -u ethan bash -c 'cd ~/Personal-OS && python3 -m ethan.setup_secrets'"`。各项直接回车保留（包括备份密钥；“DemoApp 举报提醒密钥”也直接回车，会自动生成），最后会显示“DemoApp 举报提醒用的密钥”。
2. 重启接收口让它读到新密钥：`ssh my-server "systemctl restart ethan2-inbox"`。
3. 让 Cloudflare Tunnel 放行这个路径（Mac 或服务器会话代做，要登录服务器）：按 [deploy.md](deploy.md) 里的 `config.yml` 示例，在 `ingress` 里加 `^/fancy/moderation$` 这一条，先备份，`ingress validate` 后 `systemctl restart cloudflared`。验证：`curl -s -o /dev/null -w '%{http_code}' -X POST https://ethan.example.com/fancy/moderation` 返回 401。
4. 把第 1 步那串值填进 DemoApp 部署配置的 `ETHAN_MODERATION_TOKEN`（DemoApp 那边的密钥设置里），**不要贴进聊天**。
