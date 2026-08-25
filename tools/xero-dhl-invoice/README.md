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
   `offline_access`,对应新版细分 scope 命名)

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
