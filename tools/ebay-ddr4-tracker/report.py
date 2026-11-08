"""把一次对比得出的销售记录写成 Excel + CSV 日报,并在终端/日志里打印摘要。"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

HEADER_FONT = Font(bold=True, color="FFFFFF")
HEADER_FILL = PatternFill("solid", fgColor="1F4E79")


def by_seller(sales: list[dict]) -> list[tuple[str, int, float, int]]:
    """[(卖家, 件数, 销售额, 商品数)],按销售额降序。"""
    agg: dict[str, list] = defaultdict(lambda: [0, 0.0, 0])
    for s in sales:
        a = agg[s["seller"] or "(未知)"]
        a[0] += s["qty"]
        a[1] += s["revenue"]
        a[2] += 1
    return sorted(((k, q, round(r, 2), n) for k, (q, r, n) in agg.items()), key=lambda x: -x[2])


def _sheet(wb: Workbook, title: str, headers: list[str], widths: list[int], first: bool = False):
    ws = wb.active if first else wb.create_sheet(title)
    ws.title = title
    ws.append(headers)
    for i, w in enumerate(widths, 1):
        c = ws.cell(1, i)
        c.font, c.fill = HEADER_FONT, HEADER_FILL
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    return ws


def write(sales: list[dict], start: str, end: str, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"ddr4_sales_{end[:10]}"
    xlsx, csv_path = out_dir / f"{stem}.xlsx", out_dir / f"{stem}.csv"

    wb = Workbook()
    ws = _sheet(wb, "销售明细", ["卖家", "商品标题", "成色", "单价", "币种", "数量", "销售额", "eBay 商品号", "链接"],
                [24, 80, 12, 10, 6, 6, 11, 15, 40], first=True)
    for s in sales:
        ws.append([s["seller"], s["title"], s["condition"], s["price"], s["currency"], s["qty"],
                   s["revenue"], s["item_id"], s["url"]])
    n = len(sales) + 1
    ws.append([])
    ws.append(["合计", "", "", "", "", f"=SUM(F2:F{n})", f"=SUM(G2:G{n})"])
    for c in ws[n + 2]:
        c.font = Font(bold=True)
    for col in ("D", "G"):
        for c in ws[col][1:]:
            c.number_format = "#,##0.00"

    ws2 = _sheet(wb, "按卖家", ["卖家", "件数", "销售额", "商品数"], [28, 8, 12, 8])
    for row in by_seller(sales):
        ws2.append(list(row))
    for c in ws2["C"][1:]:
        c.number_format = "#,##0.00"

    ws3 = _sheet(wb, "说明", ["说明"], [110])
    for line in [
        f"统计区间:{start}  →  {end}",
        "数量 = 本次快照的累计售出数 − 上次快照的累计售出数(eBay estimatedSoldQuantity,为估计值)。",
        "单价 = 快照时的标价;Best Offer 议价成交、运费、退款都不在其中,销售额是按标价估算。",
        "只统计一口价(Buy It Now)商品;拍卖、以及统计区间内新上架商品的首日销量不计入。",
        "卖空后已下架、且 eBay 已查不到详情的商品,最后一笔销量可能漏计。",
    ]:
        ws3.append([line])
    wb.save(xlsx)

    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["start", "end", "seller", "title", "condition", "price", "currency", "qty", "revenue",
                    "item_id", "url"])
        for s in sales:
            w.writerow([start, end, s["seller"], s["title"], s["condition"], s["price"], s["currency"],
                        s["qty"], s["revenue"], s["item_id"], s["url"]])
    return [xlsx, csv_path]


def print_summary(sales: list[dict], start: str, end: str, top: int = 10) -> None:
    qty = sum(s["qty"] for s in sales)
    rev = sum(s["revenue"] for s in sales)
    cur = next((s["currency"] for s in sales if s["currency"]), "")
    print(f"\n==== DDR4 销售日报 {start} → {end} ====")
    print(f"共 {qty} 件 / {len(sales)} 个商品 / {len({s['seller'] for s in sales})} 个卖家 / 估算销售额 {rev:,.2f} {cur}")
    print(f"\n-- 卖家 Top {top} --")
    for seller, q, r, n in by_seller(sales)[:top]:
        print(f"{r:>10,.2f}  {q:>4} 件  {seller}")
    print(f"\n-- 商品 Top {top}(按销售额)--")
    for s in sales[:top]:
        print(f"{s['revenue']:>10,.2f}  {s['qty']:>3} × {s['price']:.2f}  {s['seller']}  {s['title'][:70]}")
