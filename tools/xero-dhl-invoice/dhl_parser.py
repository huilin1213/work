"""解析 DHL Commercial Invoice(海关发票)PDF,提取开票所需字段。

DHL 这份 PDF 是"SHIP FROM / SHIP TO"并排两栏的固定模板,直接抽取整页文本会把
两栏内容按行错位拼接在一起,所以收货人信息改用坐标(每个单词的 x 位置)按左右
两栏分组后再重建,商品明细则直接用 pdfplumber 的表格识别(该模板的表格线条很
规整,识别率高)。

用法:
    from dhl_parser import parse_dhl_invoice
    inv = parse_dhl_invoice("CustomInvoice_1017186892.pdf")
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber


@dataclass
class LineItem:
    item_no: str
    description: str
    commodity_code: str
    qty: float
    unit: str
    unit_value: float
    sub_total: float


@dataclass
class ShipToParty:
    name: str
    address_lines: list[str] = field(default_factory=list)
    city: str = ""
    country: str = ""
    phone: str = ""
    email: str = ""
    trader_type: str = ""
    vat_no: str = ""
    eori: str = ""


@dataclass
class DHLInvoice:
    awb_no: str
    invoice_date: str
    invoice_no: str
    ship_to: ShipToParty
    line_items: list[LineItem]
    currency: str
    total_goods_value: float
    total_invoice_amount: float
    reason_for_export: str
    duty_tax_account: str
    source_file: str


def _clean_amount(text: str) -> float:
    """'2,052.00 GBP' / '2,052.00\\nGBP' -> 2052.00"""
    m = re.search(r"[\d,]+\.\d{2}", text)
    if not m:
        raise ValueError(f"无法从 {text!r} 中解析出金额")
    return float(m.group(0).replace(",", ""))


def _extract_ship_to(first_page) -> ShipToParty:
    """按坐标把 SHIP FROM / SHIP TO 两栏拆开,只取右边(SHIP TO = 收货人/客户)。"""
    words = first_page.extract_words()
    mid_x = first_page.width / 2

    rows: dict[int, list[dict]] = {}
    for w in words:
        rows.setdefault(round(w["top"]), []).append(w)

    right_lines: list[str] = []
    started = False
    for top in sorted(rows.keys()):
        ws = sorted(rows[top], key=lambda w: w["x0"])
        left = " ".join(w["text"] for w in ws if w["x0"] < mid_x).strip()
        right = " ".join(w["text"] for w in ws if w["x0"] >= mid_x).strip()
        if "SHIP TO" in right or "SHIP FROM" in left:
            started = True
            continue
        if not started:
            continue
        if left.startswith("Shipper Reference") or left.startswith("Ite-"):
            break
        if right:
            right_lines.append(right)

    phone = next((l for l in right_lines if l.startswith("+")), "")
    email = next((l for l in right_lines if "@" in l), "")
    trader_type = next(
        (l.split(":", 1)[1].strip() for l in right_lines if l.startswith("Trader Type:")), ""
    )
    vat_no = next((l.split(":", 1)[1].strip() for l in right_lines if l.startswith("VAT No:")), "")
    eori = next((l.split(":", 1)[1].strip() for l in right_lines if l.startswith("EORI:")), "")

    addr_block = [
        l
        for l in right_lines
        if l != phone
        and l != email
        and not l.startswith("Trader Type:")
        and not l.startswith("VAT No:")
        and not l.startswith("EORI:")
    ]
    # DHL 模板里"联系人"和"公司/收件人名"经常连续重复一行,去重相邻重复
    dedup: list[str] = []
    for l in addr_block:
        if not dedup or dedup[-1] != l:
            dedup.append(l)

    name = dedup[0] if dedup else ""
    country = dedup[-1] if len(dedup) > 1 else ""
    address_lines = dedup[1:-1] if len(dedup) > 2 else []
    city = ""
    if address_lines and address_lines[-1].isupper():
        city = address_lines[-1]
        address_lines = address_lines[:-1]

    return ShipToParty(
        name=name,
        address_lines=address_lines,
        city=city,
        country=country,
        phone=phone,
        email=email,
        trader_type=trader_type,
        vat_no=vat_no,
        eori=eori,
    )


def _extract_line_items(pdf) -> list[LineItem]:
    line_items: list[LineItem] = []
    for page in pdf.pages:
        for table in page.extract_tables():
            if not table:
                continue
            header = [(c or "").replace("\n", " ").strip() for c in table[0]]
            if len(header) < 2 or "Description" not in header[1]:
                continue  # 不是商品明细表(比如某页没有表格)
            for row in table[1:]:
                if not row or not row[0] or not row[0].strip().isdigit():
                    continue  # 跳过表头重复行/空行
                item_no, desc, commodity, _tax_paid, _weight, _coo, _ref, qty, unit_value, sub_total = row
                qty_m = re.search(r"([\d.]+)\s*(\S+)?", (qty or "").replace("\n", " ").strip())
                line_items.append(
                    LineItem(
                        item_no=item_no.strip(),
                        description=(desc or "").replace("\n", " ").strip(),
                        commodity_code=(commodity or "").replace("\n", "").strip(),
                        qty=float(qty_m.group(1)) if qty_m else 0.0,
                        unit=(qty_m.group(2) or "").strip() if qty_m else "",
                        unit_value=_clean_amount(unit_value or ""),
                        sub_total=_clean_amount(sub_total or ""),
                    )
                )
    return line_items


def parse_dhl_invoice(pdf_path: str | Path) -> DHLInvoice:
    pdf_path = Path(pdf_path)
    with pdfplumber.open(pdf_path) as pdf:
        first_page_text = pdf.pages[0].extract_text() or ""
        # 头部信息限定在单独一行内匹配,避免 Invoice No 为空时 \s* 跨行吃掉下一行内容
        header_line = next(
            (l for l in first_page_text.splitlines() if l.startswith("AWB No:")), ""
        )
        header_m = re.search(
            r"AWB No:\s*(\S+)\s+Invoice Date:\s*(\S+)\s+Invoice No:\s*(\S*)",
            header_line,
        )
        if not header_m:
            raise ValueError(
                "无法识别 DHL 发票头部信息(AWB No / Invoice Date / Invoice No),"
                "请确认这是标准 DHL Commercial Invoice 模板"
            )
        awb_no, invoice_date, invoice_no = header_m.groups()

        ship_to = _extract_ship_to(pdf.pages[0])
        line_items = _extract_line_items(pdf)

        full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)

        def _field(label: str, default: str = "") -> str:
            m = re.search(re.escape(label) + r"\s*(.+)", full_text)
            return m.group(1).strip() if m else default

        currency_m = re.search(r"Currency Code:\s*(\S+)", full_text)
        total_goods_m = re.search(r"Total Goods Value:\s*([\d,]+\.\d{2})", full_text)
        total_invoice_m = re.search(r"Total Invoice Amount:\s*([\d,]+\.\d{2})", full_text)

        return DHLInvoice(
            awb_no=awb_no,
            invoice_date=invoice_date,
            invoice_no=invoice_no,
            ship_to=ship_to,
            line_items=line_items,
            currency=currency_m.group(1) if currency_m else "GBP",
            total_goods_value=float(total_goods_m.group(1).replace(",", "")) if total_goods_m else 0.0,
            total_invoice_amount=float(total_invoice_m.group(1).replace(",", "")) if total_invoice_m else 0.0,
            reason_for_export=_field("Reason for Export:"),
            duty_tax_account=_field("Duty / taxes acct:"),
            source_file=str(pdf_path),
        )


if __name__ == "__main__":
    import sys

    for path in sys.argv[1:]:
        result = parse_dhl_invoice(path)
        print(result)
