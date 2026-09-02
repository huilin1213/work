"""用 Playwright 驱动一个已登录的浏览器,抓取 eBay 购买历史(Purchase history)
逐单明细,包括官方没有报表可导的字段:卖家名称、物流 tracking number、VAT、
buyer protection fee、postage fee 等。

设计取舍(仿照 dhl_parser.py 的思路):eBay 页面的 CSS 类名是混淆过的、经常随
改版变化,但页面上给用户看的文案("Order number"、"Tracking number"、"Buyer
protection" ...)相对稳定得多。所以这里不依赖固定的 CSS 选择器抓字段,而是抓
整块可见文本(`inner_text`)按"标签: 值"这种模式用正则/关键字去匹配 —— 更接近
人眼读页面的方式,eBay 小改版通常不会破坏它,只有页面上的措辞本身变了才需要
跟着改 `LABELS` 这几个候选词表。

用法:
    from ebay_scraper import fetch_orders
    orders = fetch_orders(context, domain="ebay.co.uk", years=[2026, 2025])

注意:这是非官方的页面抓取(eBay 没有面向普通买家的"购买历史" API),只应该用
来读取你自己账号的购买记录做个人记账用,不要用来抓别人的数据或高频访问。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from playwright.sync_api import Page

# 每类字段在 eBay 页面上可能出现的文案,按出现概率排序;抓取"跑不出来"的时候
# 大概率是 eBay 改了措辞,先来这里加一个候选词,不用动下面的解析逻辑。
LABELS = {
    "order_number": ["Order number", "Order #", "Order ID"],
    "order_date": ["Order date", "Ordered on", "Date sold"],
    "seller": ["Seller", "Sold by"],
    "tracking": ["Tracking number", "Tracking no", "Tracking #"],
    "postage": ["Postage", "Shipping", "Delivery"],
    "buyer_protection": ["Buyer protection fee", "eBay buyer protection fee", "Buyer protection"],
    "vat": ["VAT", "Import charges", "Import VAT"],
    "order_total": ["Order total", "Total"],
    "item_price": ["Item price", "Item cost", "Price"],
    "quantity": ["Quantity", "Qty"],
}


@dataclass
class LineItem:
    product_name: str
    quantity: int
    item_price: Decimal


@dataclass
class EbayOrder:
    order_number: str
    order_date: str
    seller_name: str
    tracking_number: str
    postage_fee: Decimal
    buyer_protection_fee: Decimal
    vat_amount: Decimal
    order_total: Decimal | None
    detail_url: str
    line_items: list[LineItem] = field(default_factory=list)


def _parse_money(text: str) -> Decimal:
    """'£12.34' / '12,34 €' / 'US $1,234.56' -> Decimal('12.34') 这种数值部分。

    eBay 不同国家站点的小数分隔符不一样(英美用点,欧洲大陆用逗号),这里用一个
    简单的启发式:如果逗号后面正好跟 2 位数字且是整个数字的结尾,当成小数点
    (欧陆写法);否则当成千位分隔符去掉。
    """
    m = re.search(r"[\d](?:[\d,.\s])*\d|\d", text)
    if not m:
        raise ValueError(f"无法从 {text!r} 中解析出金额")
    raw = m.group(0).replace(" ", "")
    if re.search(r",\d{2}$", raw) and "." not in raw:
        raw = raw.replace(".", "").replace(",", ".")
    else:
        raw = raw.replace(",", "")
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"无法从 {text!r} 中解析出金额") from exc


def _find_value(lines: list[str], candidates: list[str]) -> str:
    """在整页文本行里找 '标签: 值' 或 '标签' 独占一行、值在下一行 两种常见排版。"""
    for i, line in enumerate(lines):
        stripped = line.strip()
        for label in candidates:
            if not stripped.lower().startswith(label.lower()):
                continue
            remainder = stripped[len(label):].lstrip(":  ").strip()
            if remainder:
                return remainder
            # 值可能被布局拆到下一行(常见于卡片式布局:标签和值不在同一个文本节点)
            for j in range(i + 1, min(i + 3, len(lines))):
                nxt = lines[j].strip()
                if nxt and nxt.lower() not in (c.lower() for c in candidates):
                    return nxt
    return ""


def _find_money(lines: list[str], candidates: list[str]) -> Decimal:
    raw = _find_value(lines, candidates)
    if not raw or raw.lower() in ("free", "included"):
        return Decimal("0")
    try:
        return _parse_money(raw)
    except ValueError:
        return Decimal("0")


def _extract_order_number(url: str, lines: list[str]) -> str:
    m = re.search(r"[?&](?:orderId|orderid|itemId)=([\w-]+)", url)
    if m:
        return m.group(1)
    return _find_value(lines, LABELS["order_number"])


def _extract_line_items(lines: list[str]) -> list[LineItem]:
    """一笔订单可能包含多个不同商品(不同数量/单价),逐条抓取。

    eBay 订单详情页里每个商品一般是"标题"紧跟着"Quantity: N"和"Item price: X"
    这几行,这里按 "Item price" 出现的位置往回找最近的商品标题、往后找同一小段
    里的 Quantity。找不到时退化成整单只当一行(数量 1、单价取 order_total)。
    """
    items: list[LineItem] = []
    price_label_set = {l.lower() for l in LABELS["item_price"]}
    for i, line in enumerate(lines):
        stripped = line.strip()
        matched_label = next(
            (l for l in LABELS["item_price"] if stripped.lower().startswith(l.lower())), None
        )
        if not matched_label:
            continue
        remainder = stripped[len(matched_label):].lstrip(":  ").strip()
        price_text = remainder or (lines[i + 1].strip() if i + 1 < len(lines) else "")
        try:
            price = _parse_money(price_text)
        except ValueError:
            continue

        # 商品标题:往前找最近一行"看起来像标题"(非标签、非纯数字/金额)的文本
        title = ""
        for j in range(i - 1, max(i - 6, -1), -1):
            candidate = lines[j].strip()
            if not candidate:
                continue
            if any(candidate.lower().startswith(l.lower()) for labels in LABELS.values() for l in labels):
                continue
            title = candidate
            break

        # 数量:往后找几行里的 "Quantity: N"
        qty = 1
        for j in range(i, min(i + 4, len(lines))):
            qty_val = _find_value([lines[j]], LABELS["quantity"])
            if qty_val:
                qty_m = re.search(r"\d+", qty_val)
                if qty_m:
                    qty = int(qty_m.group(0))
                break

        items.append(LineItem(product_name=title or "(未识别商品名)", quantity=qty, item_price=price))
    return items


def parse_order_detail(page: Page, detail_url: str) -> EbayOrder:
    """解析当前已打开的订单详情页(page 必须已经 goto 到 detail_url)。"""
    text = page.inner_text("body")
    lines = [l for l in text.splitlines() if l.strip()]

    order_number = _extract_order_number(detail_url, lines)
    order_date = _find_value(lines, LABELS["order_date"])
    seller = _find_value(lines, LABELS["seller"])
    tracking = _find_value(lines, LABELS["tracking"])
    postage = _find_money(lines, LABELS["postage"])
    buyer_protection = _find_money(lines, LABELS["buyer_protection"])
    vat = _find_money(lines, LABELS["vat"])
    total_raw = _find_value(lines, LABELS["order_total"])
    order_total = None
    if total_raw:
        try:
            order_total = _parse_money(total_raw)
        except ValueError:
            order_total = None

    line_items = _extract_line_items(lines)
    if not line_items:
        # 兜底:一个商品都没识别出来时,至少把整单记一行,避免这单彻底丢失
        fallback_price = order_total if order_total is not None else Decimal("0")
        line_items = [LineItem(product_name="(未识别商品明细,见订单详情链接)", quantity=1, item_price=fallback_price)]

    order = EbayOrder(
        order_number=order_number or detail_url,
        order_date=order_date,
        seller_name=seller,
        tracking_number=tracking,
        postage_fee=postage,
        buyer_protection_fee=buyer_protection,
        vat_amount=vat,
        order_total=order_total,
        detail_url=detail_url,
        line_items=line_items,
    )

    if order.order_total is not None:
        calc_total = sum((li.item_price for li in order.line_items), Decimal("0")) + postage + buyer_protection + vat
        if abs(calc_total - order.order_total) > Decimal("0.05"):
            print(
                f"⚠️  订单 {order.order_number}:逐项合计 {calc_total} 与页面 Order total "
                f"{order.order_total} 不一致,建议打开 {detail_url} 人工核对"
            )

    return order


def list_order_detail_links(page: Page, domain: str, year: int) -> list[str]:
    """打开某一年的购买历史列表页,收集这一页所有订单的"查看订单详情"链接。"""
    url = f"https://www.{domain}/mye/myebay/purchase?filter=year%3A{year}"
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)  # 列表是前端异步渲染的,给它一点时间加载订单卡片

    # 尽量把"加载更多"点完,把这一年的订单都展开出来
    for _ in range(20):
        more_button = page.get_by_role("button", name=re.compile(r"show more|see more|load more", re.I))
        if more_button.count() == 0:
            break
        try:
            more_button.first.click(timeout=3000)
            page.wait_for_timeout(1000)
        except Exception:  # noqa: BLE001 - 没点到就算了,不影响已经加载出来的订单
            break

    links = page.get_by_role("link", name=re.compile(r"order details|view order details|see order details", re.I))
    hrefs: list[str] = []
    for i in range(links.count()):
        href = links.nth(i).get_attribute("href")
        if href:
            hrefs.append(href if href.startswith("http") else f"https://www.{domain}{href}")
    # 去重,保持顺序
    seen: set[str] = set()
    deduped = []
    for h in hrefs:
        if h not in seen:
            seen.add(h)
            deduped.append(h)
    return deduped


def fetch_orders(page: Page, domain: str, years: list[int], delay_ms: int = 800) -> list[EbayOrder]:
    """抓取给定年份范围内的所有订单明细。逐单打开详情页,之间加一点延时,别把
    eBay 当接口猛戳。"""
    orders: list[EbayOrder] = []
    for year in years:
        print(f"📄 打开 {year} 年购买历史列表...")
        try:
            detail_links = list_order_detail_links(page, domain, year)
        except Exception as exc:  # noqa: BLE001 - 某一年列表页出问题不影响其他年份
            print(f"❌ 抓取 {year} 年列表失败: {exc}")
            continue
        print(f"   找到 {len(detail_links)} 个订单")
        for link in detail_links:
            try:
                page.goto(link, wait_until="domcontentloaded")
                page.wait_for_timeout(delay_ms)
                orders.append(parse_order_detail(page, link))
            except Exception as exc:  # noqa: BLE001 - 单个订单解析失败不影响其他订单
                print(f"❌ 解析订单详情失败 ({link}): {exc}")
    return orders
