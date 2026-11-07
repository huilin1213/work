"""查 eBay 类目 ID,填到 .env 的 EBAY_CATEGORY_IDS 里,能排除笔记本/台式机内存等无关结果。

用法:
    python3 find_category.py                 # 默认查 "DDR4 server memory"
    python3 find_category.py "ECC RDIMM"     # 自定义关键词
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from ebay_client import EbayClient

HERE = Path(__file__).resolve().parent


def main() -> None:
    load_dotenv(HERE / ".env")
    q = " ".join(sys.argv[1:]) or "DDR4 server memory"
    client = EbayClient(
        os.getenv("EBAY_CLIENT_ID", "").strip(),
        os.getenv("EBAY_CLIENT_SECRET", "").strip(),
        marketplace=os.getenv("EBAY_MARKETPLACE", "EBAY_US").strip(),
        env=os.getenv("EBAY_ENV", "production").strip(),
    )
    print(f"「{q}」在 {client.marketplace} 的类目建议:\n")
    for s in client.category_suggestions(q):
        cat = s["category"]
        path = " > ".join(a["categoryName"] for a in reversed(s.get("categoryTreeNodeAncestors") or []))
        print(f"  {cat['categoryId']:>8}  {path} > {cat['categoryName']}")
    print("\n把想要的类目 ID 填到 .env 的 EBAY_CATEGORY_IDS(多个用逗号分隔)")


if __name__ == "__main__":
    main()
