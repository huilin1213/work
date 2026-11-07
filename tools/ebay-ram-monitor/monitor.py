"""eBay DDR4 服务器内存监控:新链接、老链接补货/重新上架、降价,第一时间推送到手机。

用法:
    python3 monitor.py                 # 常驻运行(第一次会先静默建立基线,不推送)
    python3 monitor.py --once          # 只搜一次,打印最新 20 条,用来检查搜索词/配置
    python3 monitor.py --test-notify   # 发一条测试推送,检查手机能不能收到

三个任务交替执行,全部走同一个限速器,保证不超过每天的 API 配额:

  1. 快速轮询(最高优先级):按「最新上架」排序拉第一页(200 条),
     没见过的 itemId → 🆕 新上架。
  2. 全量扫描(每 SWEEP_MINUTES 分钟):把所有结果翻一遍(最多 10000 条)。
     - 补上快速轮询漏掉的(eBay 索引有延迟,晚进索引的会排在后面)
     - 卖完的「多数量 GTC 链接」会从搜索里隐藏,补货后重新出现
       → 📦 老链接重新上架/补货
  3. 库存复查(用剩下的配额):getItems 每次 20 条,对比可售数量 / 已售数量,
     可售数量或总数量变多 → 📦 补货;已售数量变多只记录(竞争对手在买)。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from ebay_client import MAX_GET_ITEMS, MAX_PAGE_SIZE, MAX_RESULT_WINDOW, EbayAPIError, EbayClient
from notifier import Notifier
from store import Store

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "ebay_monitor.db"
# 见过之前就创建超过这么久的链接,判定为「老链接重新出现」而不是新上架
OLD_LISTING_HOURS = 6
# 快速轮询每个搜索词最多往后翻几页(第一页全是新的才继续翻)
FAST_MAX_PAGES = 5


@dataclass
class Config:
    queries: list[str]
    category_ids: str
    filters: str
    exclude: list[str]
    budget: int
    fast_poll: int
    sweep_minutes: int
    watch_minutes: int
    price_drop_pct: float

    @classmethod
    def from_env(cls) -> "Config":
        def num(name: str, default: str) -> str:
            return (os.getenv(name) or default).strip()

        queries = [q.strip() for q in num("EBAY_QUERIES", "DDR4 server ram").split(";") if q.strip()]
        filters = []
        bo = num("BUYING_OPTIONS", "FIXED_PRICE")
        if bo:
            filters.append(f"buyingOptions:{{{bo}}}")
        lo, hi = num("MIN_PRICE", ""), num("MAX_PRICE", "")
        if lo or hi:
            filters.append(f"price:[{lo}..{hi}]")
            # price 过滤必须同时带币种
            currency = {"EBAY_GB": "GBP", "EBAY_DE": "EUR", "EBAY_FR": "EUR", "EBAY_IT": "EUR",
                        "EBAY_ES": "EUR", "EBAY_AU": "AUD", "EBAY_CA": "CAD"}.get(
                num("EBAY_MARKETPLACE", "EBAY_US"), "USD")
            filters.append(f"priceCurrency:{currency}")
        extra = num("EXTRA_FILTER", "")
        if extra:
            filters.append(extra)
        return cls(
            queries=queries,
            category_ids=num("EBAY_CATEGORY_IDS", ""),
            filters=",".join(filters),
            exclude=[w.strip().lower() for w in num("EXCLUDE_KEYWORDS", "").split(",") if w.strip()],
            budget=int(num("EBAY_DAILY_CALL_BUDGET", "4500")),
            fast_poll=int(num("FAST_POLL_SECONDS", "60")),
            sweep_minutes=int(num("SWEEP_MINUTES", "60")),
            watch_minutes=int(num("WATCH_RECHECK_MINUTES", "30")),
            price_drop_pct=float(num("PRICE_DROP_ALERT_PCT", "5")),
        )


def summarize(raw: dict) -> dict:
    """把 Browse API 的 itemSummary / item 转成我们存的扁平字段。"""
    price = raw.get("price") or {}
    seller = raw.get("seller") or {}
    loc = raw.get("itemLocation") or {}
    fb = ""
    if seller.get("feedbackScore") is not None:
        fb = f"{seller.get('feedbackScore')} / {seller.get('feedbackPercentage', '?')}%"
    try:
        price_val = float(price["value"]) if price.get("value") is not None else None
    except (TypeError, ValueError):
        price_val = None
    return {
        "item_id": raw["itemId"],
        "legacy_id": raw.get("legacyItemId", ""),
        "title": raw.get("title", ""),
        "url": raw.get("itemWebUrl", ""),
        "seller": seller.get("username", ""),
        "seller_fb": fb,
        "price": price_val,
        "currency": price.get("currency", ""),
        "buying_options": ",".join(raw.get("buyingOptions") or []),
        "condition": raw.get("condition", ""),
        "location": ", ".join(x for x in (loc.get("city"), loc.get("stateOrProvince"), loc.get("country")) if x),
        "created_at": raw.get("itemCreationDate") or raw.get("itemOriginDate") or "",
    }


def parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class SweepState:
    started: float = field(default_factory=time.time)
    query_idx: int = 0
    offset: int = 0
    complete: bool = True   # 中途有任何一个搜索词结果超过 10000 条 → 不完整,不判定下架
    total_pages: int = 0


class Monitor:
    def __init__(self, cfg: Config, client: EbayClient, store: Store, notifier: Notifier) -> None:
        self.cfg = cfg
        self.client = client
        self.store = store
        self.notifier = notifier
        self.bootstrapped = store.get_meta("bootstrap_done") == "1"
        self.last_fast = 0.0
        self.last_sweep_end = time.time()
        self.sweep: SweepState | None = None
        self.last_pages_per_sweep = int(store.get_meta("pages_per_sweep") or 0)
        self.last_status = time.time()
        self.batch_ok = True  # 先试批量 getItems,403 就自动改成逐个 getItem

    # ---------- 工具 ----------
    def excluded(self, title: str) -> bool:
        t = title.lower()
        return any(w in t for w in self.cfg.exclude)

    @staticmethod
    def fmt_price(price: float | None, currency: str) -> str:
        return f"{currency} {price:,.2f}" if price is not None else "价格见链接"

    def alert(self, kind: str, it: dict, extra: str = "") -> None:
        self.store.log_event(it["item_id"], kind, extra)
        if self.excluded(it.get("title", "")):
            return
        heads = {
            "NEW": "🆕 新上架",
            "NEW_LATE": "🆕 新上架(补漏)",
            "OLD_REAPPEAR": "📦 老链接重新出现(可能补货)",
            "REAPPEAR": "📦 重新上架/补货",
            "RESTOCK": "📦 补货",
            "PRICE_DROP": "💲 降价",
        }
        title = f"{heads.get(kind, kind)} {self.fmt_price(it.get('price'), it.get('currency', ''))}"
        if extra:
            title += f" · {extra}"
        lines = [it.get("title", "")]
        meta = " | ".join(x for x in (
            it.get("condition"),
            f"卖家 {it.get('seller')} ({it.get('seller_fb')})" if it.get("seller") else "",
            it.get("location"),
        ) if x)
        if meta:
            lines.append(meta)
        self.notifier.send(title, "\n".join(lines), it.get("url", ""))

    # ---------- 处理一条搜索结果 ----------
    def handle_summary(self, raw: dict, source: str) -> bool:
        """返回 True 表示这是第一次见到的链接。"""
        it = summarize(raw)
        row = self.store.get(it["item_id"])
        if row is None:
            self.store.insert(it)
            if self.bootstrapped:
                created = parse_iso(it["created_at"])
                age_h = (datetime.now(timezone.utc) - created).total_seconds() / 3600 if created else 0
                if age_h > OLD_LISTING_HOURS:
                    self.alert("OLD_REAPPEAR", it, f"{int(age_h // 24)} 天前的链接")
                else:
                    self.alert("NEW" if source == "fast" else "NEW_LATE", it)
            return True

        was_missing = row["missing_since"] is not None
        old_price = row["price"]
        self.store.mark_seen(it["item_id"], it["price"], it["title"])
        if was_missing and self.bootstrapped:
            self.alert("REAPPEAR", it)
        elif (
            self.cfg.price_drop_pct > 0 and old_price and it["price"] is not None
            and it["price"] <= old_price * (1 - self.cfg.price_drop_pct / 100)
        ):
            self.alert("PRICE_DROP", it, f"原价 {old_price:,.2f}")
        return False

    # ---------- 任务 1:快速轮询 ----------
    def fast_poll(self) -> None:
        self.last_fast = time.time()
        for q in self.cfg.queries:
            offset = 0
            for _ in range(FAST_MAX_PAGES):
                body = self.client.search(q, self.cfg.category_ids, self.cfg.filters,
                                          limit=MAX_PAGE_SIZE, offset=offset)
                items = body.get("itemSummaries") or []
                new = sum(self.handle_summary(r, "fast") for r in items)
                # 这一页里有见过的链接 → 更早的都见过了,不用再翻
                if new < len(items) or len(items) < MAX_PAGE_SIZE:
                    break
                offset += MAX_PAGE_SIZE

    # ---------- 任务 2:全量扫描(一次只翻一页,方便和快速轮询穿插) ----------
    def sweep_step(self) -> None:
        if self.sweep is None:
            self.sweep = SweepState()
        s = self.sweep
        q = self.cfg.queries[s.query_idx]
        body = self.client.search(q, self.cfg.category_ids, self.cfg.filters,
                                  limit=MAX_PAGE_SIZE, offset=s.offset)
        s.total_pages += 1
        items = body.get("itemSummaries") or []
        for r in items:
            self.handle_summary(r, "sweep")
        total = int(body.get("total") or 0)
        s.offset += MAX_PAGE_SIZE
        window_end = min(total, MAX_RESULT_WINDOW)
        if s.offset >= window_end or not items:
            if total > MAX_RESULT_WINDOW:
                s.complete = False
                print(f"⚠️  「{q}」共 {total} 条,超过 API 上限 10000,只能扫到最新的 10000 条。"
                      "建议加类目/价格过滤或拆成几个更窄的搜索词。")
            s.query_idx += 1
            s.offset = 0
            if s.query_idx >= len(self.cfg.queries):
                self.finish_sweep()

    def finish_sweep(self) -> None:
        s = self.sweep
        assert s is not None
        if s.complete:
            self.store.after_complete_sweep(s.started)
        self.last_pages_per_sweep = s.total_pages
        self.store.set_meta("pages_per_sweep", str(s.total_pages))
        self.last_sweep_end = time.time()
        self.sweep = None
        total, active = self.store.counts()
        print(f"✅ 全量扫描完成:{s.total_pages} 页,库里在售 {active} 条(累计见过 {total} 条)", flush=True)

    # ---------- 任务 3:库存复查 ----------
    def stock_check(self) -> bool:
        every = self.cfg.watch_minutes * 60
        if self.batch_ok:
            rows = self.store.due_for_stock_check(every, every * 4, MAX_GET_ITEMS)
        else:
            # 只能逐个查时配额很紧:单件链接查过一次就不再复查(它们卖完会从搜索里消失,
            # 补货后重新出现,由全量扫描负责),配额集中给多数量链接
            rows = self.store.due_for_stock_check(every, float("inf"), 1)
        if not rows:
            return False

        if self.batch_ok:
            try:
                fetched = self.client.get_items([row["item_id"] for row in rows])
            except EbayAPIError as exc:
                if exc.status != 403:
                    raise
                self.batch_ok = False
                print("ℹ️  这个密钥没有批量查询(getItems)权限,改为逐个查询库存。"
                      "单件链接只查一次,配额集中给多数量链接。", flush=True)
                return True
            results = {r["itemId"]: r for r in fetched}
        else:
            raw = self.client.get_item(rows[0]["item_id"])
            results = {rows[0]["item_id"]: raw} if raw else {}

        for row in rows:
            raw = results.get(row["item_id"])
            if raw is None:
                # 查不到多半是已结束/卖完,但也可能是接口不支持某些链接(比如多规格)。
                # 这里不判定下架(否则误报「重新上架」),交给全量扫描判断,只是降低复查频率。
                self.store.update_stock(row["item_id"], multi=0)
                continue
            self.apply_stock(row, raw)
        return True

    def apply_stock(self, row, raw: dict) -> None:
        av = (raw.get("estimatedAvailabilities") or [{}])[0]
        avail = av.get("estimatedAvailableQuantity")
        sold = av.get("estimatedSoldQuantity")
        status = av.get("estimatedAvailabilityStatus")
        threshold = av.get("availabilityThreshold")
        multi = int(threshold is not None or (avail or 0) + (sold or 0) > 1)

        it = summarize(raw)
        it["item_id"] = row["item_id"]
        self.store.update_stock(row["item_id"], avail_qty=avail, sold_qty=sold, avail_status=status,
                                threshold=threshold, multi=multi,
                                price=it["price"] if it["price"] is not None else row["price"])
        if row["last_checked"] is None:
            return  # 第一次查,只建立基准

        old_avail, old_sold = row["avail_qty"], row["sold_qty"]
        if sold is not None and old_sold is not None and sold > old_sold:
            # 别人买走了,不推送,只记录 —— 以后可以用来分析哪些型号竞争最激烈
            self.store.log_event(row["item_id"], "SOLD", f"+{sold - old_sold}")

        restock = None
        if row["avail_status"] == "OUT_OF_STOCK" and status in ("IN_STOCK", "LIMITED_STOCK"):
            restock = f"恢复有货,现有 {avail if avail is not None else '?'}"
        elif avail is not None and old_avail is not None:
            # 用「可售+已售」判断:即使同一时间有人买走,只要总数变多就是卖家加了库存
            added = (avail + (sold or 0)) - (old_avail + (old_sold or 0))
            if added > 0 or avail > old_avail:
                restock = f"+{max(added, avail - old_avail)},现有 {avail}"
        elif threshold is not None and row["threshold"] is None and old_avail is not None:
            restock = f"现有 {threshold}+(之前 {old_avail})"
        if restock:
            self.alert("RESTOCK", it, restock)

        if (
            self.cfg.price_drop_pct > 0 and row["price"] and it["price"] is not None
            and it["price"] <= row["price"] * (1 - self.cfg.price_drop_pct / 100)
        ):
            self.alert("PRICE_DROP", it, f"原价 {row['price']:,.2f}")

    # ---------- 调度 ----------
    def bootstrap(self) -> None:
        print("🔧 第一次运行:先把现有的链接全部扫一遍记下来(不推送),之后才开始推送新变化。")
        self.sweep = SweepState()
        while self.sweep is not None:
            self.sweep_step()
        self.store.set_meta("bootstrap_done", "1")
        self.bootstrapped = True
        print("🔧 基线建立完成,开始实时监控。\n", flush=True)

    def print_budget(self) -> None:
        q = len(self.cfg.queries)
        fast = 86400 / self.cfg.fast_poll * q
        sweep = self.last_pages_per_sweep * (1440 / self.cfg.sweep_minutes)
        left = self.cfg.budget - fast - sweep
        print(f"📊 每日配额估算(上限 {self.cfg.budget}):快速轮询 ≈{fast:.0f},"
              f"全量扫描 ≈{sweep:.0f}({self.last_pages_per_sweep} 页/次),剩余给库存复查 ≈{max(left, 0):.0f}")
        if left < 0:
            print("⚠️  配额不够:请调大 FAST_POLL_SECONDS / SWEEP_MINUTES,加价格/类目过滤,"
                  "或去 eBay 开发者后台申请 Growth Check 提额。实际运行会被限速器自动放慢。")

    def run(self) -> None:
        if not self.bootstrapped:
            self.bootstrap()
        self.print_budget()
        while True:
            try:
                now = time.time()
                if now - self.last_fast >= self.cfg.fast_poll:
                    self.fast_poll()
                    continue
                # 低优先级任务只在限速器里还有余量时跑,保证下一次快速轮询不用排队
                if self.client.limiter.available() < 2:
                    time.sleep(1)
                    continue
                if self.sweep is not None or now - self.last_sweep_end >= self.cfg.sweep_minutes * 60:
                    self.sweep_step()
                    continue
                if not self.stock_check():
                    time.sleep(1)
                if now - self.last_status > 3600:
                    self.last_status = now
                    total, active = self.store.counts()
                    print(f"ℹ️  {datetime.now():%H:%M} 今日 API 调用 {self.client.calls_today} 次,"
                          f"在售 {active} 条", flush=True)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - 常驻进程,单次出错不退出
                print(f"❌ 出错,30 秒后继续: {exc}", flush=True)
                time.sleep(30)


def main() -> None:
    load_dotenv(HERE / ".env")
    ap = argparse.ArgumentParser(description="eBay DDR4 服务器内存新链接/补货监控")
    ap.add_argument("--once", action="store_true", help="只搜一次并打印最新 20 条")
    ap.add_argument("--test-notify", action="store_true", help="发一条测试推送")
    args = ap.parse_args()

    notifier = Notifier()
    if args.test_notify:
        if not notifier.channels():
            print("⚠️  .env 里没有配置 NTFY_TOPIC 或 TELEGRAM_*,只会打印到终端")
        notifier.send("✅ 测试推送", "eBay 内存监控可以正常推送到这台设备", "https://www.ebay.com")
        return

    cfg = Config.from_env()
    client = EbayClient(
        os.getenv("EBAY_CLIENT_ID", "").strip(),
        os.getenv("EBAY_CLIENT_SECRET", "").strip(),
        marketplace=os.getenv("EBAY_MARKETPLACE", "EBAY_US").strip(),
        env=os.getenv("EBAY_ENV", "production").strip(),
        daily_budget=cfg.budget,
    )

    if args.once:
        for q in cfg.queries:
            body = client.search(q, cfg.category_ids, cfg.filters, limit=20)
            print(f"\n🔍 「{q}」 共 {body.get('total', 0)} 条结果,最新 20 条:")
            for r in body.get("itemSummaries") or []:
                it = summarize(r)
                flag = " [会被排除]" if any(w in it["title"].lower() for w in cfg.exclude) else ""
                print(f"  {it['created_at'][:16]}  {Monitor.fmt_price(it['price'], it['currency']):>14}  "
                      f"{it['title'][:80]}{flag}")
        return

    print(f"🚀 eBay 内存监控启动 | 搜索词: {cfg.queries} | 类目: {cfg.category_ids or '不限'}")
    print(f"   过滤: {cfg.filters or '无'} | 推送: {', '.join(notifier.channels()) or '仅终端'}")
    try:
        Monitor(cfg, client, Store(DB_PATH), notifier).run()
    except KeyboardInterrupt:
        print("\n👋 已停止监控")
        sys.exit(0)


if __name__ == "__main__":
    main()
