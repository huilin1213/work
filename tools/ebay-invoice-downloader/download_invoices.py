"""翻你的 eBay 购买记录(Purchase history),把有发票/Tax invoice 链接的订单页面
存成 PDF,存到本地文件夹(见 .env 的 EBAY_OUTPUT_DIR)。

**重要说明 —— 这版还没在真实 eBay 页面上跑通过**:写这个脚本的环境连不了
ebay.co.uk(网络出口被挡了),没法照着真实页面结构调选择器。下面用的是"按文字找
链接/按钮"这种相对通用、不依赖具体 CSS class 的写法(eBay 换版式的话,精确的
class 名字会变,但"链接文字里带 invoice"这种大概率还在),但**没有把握第一次跑
就一次成功**。第一次用请先加 `--debug` 跑,卡住的地方会自动停在 Playwright
Inspector 里,你可以看着真实页面把选择器改对,发给我我再改进这个脚本。

用法:
    python3 ebay_login.py            # 第一次先登录一次(见该脚本说明)
    python3 download_invoices.py             # 正常运行
    python3 download_invoices.py --debug     # 调试模式:开可见浏览器 + 卡住时暂停
    python3 download_invoices.py --limit 3   # 只处理前 3 个发票链接,先小范围验证
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import Page, sync_playwright

load_dotenv()

HERE = Path(__file__).parent
STATE_FILE = HERE / ".ebay_state.json"
DOWNLOADED_FILE = HERE / ".ebay_downloaded.json"

ORDER_NO_RE = re.compile(r"\b(\d{2}-\d{5}-\d{5})\b")
# 三种发票页面上出现过的日期标签,格式都是 "5 Sep 2026" 这种
DATE_RE = re.compile(
    r"(?:Date of invoice|Order date|Placed on|Paid on)\s*[:\n]?\s*(\d{1,2} \w{3,9} \d{4})",
    re.IGNORECASE,
)


def _load_downloaded() -> set[str]:
    if DOWNLOADED_FILE.exists():
        return set(json.loads(DOWNLOADED_FILE.read_text()))
    return set()


def _save_downloaded(order_nos: set[str]) -> None:
    DOWNLOADED_FILE.write_text(json.dumps(sorted(order_nos), ensure_ascii=False, indent=2))


def _collect_invoice_links(page: Page, max_scrolls: int = 30) -> list[str]:
    """在购买记录页面里反复往下滚 / 点"加载更多",收集所有"文字里带 invoice"的
    链接地址。eBay 具体用"Show more"还是无限滚动,现在没法验证,两种都试。"""
    seen: set[str] = set()
    stable_rounds = 0
    for _ in range(max_scrolls):
        links = page.get_by_role("link", name=re.compile(r"invoice", re.IGNORECASE)).all()
        before = len(seen)
        for link in links:
            href = link.get_attribute("href")
            if href:
                seen.add(href)
        if len(seen) == before:
            stable_rounds += 1
            if stable_rounds >= 2:
                break
        else:
            stable_rounds = 0

        more_btn = page.get_by_role("button", name=re.compile(r"show more|see more|load more", re.IGNORECASE))
        if more_btn.count() > 0:
            try:
                more_btn.first.click(timeout=3000)
                page.wait_for_timeout(1500)
                continue
            except Exception:
                pass
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(1200)

    return sorted(seen)


def _extract_order_no(text: str) -> str | None:
    m = ORDER_NO_RE.search(text)
    return m.group(1) if m else None


def _extract_date(text: str) -> datetime | None:
    m = DATE_RE.search(text)
    if not m:
        return None
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(m.group(1), fmt)
        except ValueError:
            continue
    return None


def run(debug: bool = False, limit: int | None = None) -> None:
    site = os.environ.get("EBAY_SITE", "https://www.ebay.co.uk")
    purchase_url = os.environ.get("EBAY_PURCHASE_URL") or f"{site}/mye/myebay/purchase"
    month = os.environ.get("EBAY_MONTH", "").strip()
    out_dir = Path(os.environ.get("EBAY_OUTPUT_DIR", str(Path.home() / "eBay发票" / "待整理"))).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    if not STATE_FILE.exists():
        raise SystemExit("还没登录过,先跑一次: python3 ebay_login.py")

    downloaded = _load_downloaded()
    saved, skipped_no_invoice_date, skipped_dup = 0, 0, 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not debug)
        context = browser.new_context(storage_state=str(STATE_FILE))
        page = context.new_page()
        page.goto(purchase_url)
        if debug:
            print("🔍 调试模式:页面打开了,如果订单列表没显示出来(比如要手动选日期")
            print("   筛选),现在自己在这个浏览器窗口里操作;操作完在下面按回车让脚本继续。")
            input("   按回车继续自动收集发票链接... ")

        links = _collect_invoice_links(page)
        print(f"📋 购买记录页面里找到 {len(links)} 个带 'invoice' 字样的链接")

        if limit:
            links = links[:limit]

        for href in links:
            url = href if href.startswith("http") else f"{site}{href}"
            page.goto(url)
            text = page.inner_text("body")

            order_no = _extract_order_no(text)
            invoice_date = _extract_date(text)

            if order_no and order_no in downloaded:
                skipped_dup += 1
                continue

            if month:
                if invoice_date is None:
                    if debug:
                        print(f"⚠️  没读到日期,不确定是不是 {month} 的,跳过: {url}")
                        page.pause()
                    skipped_no_invoice_date += 1
                    continue
                if invoice_date.strftime("%Y-%m") != month:
                    continue

            filename = f"eBay_{order_no or ('未知订单号_' + str(saved + 1))}.pdf"
            out_path = out_dir / filename
            page.pdf(path=str(out_path))
            print(f"✅ 已保存: {filename}")
            saved += 1
            if order_no:
                downloaded.add(order_no)

        browser.close()

    _save_downloaded(downloaded)
    print(f"\n完成:保存 {saved} 张,{skipped_dup} 张之前已下载过跳过,{skipped_no_invoice_date} 张没读到日期跳过")
    print(f"存放位置: {out_dir}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--debug", action="store_true", help="打开可见浏览器,遇到问题可以手动介入")
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 个发票链接,先小范围验证用")
    args = ap.parse_args()
    run(debug=args.debug, limit=args.limit)


if __name__ == "__main__":
    main()
