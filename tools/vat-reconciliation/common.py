"""几个 bank_parsers.py / invoice_ledger.py 共用的小工具。"""
from __future__ import annotations

import re
from datetime import date, datetime

# eBay 订单号的格式固定是 "两位数字-五位数字-五位数字",不管是出现在 Revolut
# 流水的 Description 里(比如 "Ebay O*23-14958-56688"),还是发票 PDF/照片上的
# "Order: 23-14958-56688",都是同一个正则能抓出来 —— 这是这个工具最重要的匹配
# 依据,比"金额+日期模糊匹配"可靠得多。
EBAY_ORDER_RE = re.compile(r"\b(\d{2}-\d{5}-\d{5})\b")


def parse_date(value) -> date:
    """把各种常见日期写法统一转成 date 对象,认不出就直接报错(不要静默出错)。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"认不出这个日期格式: {value!r}")
