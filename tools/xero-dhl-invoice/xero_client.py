"""Xero API 的最小封装:token 存取/自动刷新 + 创建发票 + 按 Reference 查重。

不用官方 SDK,只用 requests 直接调 REST API,方便你直接看懂/改动每一步在做什么。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN_URL = "https://identity.xero.com/connect/token"
API_BASE = "https://api.xero.com/api.xro/2.0"
TOKEN_FILE = Path(__file__).parent / ".xero_tokens.json"


def save_tokens(tokens: dict, tenant_id: str) -> None:
    data = {**tokens, "tenant_id": tenant_id, "obtained_at": time.time()}
    TOKEN_FILE.write_text(json.dumps(data, indent=2))
    try:
        TOKEN_FILE.chmod(0o600)  # 只有当前用户可读,里面是 refresh token
    except OSError:
        pass  # Windows 等不支持 chmod 的平台忽略


def _load_tokens() -> dict:
    if not TOKEN_FILE.exists():
        raise SystemExit("还没有做过 Xero 授权,请先运行: python xero_auth.py")
    return json.loads(TOKEN_FILE.read_text())


def get_access_token() -> tuple[str, str]:
    """返回 (access_token, tenant_id);access token 过期了就自动用 refresh token 换新的。"""
    tokens = _load_tokens()
    expires_in = tokens.get("expires_in", 1800)
    obtained_at = tokens.get("obtained_at", 0)
    if time.time() < obtained_at + expires_in - 60:
        return tokens["access_token"], tokens["tenant_id"]

    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": os.environ["XERO_CLIENT_ID"],
            "refresh_token": tokens["refresh_token"],
        },
        timeout=30,
    )
    if not resp.ok:
        raise SystemExit(
            f"刷新 token 失败 ({resp.status_code}): {resp.text}\n"
            "如果 refresh token 已经过期(超过 60 天没运行过),请重新运行 python xero_auth.py"
        )
    new_tokens = resp.json()
    save_tokens(new_tokens, tokens["tenant_id"])
    return new_tokens["access_token"], tokens["tenant_id"]


def _headers() -> dict:
    access_token, tenant_id = get_access_token()
    return {
        "Authorization": f"Bearer {access_token}",
        "Xero-tenant-id": tenant_id,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def find_invoice_by_reference(reference: str) -> dict | None:
    """按 Reference(这里用 'DHL AWB <运单号>')查重,避免同一票货重复开票。

    Xero 里"删除"/"作废"一张发票并不是真的抹掉记录,而是把 Status 标成
    DELETED / VOIDED,记录还查得到——所以这两种状态要排除掉,不然哪怕你在
    Xero 网页上删了这张发票,脚本还是会以为它存在而跳过、不重新生成。
    """
    resp = requests.get(
        f"{API_BASE}/Invoices",
        headers=_headers(),
        params={"where": f'Reference=="{reference}"&&Status!="DELETED"&&Status!="VOIDED"'},
        timeout=30,
    )
    resp.raise_for_status()
    invoices = resp.json().get("Invoices", [])
    return invoices[0] if invoices else None


def create_invoice(payload: dict) -> dict:
    resp = requests.post(
        f"{API_BASE}/Invoices",
        headers=_headers(),
        json={"Invoices": [payload]},
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"Xero 拒绝了这张发票 ({resp.status_code}): {resp.text}")
    body = resp.json()
    invoices = body.get("Invoices", [])
    if not invoices:
        raise RuntimeError(f"Xero 返回了 200 但没有发票数据,原始响应: {body}")
    invoice = invoices[0]
    warnings = invoice.get("Warnings") or []
    if warnings:
        print("   Xero 返回了以下提示,建议开票后去网页版确认一下:")
        for w in warnings:
            print(f"    - {w.get('Message')}")
    return invoice


# ---------------------------------------------------------------------------
# 下面这些是 rebuild_invoices.py(按计划文件作废/重开发票、登记收款和预付款)
# 用到的接口。统一走 _get/_send,出错时把 Xero 的原始报错带出来,方便排查。
# ---------------------------------------------------------------------------

def _get(path: str, params: dict | None = None) -> dict:
    resp = requests.get(f"{API_BASE}/{path}", headers=_headers(), params=params, timeout=30)
    if not resp.ok:
        raise RuntimeError(f"GET {path} 失败 ({resp.status_code}): {resp.text}")
    return resp.json()


def _send(method: str, path: str, body: dict) -> dict:
    resp = requests.request(method, f"{API_BASE}/{path}", headers=_headers(), json=body, timeout=30)
    if not resp.ok:
        raise RuntimeError(f"{method} {path} 失败 ({resp.status_code}): {resp.text}")
    return resp.json()


def get_invoice_by_number(invoice_number: str) -> dict | None:
    """按发票号取完整发票(含 Payments 列表)。VOIDED/DELETED 的也会返回,调用方自己判断。"""
    found = _get("Invoices", {"InvoiceNumbers": invoice_number}).get("Invoices", [])
    if not found:
        return None
    # 列表接口不一定带全 Payments,再按 ID 取一次详情
    return _get(f"Invoices/{found[0]['InvoiceID']}")["Invoices"][0]


def delete_payment(payment_id: str) -> None:
    """Xero 里"删除收款"就是把 Payment 的 Status 改成 DELETED(已对账的收款会被拒绝)。"""
    _send("POST", f"Payments/{payment_id}", {"Status": "DELETED"})


def void_invoice(invoice_id: str) -> dict:
    body = {"Invoices": [{"InvoiceID": invoice_id, "Status": "VOIDED"}]}
    return _send("POST", "Invoices", body)["Invoices"][0]


def create_payment(invoice_id: str, account_code: str, date: str, amount: float, reference: str) -> dict:
    body = {"Payments": [{
        "Invoice": {"InvoiceID": invoice_id},
        "Account": {"Code": account_code},
        "Date": date,
        "Amount": round(amount, 2),
        "Reference": reference,
    }]}
    return _send("PUT", "Payments", body)["Payments"][0]


def find_payments_by_reference(reference: str) -> list[dict]:
    where = f'Reference=="{reference}"&&Status!="DELETED"'
    return _get("Payments", {"where": where}).get("Payments", [])


def create_bank_transaction(txn: dict) -> dict:
    return _send("PUT", "BankTransactions", {"BankTransactions": [txn]})["BankTransactions"][0]


def find_bank_transactions_by_reference(reference: str) -> list[dict]:
    where = f'Reference=="{reference}"&&Status!="DELETED"'
    return _get("BankTransactions", {"where": where}).get("BankTransactions", [])


def get_account_by_code(code: str) -> dict | None:
    accounts = _get("Accounts", {"where": f'Code=="{code}"'}).get("Accounts", [])
    return accounts[0] if accounts else None
