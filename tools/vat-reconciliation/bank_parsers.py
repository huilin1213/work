"""把三个账户(HSBC Debit / HSBC Credit / Revolut)导出的流水,统一转换成
BankTxn 列表,供 reconcile.py 跟发票登记表做匹配。

三个账户格式完全不一样:
- Revolut: xlsx。第一行是报表标题(合并单元格,比如
  "transaction-statement_01-Aug-2026_31-Aug-2026"),第二行才是真正的表头,
  列名是英文的 "Date completed (UTC)" / "Description" / "Amount" /
  "Payment currency" / "Type" / "State" 等 —— load_revolut_export() 是照着
  真实样本(REVOLUT_-_AUG.xlsx)写的,应该可以直接用。
- HSBC Debit / HSBC Credit: 目前还没拿到真实的 CSV 导出样本,load_hsbc_csv()
  是按网银 CSV 导出常见的列名("Date"/"Transaction Date",
  "Description"/"Details"/"Narrative","Amount" 或者 "Debit"+"Credit" 分两列)
  写的通用版本 —— 列名对不上会直接报错并提示,不会凭空瞎猜着往下跑出错误结果。
  把你实际导出的 HSBC CSV(哪怕打码金额也行)发我一份,我照真实列名改一下这两个
  函数,标题上说的"猜测"字样就可以去掉了。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import openpyxl

from common import EBAY_ORDER_RE, parse_date

# 这些收款人/描述是同一个老板名下账户之间互相转账(股东借款、公司间调拨等),
# 不是"买东西的支出",不应该出现在"待催发票"清单里,否则清单会被噪音淹没。
# 按你自己的实际情况增删这个列表。
OWN_ENTITY_KEYWORDS = [
    "Huilin Wang",
    "Huilin W",
    "Blue Bridgewell",
]

# Revolut 流水里这几种 Type 不是"买东西的支出":
# EXCHANGE = 币种内部兑换,TOPUP = 充值/收款,CARD_REFUND = 退款(收入)。
NON_EXPENSE_TYPES = {"EXCHANGE", "TOPUP", "CARD_REFUND"}


@dataclass
class BankTxn:
    account: str  # "Revolut" / "HSBC Debit" / "HSBC Credit"
    txn_date: object  # datetime.date
    description: str
    amount: float  # 负数=支出,正数=收入/退款
    currency: str
    txn_type: str = ""  # Revolut 有,HSBC 版本目前留空
    ebay_order_no: str | None = None
    row_ref: str = ""  # 方便人工回查这一行在原始文件里的位置

    def __post_init__(self):
        if self.ebay_order_no is None:
            m = EBAY_ORDER_RE.search(self.description)
            if m:
                self.ebay_order_no = m.group(1)

    def is_own_transfer(self) -> bool:
        return any(k.lower() in self.description.lower() for k in OWN_ENTITY_KEYWORDS)

    def is_candidate_expense(self) -> bool:
        """判断这笔流水"看起来像"一笔需要对应发票的采购支出。"""
        if self.amount >= 0:
            return False
        if self.txn_type and self.txn_type in NON_EXPENSE_TYPES:
            return False
        if self.is_own_transfer():
            return False
        return True


def load_revolut_export(path: str | Path, account_label: str = "Revolut") -> list[BankTxn]:
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))

    header_row_idx = next(
        (i for i, row in enumerate(rows) if row and "Date completed (UTC)" in row), None
    )
    if header_row_idx is None:
        raise ValueError(f"{path} 里没找到 'Date completed (UTC)' 这一列,确认一下是不是 Revolut 官方导出的 xlsx")
    header = rows[header_row_idx]
    idx = {name: i for i, name in enumerate(header) if name}

    required = ["Date completed (UTC)", "Description", "Amount", "Payment currency", "State"]
    missing = [c for c in required if c not in idx]
    if missing:
        raise ValueError(f"Revolut 导出文件里缺少列: {missing},表头可能变了,发我看看")

    txns: list[BankTxn] = []
    for r_i, row in enumerate(rows[header_row_idx + 1 :], start=header_row_idx + 2):
        if not row or row[idx["Date completed (UTC)"]] is None:
            continue
        state = row[idx["State"]]
        if state not in ("COMPLETED", None):
            continue  # 跳过失败/待处理的交易
        amount = row[idx["Amount"]]
        if amount is None:
            continue
        txns.append(
            BankTxn(
                account=account_label,
                txn_date=parse_date(row[idx["Date completed (UTC)"]]),
                description=str(row[idx["Description"]] or ""),
                amount=float(amount),
                currency=str(row[idx["Payment currency"]] or "GBP"),
                txn_type=str(row[idx["Type"]] or "") if "Type" in idx else "",
                row_ref=f"{Path(path).name} 第{r_i}行",
            )
        )
    return txns


def load_hsbc_csv(path: str | Path, account_label: str) -> list[BankTxn]:
    """通用 CSV 解析 —— 还没拿到真实 HSBC 导出样本校准过,列名对不上会直接
    报错并提示,而不是猜一个错的结果出来。"""
    date_candidates = ["Date", "Transaction Date", "Posting Date"]
    desc_candidates = ["Description", "Details", "Narrative", "Transaction Description"]
    amount_candidates = ["Amount", "Value"]
    debit_candidates = ["Debit", "Paid out", "Money Out", "Debit Amount"]
    credit_candidates = ["Credit", "Paid in", "Money In", "Credit Amount"]

    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []

        def pick(cands):
            return next((c for c in cands if c in fieldnames), None)

        date_col = pick(date_candidates)
        desc_col = pick(desc_candidates)
        amount_col = pick(amount_candidates)
        debit_col = pick(debit_candidates)
        credit_col = pick(credit_candidates)

        if not date_col or not desc_col or not (amount_col or debit_col or credit_col):
            raise ValueError(
                f"没认出 {path} 的列名(现有列: {fieldnames})。"
                "这是没校准过真实 HSBC 样本的通用猜测版本——"
                "把这个文件发我一份(可以打码金额),我照实际列名改一下 load_hsbc_csv()。"
            )

        txns: list[BankTxn] = []
        for r_i, row in enumerate(reader, start=2):
            if amount_col:
                amt_raw = (row.get(amount_col) or "").replace(",", "").replace("£", "").strip()
                amount = float(amt_raw) if amt_raw else None
            else:
                debit = (row.get(debit_col) or "").replace(",", "").replace("£", "").strip()
                credit = (row.get(credit_col) or "").replace(",", "").replace("£", "").strip()
                if debit:
                    amount = -abs(float(debit))
                elif credit:
                    amount = abs(float(credit))
                else:
                    amount = None
            if amount is None:
                continue
            txns.append(
                BankTxn(
                    account=account_label,
                    txn_date=parse_date(row[date_col]),
                    description=row[desc_col] or "",
                    amount=amount,
                    currency="GBP",
                    row_ref=f"{Path(path).name} 第{r_i}行",
                )
            )
    return txns
