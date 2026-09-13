"""发票登记表(Excel)的读写。

为什么不直接"自动解析发票 PDF/照片"跳过这一步:纸质发票是拍照存进文件夹的
(塑料文件袋反光、拍摄角度、每个卖家版式都不一样),本地 OCR 在这种照片上认错
数字(尤其是金额小数点、6/8 这类形近数字)的概率不低,VAT 记账不能拿"大概率
认对"的数字去对账。所以现阶段的工作方式是:

  1. 收到新发票(纸质拍照或 PDF)后,照着 ledger_template.xlsx 的格式填一行
     关键字段(日期/供应商/含税总额/VAT金额,3 分钟内能填完一张);
     eBay 订单里买的,把"订单号"也填上(在发票上找 "Order:" 后面那串
     "两位数字-五位数字-五位数字"),这是后面匹配银行流水最可靠的依据。
     —— 或者干脆把照片发到跟我的对话里,我帮你读出这几个字段,你复制进表里,
     比对着糊掉的照片自己一个个字打字快,也比不校准的 OCR 准。
  2. 填完之后,reconcile.py 直接读这张表去跟银行流水核对,不需要再解析
     PDF/图片本身。

用法:
    python3 invoice_ledger.py new                  # 在当前目录生成空模板 ledger_template.xlsx
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import openpyxl

from common import EBAY_ORDER_RE, parse_date

COLUMNS = ["日期", "供应商", "含税总额", "VAT金额", "eBay订单号", "发票文件名", "备注"]
TEMPLATE_PATH = Path(__file__).parent / "ledger_template.xlsx"


@dataclass
class InvoiceRecord:
    invoice_date: date
    vendor: str
    gross_total: float
    vat_amount: float
    ebay_order_no: str | None
    source_file: str
    notes: str
    row_ref: str = ""


def load_ledger(path: str | Path) -> list[InvoiceRecord]:
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    header = rows[0]
    idx = {name: i for i, name in enumerate(header) if name}
    required = ["日期", "供应商", "含税总额", "VAT金额"]
    missing = [c for c in required if c not in idx]
    if missing:
        raise ValueError(f"登记表缺少必填列: {missing},对照 ledger_template.xlsx 检查表头拼写")

    records: list[InvoiceRecord] = []
    for r_i, row in enumerate(rows[1:], start=2):
        if not row or row[idx["日期"]] is None:
            continue
        vendor = str(row[idx["供应商"]] or "").strip()
        gross = row[idx["含税总额"]]
        vat = row[idx["VAT金额"]]
        if gross is None or vat is None:
            print(f"⚠️  第 {r_i} 行金额没填全,已跳过: {row}")
            continue

        ebay_no = None
        if "eBay订单号" in idx and row[idx["eBay订单号"]]:
            ebay_no = str(row[idx["eBay订单号"]]).strip()
        if not ebay_no:
            m = EBAY_ORDER_RE.search(vendor)
            ebay_no = m.group(1) if m else None

        records.append(
            InvoiceRecord(
                invoice_date=parse_date(row[idx["日期"]]),
                vendor=vendor,
                gross_total=float(gross),
                vat_amount=float(vat),
                ebay_order_no=ebay_no,
                source_file=str(row[idx["发票文件名"]] or "") if "发票文件名" in idx and row[idx["发票文件名"]] else "",
                notes=str(row[idx["备注"]] or "") if "备注" in idx and row[idx["备注"]] else "",
                row_ref=f"{Path(path).name} 第{r_i}行",
            )
        )
    return records


def write_template(path: str | Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "发票登记"
    ws.append(COLUMNS)
    ws.append(
        [
            date(2026, 8, 3),
            "示例: Green IT (eBay 卖家)",
            640.00,
            106.67,
            "23-14958-56688",
            "IMG_0001.jpg",
            "示例行,填自己的数据后删掉",
        ]
    )
    for col_cells in ws.columns:
        width = max(len(str(c.value)) for c in col_cells if c.value is not None)
        ws.column_dimensions[col_cells[0].column_letter].width = max(10, width + 2)
    wb.save(path)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "new":
        out = Path(sys.argv[2]) if len(sys.argv) > 2 else TEMPLATE_PATH
        write_template(out)
        print(f"✅ 已生成空模板: {out}")
    else:
        print("用法: python3 invoice_ledger.py new [输出路径]")
