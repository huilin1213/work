# VAT 发票 ↔ 银行支出对账

把「收到的 VAT 发票」和「HSBC Debit / HSBC Credit / Revolut 三个账户的实际支出」
自动配对,算出每笔支出对应的 VAT 金额,并且把**银行里有支出、但还没收到发票**的
eBay 订单单独列出来,附上可以直接发给卖家的催发票话术。

## 为什么不是"直接解析发票 PDF/照片,全自动"

你现在的纸质发票是拍照存进文件夹的(塑料文件袋反光、每个 eBay 卖家的发票版式都
不一样)。本地 OCR 在这种照片上认错数字(尤其是金额小数点、6/8 这类形近字符)的
概率不低——VAT 申报的数字不能靠"大概率认对"。

所以现在的工作方式是**你维护一张登记表,脚本负责核对**:
1. 收到新发票后,照着 `ledger_template.xlsx` 的格式填一行关键字段(日期/供应商/
   含税总额/VAT金额,3 分钟内能填完一张)。eBay 订单买的,把"eBay订单号"也填上
   (发票上 "Order:" 后面那串「两位数字-五位数字-五位数字」,比如
   `23-14958-56688`)——这是后面匹配银行流水最可靠的依据,比"金额+日期"模糊匹配
   靠谱得多。
   - 或者更省事:把发票照片发到跟我的对话里,我帮你读出这几个字段,你核对一下
     金额、复制进表里就行,比对着糊掉的照片自己一个个字打字快,也比没校准过的
     本地 OCR 准。
2. 填完之后跑 `reconcile.py`,它直接读这张表去跟银行流水核对,不需要再解析
   PDF/图片本身。

## 目录结构

```
common.py           日期解析、eBay 订单号正则(两个模块共用)
bank_parsers.py      把 HSBC / Revolut 导出的流水统一转成 BankTxn
invoice_ledger.py    发票登记表的读写;`python3 invoice_ledger.py new` 生成空模板
reconcile.py          核心匹配逻辑 + 产出 Excel 核对表
ledger_template.xlsx  发票登记表空模板(复制一份自己维护,比如 发票登记-2026-08.xlsx)
```

## 一、环境准备

```bash
cd tools/vat-reconciliation
pip install -r requirements.txt
```

## 二、导出三个账户的流水

**优先用 CSV/Excel 导出,不要用 PDF 对账单** —— PDF 表格解析容易因为换行/分栏
出错(DHL 那个工具已经踩过这个坑),网银导出的 CSV/Excel 列是对齐的,可靠得多。

- **Revolut**:网页版 Statements 里选日期范围导出 **Excel (.xlsx)**,
  `bank_parsers.py` 的 `load_revolut_export()` 已经照真实样本
  (`REVOLUT_-_AUG.xlsx`)写好了,直接能用。
- **HSBC Debit / HSBC Credit**:网银导出 **CSV**。`load_hsbc_csv()` 目前是按
  常见列名("Date"/"Transaction Date","Description"/"Details"/"Narrative",
  "Amount" 或者 "Debit"+"Credit" 分两列)写的**通用猜测版本**,还没拿真实 HSBC
  导出文件校准过 —— 列名对不上会直接报错并提示,不会给你一个算错的结果。
  第一次用之前,把你实际导出的 HSBC CSV(可以打码金额)发我一份,我照真实列名
  把这个函数改准,到时候把 README 这段提示删掉。

## 三、维护发票登记表

```bash
python3 invoice_ledger.py new 发票登记-2026-08.xlsx
```

打开生成的表,按格式一行一张发票地填(参考表里的示例行):

| 列名 | 说明 |
|---|---|
| 日期 | 发票日期 |
| 供应商 | 卖家/供应商名字,eBay 发票上"Post from"那栏的公司名 |
| 含税总额 | 发票上的 Order total / GBP Total / TOTAL,银行实际扣款的金额 |
| VAT金额 | 发票上的 VAT amount |
| eBay订单号 | eBay 订单买的才填,格式 `23-14958-56688`;不是 eBay 订单的留空 |
| 发票文件名 | 对应哪个 PDF/照片文件,方便回查,选填 |
| 备注 | 选填 |

## 四、跑对账

```bash
python3 reconcile.py \
  --ledger 发票登记-2026-08.xlsx \
  --bank ~/Desktop/VAT\ ACCOUNTING/AUGUST/BANK\ STATEMENT/REVOLUT_-_AUG.xlsx:revolut \
  --bank ~/Desktop/VAT\ ACCOUNTING/AUGUST/BANK\ STATEMENT/hsbc_debit_aug.csv:hsbc:"HSBC Debit" \
  --bank ~/Desktop/VAT\ ACCOUNTING/AUGUST/BANK\ STATEMENT/hsbc_credit_aug.csv:hsbc:"HSBC Credit" \
  --out VAT对账结果-2026-08.xlsx
```

跑完打开 `VAT对账结果-2026-08.xlsx`,里面 5 个 sheet:

- **汇总** —— 已匹配笔数、VAT 合计、待催发票笔数/金额等整体数字
- **已匹配** —— 发票和银行支出配对成功,附对应的 VAT 金额;黄色高亮的行是
  "按 eBay 订单号配对上了,但金额跟发票不完全一致",人工核对一下(可能是部分
  退款/折扣之后的尾差)
- **发票未匹配银行流水** —— 登记表里有这张发票,但银行流水里没找到对应支出,
  常见原因是这张发票是用另一张卡付的款(先确认三个账户的流水都导入了)
- **待催发票-eBay订单** —— 银行流水里能看出是 eBay 订单支出,但登记表里还没
  有对应发票,每一行都自带一句可以直接复制发给卖家的英文话术
- **待催发票-其他** —— 其它没对上发票的支出(非 eBay),人工确认是不是需要
  发票、要不要联系对方补开

## 匹配逻辑

1. **eBay 订单号完全一致**优先:发票登记表里的"eBay订单号"和银行流水描述里的
   订单号(比如 Revolut 里的 "Ebay O\*23-14958-56688")字符串完全相同就直接
   配对——这个比"金额+日期"模糊匹配可靠得多,不受汇率尾差、入账延迟影响。
2. 剩下配不上订单号的(通常是非 eBay 的直接供应商,比如 IT Recycling
   Company),按"供应商名字模糊相似 + 金额一致(默认容许 ±£0.02 误差)+ 日期
   相差在默认 10 天以内"匹配。误差范围可以用 `--amount-tolerance` /
   `--date-window` 调整。
3. 转账给自己名下其他账户/关联方的钱(`bank_parsers.py` 里
   `OWN_ENTITY_KEYWORDS` 列的名字,比如 "Huilin Wang" / "Blue Bridgewell")
   不算"采购支出",不会出现在待催发票清单里——按你自己的实际情况增删这个列表。

## 已知限制 / 后续可以做的事

- `load_hsbc_csv()` 还没拿真实样本校准过,见上面"环境准备"部分说明
- 目前只支持 GBP;Revolut 的 `EXCHANGE`(币种兑换)行会被自动排除,不当成支出
- eBay 订单号匹配依赖发票和银行流水双方都能提取出同一个订单号——如果银行描述
  被截断(比如卡组织限制商户描述长度)导致订单号不完整,会退化成模糊匹配,匹配
  不上就会出现在"待催发票"清单里,需要人工核对一下是不是其实已经开过发票
- 如果之后想让"发票登记表"这一步更省事,可以在这个基础上加一个轻量 OCR 预填
  (比如 pytesseract 先猜一版草稿,人工只需要核对不用从头打字),现在先不做是
  因为没有先验证过这批照片在识别率上到底能到多准
