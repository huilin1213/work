"""从 eBay 购买历史(Purchase history)网页抓取逐单明细,同步进本地 Excel。

用法:
    python3 main.py                # 抓今年 + 去年(默认),同步进 EXCEL_PATH
    python3 main.py 2026 2025 2024 # 指定要抓哪几年

前置条件:先运行一次 `python3 ebay_login.py` 手动登录并保存好登录状态,
否则这里打开的页面会是登录页,抓不到任何订单。
"""
from __future__ import annotations

import os
import sys
from datetime import date

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from ebay_scraper import fetch_orders
from excel_sync import sync

load_dotenv()

DOMAIN = os.environ.get("EBAY_DOMAIN", "ebay.co.uk")
EXCEL_PATH = os.environ.get("EXCEL_PATH", "./ebay_purchases.xlsx")
STORAGE_STATE_PATH = os.environ.get("STORAGE_STATE_PATH", "./storage_state.json")
HEADLESS = os.environ.get("HEADLESS", "true").strip().lower() != "false"


def sync_once(years: list[int]) -> None:
    if not os.path.exists(STORAGE_STATE_PATH):
        raise SystemExit(
            f"没找到登录状态文件 {STORAGE_STATE_PATH},请先运行 `python3 ebay_login.py` 登录一次。"
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context(storage_state=STORAGE_STATE_PATH)
        page = context.new_page()

        # 先探一下登录状态还有没有效,没效就提前退出提示重新登录,免得抓了个寂寞
        page.goto(f"https://www.{DOMAIN}/mye/myebay/purchase", wait_until="domcontentloaded")
        page.wait_for_timeout(1000)
        if "signin" in page.url or page.get_by_role("link", name="Sign in").count() > 0:
            browser.close()
            raise SystemExit("登录状态已失效(被跳转回登录页),请重新运行 `python3 ebay_login.py`")

        orders = fetch_orders(page, DOMAIN, years)
        browser.close()

    added, skipped = sync(orders, EXCEL_PATH)
    print(f"\n✅ 本次共抓到 {len(orders)} 笔订单,新增 {added} 行商品明细到 {EXCEL_PATH}")
    print(f"   {skipped} 笔订单已经同步过,跳过(没有新商品行)")


def main() -> None:
    if len(sys.argv) > 1:
        years = [int(y) for y in sys.argv[1:]]
    else:
        this_year = date.today().year
        years = [this_year, this_year - 1]
    sync_once(years)


if __name__ == "__main__":
    main()
