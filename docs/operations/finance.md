# 接上财务：Monzo 和国内银行卡（2026-10-29）

接上以后，财务区域的“每月花销”按真实交易算；在财务页定了每月预算（只按人民币，定了本月锁住）后按预算判灯。
钥匙只存在服务器上，不进聊天、截图和仓库。代码见 `ethan/finance.py`。

## 国内银行卡（招行走邮件，她已设好；约 5 分钟，她本人在 iPhone 上做）

iPhone 快捷指令只能在**收到短信或邮件**时自动运行，App 推送和微信通知都触发不了（2026-10-29 她问）。招行储蓄卡改成了邮件提醒（发件人 95555@message.cmbchina.com，标题“一卡通账户变动通知”），消费和退款的标题一样，系统只看正文区分；招行信用卡只有微信通知和月度电子账单，先截图发给 小伴 记账。交行有短信的用短信（发件号 95559）。

1. 苹果“邮件”App 里加上收银行邮件的邮箱（126 / QQ 都行）。
2. 打开“快捷指令” → 底部“自动化” → “＋” → 选“邮件”；“发件人”填 95555@message.cmbchina.com；选“立即运行”，关掉“运行时通知”。
3. 新建空白自动化 → 添加操作“获取 URL 内容”；URL 填 `https://ethan.example.com/cloud/spend`，方法 POST，头部加 `Authorization`，值是 `Bearer ` 加钥匙（和出门 / 到家那两条同一把）。
4. 请求体选 JSON：`text` 的值点蓝色变量“快捷指令输入” → “正文”；再加 `sender`，值选“快捷指令输入” → “发件人”。不能是打的字。
5. 下一笔消费后，工作台设置页“国内银行卡”那行变“正常”就通了。认不出的内容只存档，不算钱。短信同理，只是第 2 步选“信息”、填发件号。

## Monzo（英国花销，约 10 分钟，她本人做）

Monzo 不让第三方直接读账，要她用自己的账号建一个只给自己用的“开发者客户端”。

1. 电脑浏览器打开 https://developers.monzo.com ，用 Monzo 登录（手机上会弹邮件或推送确认）。
2. 点 “Clients” → “New OAuth Client”：
   - Name：`小伴`
   - Logo URL：留空
   - Redirect URLs：`https://ethan.example.com/cloud/monzo/callback`
   - Description：`personal`
   - Confidentiality：选 **Confidential**
   - 提交。页面上会出现 Client ID（`oauth2client_` 开头）和 Client secret。
3. 在服务器上录入（和以前录别的钥匙一样，Mac 终端）：
   `ssh -t my-server "sudo -H -u ethan bash -c 'cd ~/Personal-OS && python3 -m ethan.setup_secrets'"`
   其他项直接回车保留，到“Monzo 开发者客户端 ID”和“Monzo 客户端密钥”两项时粘贴。然后
   `ssh my-server "sudo systemctl restart ethan2-inbox ethan2-workbench"`
4. 打开工作台 → 设置 → “Monzo”那行点“去授权”，在 Monzo 页面登录，邮件里点链接回来。
5. 页面显示“Monzo 连上了”后，**马上打开 Monzo App 点批准（Allow access）**。批准后 5 分钟内系统会把历史交易拉下来（Monzo 规定 5 分钟后只能拉最近 90 天）。
6. 之后每小时自动同步一次。设置页“Monzo”那行显示最近同步时间就是好的。Monzo 大约每 90 天会要求在 App 里再批准一次，到时 小伴 会提醒。
