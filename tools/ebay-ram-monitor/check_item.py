"""诊断某条 listing 为什么没有(或什么时候)推送。

用法(item 号或 eBay 链接都可以):
    python3 check_item.py 820331229965
    python3 check_item.py "https://www.ebay.co.uk/itm/820331229965?..."

会逐项对照 .env 里的条件(类目、发货地、购买方式、价格、搜索词、排除词),
并查本地数据库:程序有没有见过它、什么时候第一次看到、推送过什么。
只消耗 1 次 API 调用。
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from ebay_client import EbayAPIError, EbayClient
from monitor import DB_PATH, Config, parse_iso

HERE = Path(__file__).resolve().parent


def query_matches(q: str, title: str) -> bool:
    """近似模拟 eBay 搜索语法:空格 = 并且;(a,b,c) = 任意一个。eBay 实际匹配更宽松,仅供参考。"""
    t = title.lower()
    groups = re.findall(r"\(([^)]*)\)", q)
    words = re.sub(r"\([^)]*\)", " ", q).split()
    if not all(w.lower() in t for w in words):
        return False
    return all(any(opt.strip().lower() in t for opt in g.split(",") if opt.strip()) for g in groups)


def fmt_ts(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S") if ts else "-"


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    m = re.search(r"(\d{9,15})", sys.argv[1])
    if not m:
        print("❌ 没认出 item 号,请直接给 eBay 链接或 12 位左右的数字")
        sys.exit(1)
    legacy = m.group(1)

    load_dotenv(HERE / ".env")
    cfg = Config.from_env()
    client = EbayClient(
        os.getenv("EBAY_CLIENT_ID", "").strip(),
        os.getenv("EBAY_CLIENT_SECRET", "").strip(),
        marketplace=os.getenv("EBAY_MARKETPLACE", "EBAY_US").strip(),
        env=os.getenv("EBAY_ENV", "production").strip(),
    )

    print(f"🔎 检查 item {legacy}\n")
    try:
        it = client.get("/buy/browse/v1/item/get_item_by_legacy_id", {"legacy_item_id": legacy})
    except EbayAPIError as exc:
        if exc.status == 404:
            print("❌ eBay 说找不到这个商品(可能已结束,或者这是多规格 listing)")
        else:
            print(f"❌ 查询失败: {exc}")
        it = None

    if it:
        title = it.get("title", "")
        cat_id = str(it.get("categoryId", ""))
        country = (it.get("itemLocation") or {}).get("country", "")
        buying = it.get("buyingOptions") or []
        price = float((it.get("price") or {}).get("value") or 0)
        created = parse_iso(it.get("itemCreationDate") or "")

        print(f"标题:   {title}")
        print(f"类目:   {cat_id}  {it.get('categoryPath', '')}")
        print(f"发货地: {country}   购买方式: {', '.join(buying)}   价格: {price}")
        if created:
            print(f"上架时间: {created.astimezone().strftime('%m-%d %H:%M:%S')}")
        print("\n逐项对照 .env 条件:")

        checks = []
        if cfg.category_ids:
            ok = cat_id in [c.strip() for c in cfg.category_ids.split(",")]
            checks.append((ok, f"类目 {cat_id} {'在' if ok else '不在'} EBAY_CATEGORY_IDS={cfg.category_ids} 里"))
        fil = cfg.filters
        loc = re.search(r"itemLocationCountry:(\w+)", fil)
        if loc:
            ok = country == loc.group(1)
            checks.append((ok, f"发货地 {country or '?'} {'符合' if ok else '不符合'} itemLocationCountry:{loc.group(1)}"))
        bo = re.search(r"buyingOptions:\{([^}]*)\}", fil)
        if bo:
            wanted = bo.group(1).split("|")
            ok = any(b in wanted for b in buying)
            checks.append((ok, f"购买方式 {buying} {'符合' if ok else '不符合'} {wanted}"))
        pr = re.search(r"price:\[([\d.]*)\.\.([\d.]*)\]", fil)
        if pr:
            lo, hi = pr.group(1), pr.group(2)
            ok = (not lo or price >= float(lo)) and (not hi or price <= float(hi))
            checks.append((ok, f"价格 {price} {'在' if ok else '不在'} [{lo}..{hi}] 区间内"))
        qok = any(query_matches(q, title) for q in cfg.queries)
        checks.append((qok, f"标题{'匹配' if qok else '可能不匹配'}搜索词 {cfg.queries}(近似判断)"))
        hit = [w for w in cfg.exclude if w in title.lower()]
        checks.append((not hit, f"标题{'含排除词 ' + str(hit) + ',不会推送' if hit else '不含排除词'}"))
        for ok, msg in checks:
            print(f"  {'✅' if ok else '❌'} {msg}")

    print("\n本地数据库记录:")
    if not DB_PATH.exists():
        print("  (还没有数据库,程序没运行过)")
        return
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT * FROM items WHERE legacy_id=? OR item_id LIKE ?",
                     (legacy, f"%|{legacy}|%")).fetchone()
    if not row:
        print("  ❌ 程序从来没在搜索结果里见过它")
        print("     → 如果上面各项都是 ✅,通常是 eBay 搜索还没收录(新上架常有几分钟延迟),")
        print("       或者当时 Mac 在睡眠。过几分钟再运行一次本命令看看。")
        return
    print(f"  第一次看到: {fmt_ts(row['first_seen'])}   最后出现在搜索里: {fmt_ts(row['last_seen'])}")
    created = parse_iso(row["created_at"] or "")
    if created:
        delay = row["first_seen"] - created.timestamp()
        print(f"  从上架到被程序发现: {delay / 60:.1f} 分钟")
    events = db.execute("SELECT * FROM events WHERE item_id=? ORDER BY ts", (row["item_id"],)).fetchall()
    if events:
        for e in events:
            print(f"  {fmt_ts(e['ts'])}  {e['kind']}  {e['detail'] or ''}")
    else:
        print("  没有推送记录 —— 第一次看到它时程序可能还在建立基线(基线期间不推送)")


if __name__ == "__main__":
    main()
