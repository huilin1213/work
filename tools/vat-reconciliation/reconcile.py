"""核对「收到的 VAT 发票」和「银行实际支出」,产出一份 Excel 核对表。

用法:
    python3 reconcile.py --ledger 发票登记表.xlsx \
        --bank REVOLUT_AUG.xlsx:revolut \
        --bank hsbc_debit_aug.csv:hsbc:"HSBC Debit" \
        --bank hsbc_credit_aug.csv:hsbc:"HSBC Credit" \
        --out 八月VAT对账结果.xlsx

--bank 参数格式是 "文件路径:类型[:账户显示名]":
    类型填 revolut 或 hsbc(hsbc 现在是通用猜测版解析器,见 bank_parsers.py 顶部说明)
    账户显示名可省略,省略时 revolut 显示成 "Revolut",hsbc 用文件名当显示名

产出的 Excel 有 5 个 sheet:
    汇总                  —— 整体统计数字
    已匹配                —— 发票和银行支出配对成功,附 VAT 金额
    发票未匹配银行流水      —— 登记表里有这张发票,但银行流水里没找到对应支出
                             (常见原因:用了另一张卡付款,还没导入那张卡的流水)
    待催发票-eBay订单       —— 银行流水里能看出是 eBay 订单支出,但登记表里还没有
                             对应发票,按订单号自动生成一句可以直接发给卖家的话术
    待催发票-其他           —— 其它没对上发票的支出,人工确认是否需要发票

匹配逻辑(按优先级):
    1. eBay 订单号完全一致(发票上的 "Order:" 号码 == 银行流水描述里的订单号)
       —— 这是最可靠的依据,金额如果对不上也仍然按订单号配对,但会在备注里标红提醒
    2. 供应商名字模糊匹配 + 金额一致(容许 --amount-tolerance,默认 0.02 英镑,
       给 Revolut 多币种换算尾差留余地)+ 日期相差在 --date-window 天以内
       (默认 10 天,银行入账、eBay 走款有几天延迟很正常)
"""
from __future__ import annotations

import argparse
import difflib
from dataclasses import dataclass

import openpyxl
from openpyxl.styles import Font, PatternFill

from bank_parsers import BankTxn, load_hsbc_csv, load_revolut_export
from invoice_ledger import InvoiceRecord, load_ledger

AMOUNT_TOLERANCE_DEFAULT = 0.02
DATE_WINDOW_DEFAULT = 10
VENDOR_SIMILARITY_THRESHOLD = 0.35

HEADER_FILL = PatternFill("solid", fgColor="DDEBF7")
WARN_FILL = PatternFill("solid", fgColor="FFF2CC")
BOLD = Font(bold=True)


@dataclass
class MatchResult:
    invoice: InvoiceRecord
    txn: BankTxn
    matched_by: str  # "eBay订单号" / "供应商名+金额+日期"
    amount_mismatch: bool


def _vendor_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def match(
    invoices: list[InvoiceRecord],
    bank_txns: list[BankTxn],
    amount_tolerance: float = AMOUNT_TOLERANCE_DEFAULT,
    date_window: int = DATE_WINDOW_DEFAULT,
) -> tuple[list[MatchResult], list[InvoiceRecord], list[BankTxn]]:
    remaining_txns = list(bank_txns)
    matches: list[MatchResult] = []
    unmatched_invoices: list[InvoiceRecord] = []

    # 第一步:按 eBay 订单号精确匹配
    for inv in invoices:
        if not inv.ebay_order_no:
            continue
        candidates = [t for t in remaining_txns if t.ebay_order_no == inv.ebay_order_no and t.amount < 0]
        if not candidates:
            continue
        # 同一个订单号一般只会有一笔支出;如果有多笔,挑金额最接近的那笔,其余留给下一步/未匹配
        best = min(candidates, key=lambda t: abs(abs(t.amount) - inv.gross_total))
        remaining_txns.remove(best)
        mismatch = abs(abs(best.amount) - inv.gross_total) > amount_tolerance
        matches.append(MatchResult(inv, best, "eBay订单号", mismatch))

    matched_invoice_ids = {id(m.invoice) for m in matches}

    # 第二步:剩下没按订单号配对上的发票,按"供应商名字 + 金额 + 日期"模糊匹配
    for inv in invoices:
        if id(inv) in matched_invoice_ids:
            continue
        best_candidate = None
        best_score = 0.0
        for t in remaining_txns:
            if t.amount >= 0:
                continue
            if abs(abs(t.amount) - inv.gross_total) > amount_tolerance:
                continue
            if abs((t.txn_date - inv.invoice_date).days) > date_window:
                continue
            score = _vendor_similarity(inv.vendor, t.description)
            if score > best_score:
                best_score = score
                best_candidate = t
        if best_candidate is not None and best_score >= VENDOR_SIMILARITY_THRESHOLD:
            remaining_txns.remove(best_candidate)
            matches.append(MatchResult(inv, best_candidate, "供应商名+金额+日期", False))
        else:
            unmatched_invoices.append(inv)

    return matches, unmatched_invoices, remaining_txns


def build_chase_message(order_no: str) -> str:
    return (
        f"Hi, could you please send me a VAT invoice for order {order_no}? "
        f"Thank you!"
    )


def write_report(
    out_path: str,
    matches: list[MatchResult],
    unmatched_invoices: list[InvoiceRecord],
    unmatched_txns: list[BankTxn],
) -> None:
    wb = openpyxl.Workbook()

    # ---- 已匹配 ----
    ws = wb.active
    ws.title = "已匹配"
    headers = ["发票日期", "供应商", "含税总额", "VAT金额", "匹配依据", "银行账户", "银行交易日期", "银行金额", "金额一致", "eBay订单号", "发票文件名", "登记表位置", "银行流水位置"]
    ws.append(headers)
    for cell in ws[1]:
        cell.fill = HEADER_FILL
        cell.font = BOLD
    for m in sorted(matches, key=lambda m: m.invoice.invoice_date):
        row = [
            m.invoice.invoice_date, m.invoice.vendor, m.invoice.gross_total, m.invoice.vat_amount,
            m.matched_by, m.txn.account, m.txn.txn_date, m.txn.amount,
            "否" if m.amount_mismatch else "是",
            m.invoice.ebay_order_no or "", m.invoice.source_file,
            m.invoice.row_ref, m.txn.row_ref,
        ]
        ws.append(row)
        if m.amount_mismatch:
            for cell in ws[ws.max_row]:
                cell.fill = WARN_FILL

    # ---- 发票未匹配银行流水 ----
    ws2 = wb.create_sheet("发票未匹配银行流水")
    ws2.append(["发票日期", "供应商", "含税总额", "VAT金额", "eBay订单号", "发票文件名", "提示", "登记表位置"])
    for cell in ws2[1]:
        cell.fill = HEADER_FILL
        cell.font = BOLD
    for inv in sorted(unmatched_invoices, key=lambda i: i.invoice_date):
        ws2.append([
            inv.invoice_date, inv.vendor, inv.gross_total, inv.vat_amount,
            inv.ebay_order_no or "", inv.source_file,
            "银行流水里没找到对应支出,检查是不是用另一张卡付的款(还没导入那张卡的流水),或者金额/日期填错了",
            inv.row_ref,
        ])

    # ---- 待催发票:eBay 订单 ----
    ebay_unmatched = [t for t in unmatched_txns if t.is_candidate_expense() and t.ebay_order_no]
    other_unmatched = [t for t in unmatched_txns if t.is_candidate_expense() and not t.ebay_order_no]

    ws3 = wb.create_sheet("待催发票-eBay订单")
    ws3.append(["银行账户", "交易日期", "金额", "eBay订单号", "建议发给卖家的话术", "银行流水位置"])
    for cell in ws3[1]:
        cell.fill = HEADER_FILL
        cell.font = BOLD
    for t in sorted(ebay_unmatched, key=lambda t: t.txn_date):
        ws3.append([t.account, t.txn_date, t.amount, t.ebay_order_no, build_chase_message(t.ebay_order_no), t.row_ref])

    # ---- 待催发票:其他 ----
    ws4 = wb.create_sheet("待催发票-其他")
    ws4.append(["银行账户", "交易日期", "描述", "金额", "银行流水位置"])
    for cell in ws4[1]:
        cell.fill = HEADER_FILL
        cell.font = BOLD
    for t in sorted(other_unmatched, key=lambda t: t.txn_date):
        ws4.append([t.account, t.txn_date, t.description, t.amount, t.row_ref])

    # ---- 汇总 ----
    ws0 = wb.create_sheet("汇总", 0)
    total_vat = sum(m.invoice.vat_amount for m in matches)
    mismatched = [m for m in matches if m.amount_mismatch]
    rows = [
        ("已匹配发票数", len(matches)),
        ("已匹配发票的 VAT 合计 (£)", round(total_vat, 2)),
        ("其中金额对不上、需要人工核对", len(mismatched)),
        ("发票有但银行流水没对上", len(unmatched_invoices)),
        ("待催发票 - eBay 订单笔数", len(ebay_unmatched)),
        ("待催发票 - eBay 订单金额合计 (£)", round(sum(abs(t.amount) for t in ebay_unmatched), 2)),
        ("待催发票 - 其他支出笔数(人工确认是否需要发票)", len(other_unmatched)),
    ]
    ws0.append(["项目", "数值"])
    for cell in ws0[1]:
        cell.fill = HEADER_FILL
        cell.font = BOLD
    for r in rows:
        ws0.append(r)
    ws0.column_dimensions["A"].width = 42
    ws0.column_dimensions["B"].width = 16

    for sheet in wb.worksheets:
        for col_cells in sheet.columns:
            try:
                width = max(len(str(c.value)) for c in col_cells if c.value is not None)
            except ValueError:
                continue
            sheet.column_dimensions[col_cells[0].column_letter].width = min(60, max(10, width + 2))

    wb.save(out_path)


def _load_bank_spec(spec: str) -> list[BankTxn]:
    parts = spec.split(":")
    if len(parts) < 2:
        raise SystemExit(f"--bank 参数格式不对: {spec!r},应该是 '文件路径:revolut' 或 '文件路径:hsbc:账户显示名'")
    path, kind = parts[0], parts[1].lower()
    label = parts[2] if len(parts) > 2 else None
    if kind == "revolut":
        return load_revolut_export(path, label or "Revolut")
    if kind == "hsbc":
        if not label:
            raise SystemExit(f"--bank {spec!r}: hsbc 类型请指定账户显示名,比如 '文件路径:hsbc:HSBC Debit'")
        return load_hsbc_csv(path, label)
    raise SystemExit(f"--bank 参数里不认识的类型: {kind!r},只支持 revolut / hsbc")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ledger", required=True, help="发票登记表 xlsx 路径")
    ap.add_argument("--bank", action="append", required=True, help="银行流水,格式 '路径:revolut' 或 '路径:hsbc:账户显示名',可以传多次")
    ap.add_argument("--out", default="VAT对账结果.xlsx", help="输出的核对表路径")
    ap.add_argument("--amount-tolerance", type=float, default=AMOUNT_TOLERANCE_DEFAULT, help="模糊匹配时金额允许的误差(英镑)")
    ap.add_argument("--date-window", type=int, default=DATE_WINDOW_DEFAULT, help="模糊匹配时日期允许相差的天数")
    args = ap.parse_args()

    invoices = load_ledger(args.ledger)
    bank_txns: list[BankTxn] = []
    for spec in args.bank:
        bank_txns.extend(_load_bank_spec(spec))

    matches, unmatched_invoices, remaining_txns = match(
        invoices, bank_txns, amount_tolerance=args.amount_tolerance, date_window=args.date_window
    )
    write_report(args.out, matches, unmatched_invoices, remaining_txns)

    print(f"✅ 核对完成: {len(matches)} 张发票匹配成功,{len(unmatched_invoices)} 张发票没找到对应支出")
    ebay_chase = sum(1 for t in remaining_txns if t.is_candidate_expense() and t.ebay_order_no)
    print(f"   {ebay_chase} 笔 eBay 支出还没有发票,已列在「待催发票-eBay订单」sheet 里")
    print(f"   结果已保存到: {args.out}")


if __name__ == "__main__":
    main()
