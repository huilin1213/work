# DHL 海关发票 → Xero 自动开票

从 DHL 发货后生成的 Commercial Invoice(海关发票)PDF 里提取收货人和商品明细,
自动在 Xero 创建一张 **Draft(草稿)** 状态的销售发票,供你最终核对后手动发送。

当前是"手动运行脚本"模式:你把 PDF 存下来,跑一条命令,不涉及监听邮箱/文件夹。

## 目录结构

```
dhl_parser.py    解析 DHL PDF -> 结构化数据(收货人、逐行商品、金额、币种等)
xero_auth.py     首次授权:浏览器登录 Xero 一次,把 refresh token 存到本地
xero_client.py   Xero API 封装:token 刷新、查重、创建发票
main.py          主流程:PDF -> 校验 -> 查重 -> 创建 Xero 草稿发票
```

## 一、环境准备

```bash
cd tools/xero-dhl-invoice
pip install -r requirements.txt
cp .env.example .env
```

## 二、申请 Xero App(免费,一次性)

1. 登录 https://developer.xero.com/app/manage ,点 **New app**
2. **App type** 选 **Mobile or desktop app**(PKCE 授权,免费,不需要 Client Secret,
   适合这种个人/单账套的自动化脚本 —— 不要选 Custom Connection,那个每月额外收费)
3. **Redirect URI** 填 `http://localhost:8765/callback`(要和 `.env` 里的
   `XERO_REDIRECT_URI` 保持一致)
4. 创建后复制 **Client ID**,填到 `.env` 的 `XERO_CLIENT_ID`
5. Scopes 不需要在网页上手动勾——2026 年 4 月起 Xero 把 scope 拆得更细了,
   网页上那个 "Scopes" tab 现在只是列出所有可用 scope 名字的参考列表,
   实际请求哪些 scope 是脚本在发起授权请求时指定的(见 `xero_auth.py` 里的
   `SCOPES` 变量,目前用的是 `accounting.invoices` + `accounting.contacts` +
   `accounting.settings.read` + `offline_access`,对应新版细分 scope 命名)

## 三、首次授权(一次性,之后自动续期)

```bash
python xero_auth.py
```

会自动打开浏览器,登录你的 Xero 账号并选择要连接的组织(公司账套)。授权完成后
token 会存到本地的 `.xero_tokens.json`(已加入 `.gitignore`,不会被提交到仓库)。

之后每次跑 `main.py`,脚本会自动用 refresh token 换新的 access token,**不需要
再重复这一步** —— 唯一要注意的是:如果连续 60 天以上没有运行过脚本,refresh
token 会过期,届时重新运行 `python xero_auth.py` 再授权一次就行。

## 四、按你账套的实际情况填几个默认值

打开 `.env`,确认这三项(登录 Xero 网页版查):

| 变量 | 在 Xero 里哪里查 |
|---|---|
| `XERO_ACCOUNT_CODE` | Accounting > Advanced > Chart of Accounts,出口销售用的科目代码 |
| `XERO_TAX_TYPE` | Settings > Invoice settings > Tax rates,里面列出的 Tax Type 名字 |
| `XERO_INVOICE_STATUS` | 默认 `DRAFT`,建议先保持不动,人工核对后再改成自动过账 |

## 五、日常使用

从 DHL 网站/系统下载好这一票货的 Commercial Invoice PDF 后:

```bash
python main.py ~/Downloads/CustomInvoice_1017186892.pdf
# 一次处理多个文件也可以:
python main.py ~/Downloads/CustomInvoice_*.pdf
```

脚本会:
1. 解析出收货人(SHIP TO)信息和逐行商品明细
2. 校验逐行金额合计是否等于发票 Total Invoice Amount,不一致会提示但不中止
3. 用 `DHL AWB <运单号>` 作为 Xero 发票的 Reference 查重 —— 同一票货重复跑脚本
   不会重复开票
4. 创建 Draft 发票,打印发票号,登录 Xero 网页版核对无误后手动发送

## 六、进阶:自动监听文件夹(可选,更省事)

如果不想每次都手动敲命令、拖 PDF,可以让脚本常驻后台自动处理:

```bash
python3 watch_folder.py
```

默认监听 `~/DHL发票/待处理` 这个文件夹(会自动创建)。之后你只要把从 DHL 下载
的发票 PDF 存进这个文件夹,脚本每隔 10 秒自动检查一次,发现新文件就自动处理:
- 成功的移到 `处理完成/` 子文件夹
- 失败的移到 `处理失败/` 子文件夹,并打印失败原因,不会反复重试同一个文件

这个脚本要一直留着跑(终端窗口可以缩小,但不能关掉),按 `Control+C` 停止。
如果 DHL 网站下载发票时能自己选保存位置,直接选 `~/DHL发票/待处理` 这个文件
夹,连"存进去"这一步手动挪文件的动作都省了。

想要连开终端手动启动 `watch_folder.py` 这一步都省掉(开机自动在后台运行),
可以配置成 macOS 的 LaunchAgent 登录启动项 —— 这个需要额外配置,想做的话告诉
我,我再给你写具体步骤。

## 七、整体重做一批发票(作废旧发票 → 重开 → 登记收款 / 预付款)

当旧发票和 DHL 报关单、银行收款对不上时,用 `rebuild_invoices.py` 按一个 JSON
计划文件一次性整理(格式见 `plans/example_plan.json`)。真实计划文件含客户信息,
`plans/*.json` 已加入 `.gitignore`,只有示例会被提交。

**准备工作(一次性)**
1. 这个脚本需要额外的 `accounting.payments` 和 `accounting.banktransactions`
   scope,已加进 `xero_auth.py`,**必须重新运行一次 `python3 xero_auth.py`**
2. 在 Xero 里建两个不接银行流水的银行账户当"清算账户"(Accounting → Bank
   accounts → Add bank account),比如 `Global Pay Clearing`、`USD Receipts
   Clearing`,把它们的科目代码填到计划文件的 `clearing_accounts` 里。预付款/多付款
   只能登记在 BANK 类型的账户上,所以这里必须是银行账户,不能是普通科目

**执行顺序**(每一步默认只预览,加 `--execute` 才写入;重复执行会自动跳过已完成的)

```bash
python3 rebuild_invoices.py plans/2026-08.json validate    # 本地检查计划文件:逐行合计/units/收款分配是否自洽
python3 rebuild_invoices.py plans/2026-08.json check       # 只读检查 Xero:旧发票状态、新发票号是否可用、清算账户
python3 rebuild_invoices.py plans/2026-08.json void-old --execute    # 删除旧发票上的收款并作废(会要求输入 YES)
python3 rebuild_invoices.py plans/2026-08.json create-new --execute  # 建 DRAFT 新发票
# → 去 Xero 网页版逐张核对并 Approve(脚本不会自动过账)
python3 rebuild_invoices.py plans/2026-08.json payments --execute    # 登记收款 + 预付款/多付款
```

注意:如果旧发票的收款已经和银行流水对过账(reconciled),Xero 不允许用 API
删除,`void-old` 会报错——先在网页版 Bank account 里对那几笔点 Unreconcile 再重跑。
清算账户里的手续费、转入 Revolut 的净额仍在 Xero 网页版做银行对账时处理。

## 已知限制 / 后续可以做的事

- 目前假设 DHL PDF 是标准 Commercial Invoice 模板(见 `dhl_parser.py` 里对表格
  和坐标的解析逻辑);如果 DHL 换了模板格式,`_extract_ship_to` /
  `_extract_line_items` 可能需要跟着调整
- 商品行是逐行 1:1 搬到 Xero 发票上的(目前这份样本 17 行商品对应 17 行发票明
  细);如果你更想按类别合并成几行汇总,在 `main.py` 的 `build_invoice_payload`
  里改一下拼装逻辑就行
- Contact 匹配:目前每次都用收货人姓名作为 Xero Contact Name 直接创建/复用
  (Xero 按名字自动去重),没有做更复杂的"客户主数据"匹配
- 如果之后想做成全自动(监听邮箱/文件夹、无需手动运行),可以在这个基础上加个
  定时任务或邮件监听,再考虑要不要升级到付费的 Custom Connection
