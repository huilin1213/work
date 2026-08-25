"""从 DHL 海关发票 PDF 批量生成 Xero 草稿发票。

用法:
    python main.py path/to/CustomInvoice_*.pdf [more.pdf ...]

每个 PDF 会:
  1. 解析出收货人信息和逐行商品明细
  2. 校验:逐行金额合计是否等于发票 Total Invoice Amount(不一致会提示,但不会中止)
  3. 按 "DHL AWB <运单号>" 在 Xero 里查重,已经开过的会跳过,避免重复开票
  4. 在 Xero 创建一张 Status=DRAFT 的发票并打印发票号 —— 草稿状态需要你自己登录
     Xero 网页版做最终核对后再发送/过账,脚本不会自动帮你发送。
"""
from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

from dhl_parser import DHLInvoice, parse_dhl_invoice
from xero_client import create_invoice, find_invoice_by_reference

load_dotenv()

# 这几个值没有"标准答案",在 Xero 网页版里对应查:
#   Account Code  -> Accounting > Advanced > Chart of Accounts,找你出口销售用的科目代码
#   Tax Type      -> Settings > Invoice settings > Tax rates,里面列出的 TaxType 名字
ACCOUNT_CODE = os.environ.get("XERO_ACCOUNT_CODE", "200")
TAX_TYPE = os.environ.get("XERO_TAX_TYPE", "NONE")
INVOICE_STATUS = os.environ.get("XERO_INVOICE_STATUS", "DRAFT")


def build_invoice_payload(inv: DHLInvoice) -> dict:
    reference = f"DHL AWB {inv.awb_no}"
    line_items = [
        {
            # Item 列填 DHL 发票上的原始行号,方便跟 DHL PDF 对照着核对
            "ItemCode": li.item_no,
            "Description": f"{li.description} ({li.commodity_code})",
            "Quantity": li.qty,
            "UnitAmount": li.unit_value,
            "AccountCode": ACCOUNT_CODE,
            "TaxType": TAX_TYPE,
        }
        for li in inv.line_items
    ]
    address = {
        "AddressType": "STREET",
        "AddressLine1": inv.ship_to.address_lines[0] if inv.ship_to.address_lines else "",
        "AddressLine2": inv.ship_to.address_lines[1] if len(inv.ship_to.address_lines) > 1 else "",
        "City": inv.ship_to.city,
        "Country": inv.ship_to.country,
    }
    contact: dict = {"Name": inv.ship_to.name, "Addresses": [address]}
    if inv.ship_to.email:
        contact["EmailAddress"] = inv.ship_to.email
    if inv.ship_to.phone:
        contact["Phones"] = [{"PhoneType": "MOBILE", "PhoneNumber": inv.ship_to.phone}]

    return {
        "Type": "ACCREC",
        "Contact": contact,
        "Date": inv.invoice_date,
        "Reference": reference,
        "CurrencyCode": inv.currency,
        "LineItems": line_items,
        "Status": INVOICE_STATUS,
    }


def process_one(pdf_path: str) -> None:
    print(f"\n=== 处理 {pdf_path} ===")
    inv = parse_dhl_invoice(pdf_path)

    calc_total = round(sum(li.sub_total for li in inv.line_items), 2)
    if abs(calc_total - inv.total_invoice_amount) > 0.01:
        print(
            f"⚠️  逐行金额合计 {calc_total} 与发票 Total Invoice Amount "
            f"{inv.total_invoice_amount} 不一致,建议人工检查这份 PDF 再继续"
        )

    reference = f"DHL AWB {inv.awb_no}"
    existing = find_invoice_by_reference(reference)
    if existing:
        print(f"⏭  跳过:Xero 里已存在 Reference={reference} 的发票({existing.get('InvoiceNumber')}),不重复创建")
        return

    payload = build_invoice_payload(inv)
    print(
        f"客户:{inv.ship_to.name} | 商品行数:{len(inv.line_items)} | "
        f"金额:{inv.total_invoice_amount} {inv.currency}"
    )

    try:
        created = create_invoice(payload)
    except RuntimeError as exc:
        # 有些 Xero 账套要求 ItemCode 必须先在 Items 主数据里注册过,不接受
        # 任意数字当 Item 代码;这种情况下自动去掉 ItemCode 重试一次,
        # 保证发票还是能建出来(只是 Item 列会是空的)。
        if "Item code" in str(exc) or "item code" in str(exc):
            print("⚠️  这个账套不接受任意 ItemCode,自动去掉 Item 列重试一次...")
            for li in payload["LineItems"]:
                li.pop("ItemCode", None)
            created = create_invoice(payload)
        else:
            raise

    print(f"✅ 已创建 {INVOICE_STATUS} 发票:{created.get('InvoiceNumber')} (InvoiceID={created.get('InvoiceID')})")
    print("   登录 Xero 网页版搜这个编号就能看到,核对无误后再手动发送/过账。")


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(f"用法: python {sys.argv[0]} <DHL发票PDF路径> [更多PDF...]")
    for path in sys.argv[1:]:
        try:
            process_one(path)
        except Exception as exc:  # noqa: BLE001 - 批量处理时单个文件出错不影响其他文件
            print(f"❌ 处理 {path} 失败: {exc}")


if __name__ == "__main__":
    main()
