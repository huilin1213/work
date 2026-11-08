"""每天定时运行:给 eBay 上的 DDR4 内存条拍一次"销量快照",和上一次快照相减,
得出这段时间(通常是过去 24 小时)每个商品卖了几件,并生成日报。

用法(在 tools/ebay-ddr4-tracker/ 目录下):
    python3 tracker.py            # 拍快照 + 生成日报(定时任务就跑这个)
    python3 tracker.py --dry-run  # 只搜索并打印数量,不写数据库、不出报表

第一次运行只建立基线,没有可比的上一次快照,所以日报是空的;从第二次开始才有数据。
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from ebay_api import EbayBrowseClient, available_quantity, sold_quantity
import report

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "data" / "snapshots.sqlite3"

# 标题里必须出现 DDR4 / PC4 才算,过滤掉关键词误命中的 DDR3/DDR5 等
DDR4_RE = re.compile(r"\bDDR4\b|\bPC4[-\s]?\d", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    n_items INTEGER,
    api_calls INTEGER
);
CREATE TABLE IF NOT EXISTS listings (
    item_id TEXT PRIMARY KEY,
    legacy_id TEXT,
    title TEXT,
    seller TEXT,
    condition TEXT,
    url TEXT,
    first_seen TEXT,
    last_seen TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
    run_id INTEGER NOT NULL,
    item_id TEXT NOT NULL,
    sold_qty INTEGER,
    avail_qty INTEGER,
    price REAL,
    currency TEXT,
    PRIMARY KEY (run_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_snap_item ON snapshots(item_id, run_id);
"""


def open_db(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def _price(item: dict) -> tuple[float | None, str | None]:
    p = item.get("price") or {}
    try:
        return float(p["value"]), p.get("currency")
    except (KeyError, TypeError, ValueError):
        return None, None


def collect(client: EbayBrowseClient, query: str, category_id: str | None,
            sellers: list[str], price_lo: float, price_hi: float) -> dict[str, dict]:
    """搜索 + 取详情,返回 {itemId: 合并后的商品信息}。"""
    extra = "buyingOptions:{FIXED_PRICE}"  # 拍卖是一次性的,累计销量没有意义,只看一口价
    if sellers:
        extra += ",sellers:{" + "|".join(sellers) + "}"
    summaries = client.search_all(query, category_id, extra, price_lo, price_hi)
    summaries = {k: v for k, v in summaries.items() if DDR4_RE.search(v.get("title", ""))}
    print(f"搜索到 {len(summaries)} 个 DDR4 一口价商品(API 已用 {client.calls} 次)")
    return summaries


def snapshot(con: sqlite3.Connection, client: EbayBrowseClient, summaries: dict[str, dict],
             now: str) -> int:
    """取详情里的累计销量,写入一次快照,返回 run_id。

    除了这次搜到的商品,也会补查上一次快照里有、这次没搜到的商品
    (常见原因是最后一件刚卖掉、商品已下架),这样"卖空"的那一件也能统计到。
    """
    prev_ids = {r[0] for r in con.execute(
        "SELECT item_id FROM snapshots WHERE run_id = (SELECT MAX(run_id) FROM runs)")}
    ids = sorted(set(summaries) | prev_ids)
    details = client.get_items(ids)
    print(f"取到 {len(details)}/{len(ids)} 个商品详情(API 已用 {client.calls} 次)")

    cur = con.execute("INSERT INTO runs (ts) VALUES (?)", (now,))
    run_id = cur.lastrowid
    n = 0
    for item_id in ids:
        d = details.get(item_id)
        s = summaries.get(item_id, {})
        if d is None and not s:
            continue  # 已下架且查不到详情,没法知道最终销量
        src = d or s
        price, currency = _price(src)
        seller = ((src.get("seller") or {}).get("username")) or None
        con.execute(
            """INSERT INTO listings (item_id, legacy_id, title, seller, condition, url, first_seen, last_seen)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(item_id) DO UPDATE SET title=excluded.title, seller=excluded.seller,
                 condition=excluded.condition, url=excluded.url, last_seen=excluded.last_seen""",
            (item_id, src.get("legacyItemId"), src.get("title"), seller, src.get("condition"),
             src.get("itemWebUrl"), now, now),
        )
        con.execute(
            "INSERT INTO snapshots (run_id, item_id, sold_qty, avail_qty, price, currency) VALUES (?,?,?,?,?,?)",
            (run_id, item_id, sold_quantity(d) if d else None,
             available_quantity(d) if d else None, price, currency),
        )
        n += 1
    con.execute("UPDATE runs SET n_items=?, api_calls=? WHERE run_id=?", (n, client.calls, run_id))
    con.commit()
    return run_id


def sales_since_previous(con: sqlite3.Connection, run_id: int) -> tuple[str | None, str, list[dict]]:
    """对比本次快照和每个商品上一次有销量数据的快照,返回 (起始时间, 结束时间, 销售记录)。"""
    end_ts = con.execute("SELECT ts FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
    prev = con.execute("SELECT MAX(run_id), MAX(ts) FROM runs WHERE run_id < ?", (run_id,)).fetchone()
    if prev[0] is None:
        return None, end_ts, []
    rows = con.execute(
        """
        SELECT l.seller, l.title, l.condition, l.url, l.legacy_id, s.item_id,
               s.price, s.currency, s.sold_qty,
               (SELECT p.sold_qty FROM snapshots p
                 WHERE p.item_id = s.item_id AND p.run_id < s.run_id AND p.sold_qty IS NOT NULL
                 ORDER BY p.run_id DESC LIMIT 1) AS prev_sold
        FROM snapshots s JOIN listings l ON l.item_id = s.item_id
        WHERE s.run_id = ? AND s.sold_qty IS NOT NULL
        """,
        (run_id,),
    ).fetchall()
    sales = []
    for seller, title, cond, url, legacy, item_id, price, cur, sold, prev_sold in rows:
        if prev_sold is None or sold <= prev_sold:
            continue  # 新出现的商品没有基线,不计;没有新增销量的也跳过
        qty = sold - prev_sold
        sales.append(dict(seller=seller, title=title, condition=cond, price=price, currency=cur,
                          qty=qty, revenue=round((price or 0) * qty, 2), url=url,
                          item_id=legacy or item_id))
    sales.sort(key=lambda r: (-r["revenue"], r["seller"] or ""))
    return prev[1], end_ts, sales


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="eBay DDR4 内存条每日销量追踪")
    ap.add_argument("--dry-run", action="store_true", help="只搜索,不写库、不出报表")
    args = ap.parse_args(argv)

    load_dotenv(HERE / ".env")
    cid, secret = os.getenv("EBAY_CLIENT_ID"), os.getenv("EBAY_CLIENT_SECRET")
    if not cid or not secret:
        print("缺少 EBAY_CLIENT_ID / EBAY_CLIENT_SECRET,请先按 README 填好 .env", file=sys.stderr)
        return 2
    client = EbayBrowseClient(
        cid, secret,
        marketplace=os.getenv("EBAY_MARKETPLACE", "EBAY_GB"),
        currency=os.getenv("EBAY_CURRENCY", "GBP"),
        max_calls=int(os.getenv("EBAY_MAX_CALLS", "4500")),
    )
    query = os.getenv("SEARCH_QUERY", "DDR4")
    category = os.getenv("EBAY_CATEGORY_ID", "170083") or None
    sellers = [s.strip() for s in os.getenv("SELLERS", "").split(",") if s.strip()]
    lo = float(os.getenv("PRICE_MIN", "1"))
    hi = float(os.getenv("PRICE_MAX", "5000"))

    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    print(f"[{now}] 开始:关键词={query!r} 类目={category} 站点={client.marketplace}"
          + (f" 卖家={sellers}" if sellers else " 卖家=全部"))
    summaries = collect(client, query, category, sellers, lo, hi)
    if args.dry_run:
        print("dry-run 结束,未写入数据库")
        return 0

    con = open_db()
    run_id = snapshot(con, client, summaries, now)
    start, end, sales = sales_since_previous(con, run_id)
    if start is None:
        print("这是第一次运行,已建立基线快照;明天这个时间起才会有销量数据。")
        return 0
    paths = report.write(sales, start, end, HERE / "reports")
    report.print_summary(sales, start, end)
    print("报表已生成:", *[str(p) for p in paths], sep="\n  ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
