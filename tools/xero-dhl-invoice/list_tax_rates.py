"""列出当前 Xero 账套里所有可用的税率,以及开票要填的 TaxType 代码。

网页版界面上只显示税率的"名字"(比如 "Zero Rated Income"),不会直接告诉你
API 要用的 TaxType 代码(比如 "ZERORATEDOUTPUT")——不同账套所在地区(UK/US/
AU/NZ...)代码还不一样。这个脚本直接问 Xero API 要准确列表,照着填 .env 里的
XERO_TAX_TYPE 就不会填错。

用法(需要先跑过 xero_auth.py 完成授权):
    python3 list_tax_rates.py
"""
from __future__ import annotations

import requests

from xero_client import API_BASE, _headers


def main() -> None:
    resp = requests.get(f"{API_BASE}/TaxRates", headers=_headers(), timeout=30)
    resp.raise_for_status()
    rates = resp.json().get("TaxRates", [])

    print(f"{'TaxType (填到 .env 的 XERO_TAX_TYPE)':40s} {'Name (网页上看到的名字)':30s} {'状态':8s} 税率")
    print("-" * 100)
    for r in rates:
        if r.get("Status") != "ACTIVE":
            continue
        print(
            f"{r.get('TaxType', ''):40s} "
            f"{r.get('Name', ''):30s} "
            f"{r.get('Status', ''):8s} "
            f"{r.get('EffectiveRate', r.get('DisplayTaxRate', ''))}%"
        )

    print(
        "\n找里面名字看起来像「Zero Rated」「Export」「Zero VAT」这种、"
        "适用于出口海外客户的那一行,把左边第一列的 TaxType 复制到 .env 的 "
        "XERO_TAX_TYPE 里。"
    )


if __name__ == "__main__":
    main()
