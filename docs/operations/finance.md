# 接上财务：Monzo 和国内银行卡

[English](../en/operations/finance.md) | 简体中文

接上以后，财务领域的“本周花销”和“每日开销”按真实交易算；在财务页保底旁 ✎ 定了每月预算（只按人民币，改的时候选立即或下月生效）后按预算判灯。展示和判灯规则见 ../design/territories.md 第 3 节。
钥匙只存在服务器上，不进聊天、截图和仓库。代码见 `ethan/finance.py`。

## 银行消费邮件：服务器邮件整理时直接记账（现行）

10-30 服务器邮件整理上线后，银行通知邮件被整理走了，iPhone 快捷指令收不到、记账就断了。现在改成：服务器每 10 分钟收邮件时（见 mail.md），认出是招行、交行的消费通知（发件人含 cmbchina / 95555 / bankcomm / 95559 或银行名），就直接交给记账，不用她转发。

- **招行储蓄卡“一卡通账户变动通知”、交行通知**：一封一笔，和原来短信同一个格式解析；同一条重复来只记一次。
- **招行信用卡“每日信用管家”汇总邮件**：一封列出前一天好几笔，拆成一笔一笔记；外币照原币记（之后按汇率折算），邮件里是北京时间，换成本地日期再记；同一笔（时间、金额、币种、商户都一样）不重复记。
- 认不出的内容只存档，不算钱。10-30 起漏记的那几天由迁移 `20261101_cmb_emails_backfill`、`20261101_cmb_digest_backfill` 补上。
- 邮件本身照常按邮件整理的规则分类、归档，记账不影响它。
- 代码：`ethan/mail.py` 的 `bank_email`、`ethan/finance.py` 的 `record_cmb` / `record_cmb_digest`。

## Apple Pay 每刷一次自动记账（Revolut 等没有接口的卡）

发现最近的消费都没记进来：办了 Revolut 卡，之后的花销走 Revolut，Revolut 不给个人开放接口。
做法：iPhone 快捷指令的“钱包 → 交易”自动化，每次用 Apple Pay 付款就把卡、商户、金额发到服务器（`finance.record_applepay`）。
Monzo 卡和招行卡发过来会被跳过（已有官方同步 / 邮件，避免算两遍），其余的卡（Revolut、Amex……）都记；同一张卡、同一商户、同一金额、同一分钟只记一次。
和邮件、Monzo 记的去重：金额、币种一样、日子相差不超过 1 天就算同一笔——邮件或 Monzo 先记了，Apple Pay 这条不记；Apple Pay 先记了，邮件或 Monzo 那条到了就以它为准、删掉 Apple Pay 那条。认出是 Revolut 的卡没有别的来源，不参与去重。

她要做的（一次，约 3 分钟）：
1. 打开“快捷指令” → 底部“自动化” → 右上“＋” → 往下找“钱包”（Wallet）。
2. 卡片选“Revolut”（别的没接的卡也可以一起勾上；Monzo、招行不用勾，勾了也会被跳过）；类别全选；选“立即运行”，关掉“运行时通知”，点“下一步”。
3. 选“新建空白自动化” → 添加操作“获取 URL 内容”：
   - URL：`https://ethan.example.com/cloud/spend`
   - 点“显示更多”：方法选 **POST**；头部加一项，键 `Authorization`，值 `Bearer ` 加钥匙（和出门 / 到家那两条快捷指令同一把，空格不要漏）；
   - 请求体选 **JSON**，加四项（后三项的值都点蓝色变量“快捷指令输入”，不要自己打字）：
     - `kind`：文本 `applepay`
     - `card`：快捷指令输入 → **卡或票证**（Card or Pass）
     - `merchant`：快捷指令输入 → **商户**（Merchant）
     - `amount`：快捷指令输入 → **金额**（Amount）
4. 完成。用 Revolut 付一笔小钱，几秒后工作台“财务”里就能看到这一笔（来源 revolut）。

## 国内银行卡：iPhone 快捷指令转发（旧做法，邮件整理上线后不再需要）

下面是 2026-11-01 之前的接法，留着备用（比如哪天服务器收不到某个邮箱）。

iPhone 快捷指令只能在**收到短信或邮件**时自动运行，App 推送和微信通知都触发不了。招行储蓄卡改成了邮件提醒（发件人 95555@message.cmbchina.com，标题“一卡通账户变动通知”），消费和退款的标题一样，系统只看正文区分；招行信用卡当时只有微信通知和月度电子账单，先截图发给 小伴 记账（现在“每日信用管家”汇总邮件已自动拆笔记账，见上）。交行有短信的用短信（发件号 95559）。

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
