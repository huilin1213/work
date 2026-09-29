"""按"计划文件"批量整理 Xero 里的销售发票:作废旧发票 → 重开新发票 → 登记收款 → 单独登记预付款/多付款。

适用场景:之前的发票和 DHL 报关单、银行收款对不上,需要按对账结果整体重做一遍。
所有要改的内容(作废哪些旧发票、新发票的逐行明细、每笔收款付给哪张发票、多出来
的钱记成预付款还是多付款)都写在一个 JSON 计划文件里,脚本只负责照着执行。
计划文件包含客户姓名、金额等敏感信息,放在 plans/ 目录下,已加入 .gitignore。

用法(需要先用新 scope 重新跑过一次 xero_auth.py):
    python3 rebuild_invoices.py plans/aug2026.json validate        # 只检查计划文件本身,不连 Xero
    python3 rebuild_invoices.py plans/aug2026.json check           # 连 Xero 只读检查:旧发票、新发票号、清算账户
    python3 rebuild_invoices.py plans/aug2026.json void-old        # 预览:要删除的收款和要作废的旧发票
    python3 rebuild_invoices.py plans/aug2026.json void-old --execute
    python3 rebuild_invoices.py plans/aug2026.json create-new --execute   # 建 DRAFT 新发票
    #   ↑ 然后去 Xero 网页版逐张核对并 Approve(脚本不会自动过账,和 main.py 保持一致)
    python3 rebuild_invoices.py plans/aug2026.json payments --execute     # 登记收款 + 预付款/多付款

每一步都默认只预览(dry run),加 --execute 才真正写入 Xero;重复执行是安全的,
已经做过的会自动跳过(按发票号 / 收款 Reference 查重)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from xero_client import (
    create_bank_transaction,
    create_invoice,
    create_payment,
    delete_payment,
    find_bank_transactions_by_reference,
    find_invoice_by_reference,
    find_payments_by_reference,
    get_account_by_code,
    get_invoice_by_number,
    void_invoice,
)

DEAD_STATUSES = {"VOIDED", "DELETED"}


# ---------------------------------------------------------------------------
# 计划文件校验(纯本地,不连 Xero)
# ---------------------------------------------------------------------------

def load_plan(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def invoice_total(inv: dict) -> float:
    return round(sum(li["quantity"] * li["unit_amount"] for li in inv["lines"]), 2)


def validate(plan: dict) -> list[str]:
    """返回问题列表;空列表表示计划文件内部自洽。"""
    problems: list[str] = []
    invoices = {inv["number"]: inv for inv in plan["invoices"]}

    for inv in plan["invoices"]:
        expected = inv.get("expected_total")
        if expected is not None and abs(invoice_total(inv) - expected) > 0.005:
            problems.append(f"{inv['number']}: 逐行合计 {invoice_total(inv)} ≠ expected_total {expected}")
        units = sum(li["quantity"] for li in inv["lines"])
        if inv.get("expected_units") is not None and units != inv["expected_units"]:
            problems.append(f"{inv['number']}: 数量合计 {units} ≠ expected_units {inv['expected_units']}")

    refs = [r["reference"] for r in plan["receipts"]]
    if len(refs) != len(set(refs)):
        problems.append("receipts 里的 reference 有重复,查重会出错")

    paid: dict[str, float] = {n: 0.0 for n in invoices}
    for r in plan["receipts"]:
        if r["clearing"] not in plan["clearing_accounts"]:
            problems.append(f"{r['reference']}: clearing={r['clearing']} 不在 clearing_accounts 里")
        allocated = sum(a["amount"] for a in r.get("allocations", []))
        extra = r.get("prepayment", 0) + r.get("overpayment", 0)
        if abs(allocated + extra - r["amount"]) > 0.005:
            problems.append(
                f"{r['reference']}: 分配 {allocated:.2f} + 预付/多付 {extra:.2f} ≠ 收款金额 {r['amount']:.2f}"
            )
        for a in r.get("allocations", []):
            if a["invoice"] not in invoices:
                problems.append(f"{r['reference']}: 分配到了计划里不存在的发票 {a['invoice']}")
            else:
                paid[a["invoice"]] += a["amount"]

    for number, amount in paid.items():
        if amount - invoice_total(invoices[number]) > 0.005:
            problems.append(f"{number}: 分配的收款 {amount:.2f} 超过发票金额 {invoice_total(invoices[number]):.2f}")
    return problems


def print_summary(plan: dict) -> None:
    print("新发票:")
    for inv in plan["invoices"]:
        units = sum(li["quantity"] for li in inv["lines"])
        print(f"  {inv['number']}  {inv['date']}  {inv['reference']:24s}  {units:>4} units  £{invoice_total(inv):>10,.2f}")
    print("收款分配:")
    for r in plan["receipts"]:
        parts = [f"{a['invoice']} £{a['amount']:,.2f}" for a in r.get("allocations", [])]
        if r.get("overpayment"):
            parts.append(f"多付款 £{r['overpayment']:,.2f}")
        if r.get("prepayment"):
            parts.append(f"预付款 £{r['prepayment']:,.2f}")
        print(f"  {r['date']}  {r['reference']:28s} £{r['amount']:>10,.2f}  → {' + '.join(parts)}")


# ---------------------------------------------------------------------------
# 各个步骤
# ---------------------------------------------------------------------------

def step_check(plan: dict) -> None:
    print("== 旧发票 ==")
    for number in plan["void_invoices"]:
        inv = get_invoice_by_number(number)
        if not inv:
            print(f"  {number}: Xero 里找不到")
            continue
        payments = [p for p in inv.get("Payments", []) if p.get("Status") != "DELETED"]
        print(
            f"  {number}: {inv['Status']}  {inv.get('Reference', '')}  合计 {inv.get('Total')}  "
            f"已付 {inv.get('AmountPaid')}  收款 {len(payments)} 笔"
        )
        if inv.get("Prepayments") or inv.get("Overpayments") or inv.get("CreditNotes"):
            print("    ⚠️  这张发票还分配了预付款/多付款/贷项通知单,作废前需要先在网页版取消分配")

    print("== 新发票号 ==")
    for new in plan["invoices"]:
        inv = get_invoice_by_number(new["number"])
        if not inv or inv["Status"] == "DELETED":
            state = "可用"
        elif inv.get("Reference") == new["reference"] and inv["Status"] not in DEAD_STATUSES:
            state = f"已建过 ({inv['Status']})"
        else:
            state = f"⛔ 号码被占用 ({inv['Status']}, {inv.get('Reference', '')}),需要换号"
        print(f"  {new['number']}: {state}")

    print("== 清算账户 ==")
    for name, code in plan["clearing_accounts"].items():
        acct = get_account_by_code(code)
        if not acct:
            print(f"  {name} ({code}): ❌ 找不到这个科目代码")
        elif acct.get("Type") != "BANK":
            print(f"  {name} ({code}): ⚠️  {acct['Name']} 类型是 {acct.get('Type')},登记预付款/多付款需要 BANK 类型账户")
        else:
            print(f"  {name} ({code}): ✅ {acct['Name']} (BANK)")


def step_void_old(plan: dict, execute: bool) -> None:
    for number in plan["void_invoices"]:
        inv = get_invoice_by_number(number)
        if not inv:
            print(f"⏭  {number}: Xero 里找不到,跳过")
            continue
        if inv["Status"] in DEAD_STATUSES:
            print(f"⏭  {number}: 已经是 {inv['Status']},跳过")
            continue
        payments = [p for p in inv.get("Payments", []) if p.get("Status") != "DELETED"]
        print(f"{'▶' if execute else '👀'} {number} ({inv.get('Reference', '')}, 合计 {inv.get('Total')}):")
        for p in payments:
            print(f"    删除收款 £{p.get('Amount')}  PaymentID={p['PaymentID']}")
            if execute:
                delete_payment(p["PaymentID"])
        print("    作废发票 (VOIDED)")
        if execute:
            try:
                void_invoice(inv["InvoiceID"])
            except RuntimeError as exc:
                print(f"    ❌ 作废失败: {exc}")
                continue
            print("    ✅ 完成")


def build_new_invoice_payload(plan: dict, inv: dict) -> dict:
    return {
        "Type": "ACCREC",
        "Contact": plan["contact"],
        "InvoiceNumber": inv["number"],
        "Reference": inv["reference"],
        "Date": inv["date"],
        "DueDate": inv["due_date"],
        "CurrencyCode": plan["currency"],
        "LineAmountTypes": "Exclusive",
        "Status": "DRAFT",  # 故意固定为草稿:核对后由人在 Xero 网页版 Approve
        "LineItems": [
            {
                "Description": li["description"],
                "Quantity": li["quantity"],
                "UnitAmount": li["unit_amount"],
                "AccountCode": plan["account_code"],
                "TaxType": plan["tax_type"],
            }
            for li in inv["lines"]
        ],
    }


def step_create_new(plan: dict, execute: bool) -> None:
    created_any = False
    for inv in plan["invoices"]:
        existing = get_invoice_by_number(inv["number"])
        if existing and existing["Status"] != "DELETED":  # 删掉的草稿从没开出去,号码可以复用
            same = existing.get("Reference") == inv["reference"] and existing["Status"] not in DEAD_STATUSES
            if same:
                print(f"⏭  {inv['number']}: 已建过 ({existing['Status']}),跳过")
            else:
                # 号码被别的发票占用(哪怕是作废的):不能跳过,否则这张发票会被悄悄漏掉
                print(
                    f"⛔ {inv['number']}: 发票号已被另一张发票占用 "
                    f"({existing['Status']}, {existing.get('Reference', '')}),请在计划文件里换一个号"
                )
            continue
        clash = find_invoice_by_reference(inv["reference"])
        if clash:
            print(
                f"⛔ {inv['number']}: 还有一张未作废的发票 {clash.get('InvoiceNumber')} 用着同一个 "
                f"Reference「{inv['reference']}」,先跑 void-old 再建新的"
            )
            continue
        print(f"{'▶' if execute else '👀'} 新建 DRAFT {inv['number']}  {inv['reference']}  £{invoice_total(inv):,.2f}")
        if execute:
            created = create_invoice(build_new_invoice_payload(plan, inv))
            print(f"    ✅ InvoiceID={created.get('InvoiceID')} 状态={created.get('Status')}")
            created_any = True
    if created_any:
        print("\n下一步:去 Xero 网页版逐张核对这些草稿发票并 Approve,之后再跑 payments。")


def step_payments(plan: dict, execute: bool) -> None:
    invoice_ids: dict[str, str] = {}
    not_ready = []
    for inv in plan["invoices"]:
        found = get_invoice_by_number(inv["number"])
        if not found or found["Status"] not in ("AUTHORISED", "PAID"):
            not_ready.append(f"{inv['number']} ({found['Status'] if found else '不存在'})")
        else:
            invoice_ids[inv["number"]] = found["InvoiceID"]
    if not_ready:
        print("⛔ 以下发票还没 Approve,Xero 不允许给草稿发票登记收款:")
        for n in not_ready:
            print(f"    {n}")
        print("   先去 Xero 网页版核对并 Approve,再重新运行这一步。")
        return

    for r in plan["receipts"]:
        account = plan["clearing_accounts"][r["clearing"]]
        for i, a in enumerate(r.get("allocations", []), 1):
            ref = f"{r['reference']} #{i}"
            if find_payments_by_reference(ref):
                print(f"⏭  收款 {ref}: 已登记,跳过")
                continue
            print(f"{'▶' if execute else '👀'} 收款 {r['date']} {ref}: £{a['amount']:,.2f} → {a['invoice']} (账户 {account})")
            if execute:
                create_payment(invoice_ids[a["invoice"]], account, r["date"], a["amount"], ref)
        for kind, txn_type in (("prepayment", "RECEIVE-PREPAYMENT"), ("overpayment", "RECEIVE-OVERPAYMENT")):
            amount = r.get(kind, 0)
            if not amount:
                continue
            ref = f"{r['reference']} {kind}"
            if find_bank_transactions_by_reference(ref):
                print(f"⏭  {kind} {ref}: 已登记,跳过")
                continue
            label = "预付款" if kind == "prepayment" else "多付款"
            print(f"{'▶' if execute else '👀'} {label} {r['date']} {ref}: £{amount:,.2f} (账户 {account})")
            if not execute:
                continue
            if kind == "prepayment":
                line = {
                    "Description": r.get("note") or "Customer prepayment",
                    "Quantity": 1,
                    "UnitAmount": amount,
                    "AccountCode": plan["account_code"],
                    "TaxType": plan["tax_type"],
                }
            else:
                # 多付款在 Xero 里直接挂在客户应收上,行项目不带科目代码
                line = {"Description": r.get("note") or "Customer overpayment", "LineAmount": amount}
            create_bank_transaction({
                "Type": txn_type,
                "Contact": {"Name": plan["contact"]["Name"]},
                "BankAccount": {"Code": account},
                "Date": r["date"],
                "Reference": ref,
                "CurrencyCode": plan["currency"],
                "LineAmountTypes": "NoTax" if kind == "overpayment" else "Exclusive",
                "LineItems": [line],
            })
    if execute:
        print("\n完成。预付款/多付款会挂在客户名下,以后开新发票时在网页版 Allocate 抵扣。")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("plan", help="计划文件路径 (JSON)")
    parser.add_argument("step", choices=["validate", "check", "void-old", "create-new", "payments"])
    parser.add_argument("--execute", action="store_true", help="真正写入 Xero;不加则只预览")
    args = parser.parse_args()

    plan = load_plan(args.plan)
    problems = validate(plan)
    if problems:
        print("❌ 计划文件有问题,已中止:")
        for p in problems:
            print(f"   - {p}")
        sys.exit(1)

    if args.step == "validate":
        print("✅ 计划文件自洽\n")
        print_summary(plan)
        return
    placeholders = [k for k, v in plan["clearing_accounts"].items() if v == "CHANGE-ME"]
    if placeholders:
        raise SystemExit(
            f"计划文件里 clearing_accounts 的 {', '.join(placeholders)} 还是 CHANGE-ME,"
            "请先在 Xero 建好清算用的银行账户,把科目代码填进去"
        )
    if args.step == "check":
        step_check(plan)
        return

    if args.execute and args.step == "void-old":
        answer = input(f"即将删除收款并作废 {', '.join(plan['void_invoices'])},确认请输入 YES: ")
        if answer.strip() != "YES":
            raise SystemExit("已取消")
    {"void-old": step_void_old, "create-new": step_create_new, "payments": step_payments}[args.step](
        plan, args.execute
    )
    if not args.execute:
        print("\n(以上只是预览,没有写入 Xero。确认无误后加 --execute 再运行一次)")


if __name__ == "__main__":
    main()
