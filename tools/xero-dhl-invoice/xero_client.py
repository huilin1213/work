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
    """按 Reference(这里用 'DHL AWB <运单号>')查重,避免同一票货重复开票。"""
    resp = requests.get(
        f"{API_BASE}/Invoices",
        headers=_headers(),
        params={"where": f'Reference=="{reference}"'},
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
