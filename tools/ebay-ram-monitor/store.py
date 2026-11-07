"""SQLite 本地记录:见过哪些链接、上次的价格/库存、哪些从搜索结果里消失了。

刻意不保存任何 eBay 用户数据(卖家用户名/评价等只在推送里显示,不落库),
这样在 eBay 开发者后台可以如实选择 Marketplace Account Deletion 的豁免(opt out)。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    item_id        TEXT PRIMARY KEY,
    legacy_id      TEXT,
    title          TEXT,
    url            TEXT,
    price          REAL,
    currency       TEXT,
    buying_options TEXT,
    condition      TEXT,
    location       TEXT,
    created_at     TEXT,           -- eBay 的 itemCreationDate
    first_seen     REAL,
    last_seen      REAL,           -- 最后一次出现在搜索结果里
    miss_count     INTEGER DEFAULT 0,  -- 连续几次完整扫描没出现
    missing_since  REAL,           -- 判定为"已下架/卖完"的时间,NULL = 在售
    last_checked   REAL,           -- 最后一次用 getItems 查库存
    avail_qty      INTEGER,
    sold_qty       INTEGER,
    avail_status   TEXT,
    threshold      INTEGER,        -- eBay 只显示"10+ available"时的那个 10
    multi          INTEGER DEFAULT 0   -- 是否多数量链接
);
CREATE INDEX IF NOT EXISTS idx_items_check ON items(missing_since, last_checked);
CREATE TABLE IF NOT EXISTS events (
    ts      REAL,
    item_id TEXT,
    kind    TEXT,   -- NEW / OLD_REAPPEAR / REAPPEAR / RESTOCK / PRICE_DROP / SOLD
    detail  TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---------- meta ----------
    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
        self.db.commit()

    # ---------- items ----------
    def get(self, item_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()

    def insert(self, it: dict) -> None:
        now = time.time()
        self.db.execute(
            """INSERT INTO items(item_id, legacy_id, title, url, price,
                   currency, buying_options, condition, location, created_at, first_seen, last_seen)
               VALUES (:item_id, :legacy_id, :title, :url, :price,
                   :currency, :buying_options, :condition, :location, :created_at, :now, :now)""",
            {**it, "now": now},
        )
        self.db.commit()

    def mark_seen(self, item_id: str, price: float | None, title: str) -> None:
        self.db.execute(
            """UPDATE items SET last_seen=?, miss_count=0, missing_since=NULL,
                   price=COALESCE(?, price), title=? WHERE item_id=?""",
            (time.time(), price, title, item_id),
        )
        self.db.commit()

    def update_stock(self, item_id: str, **fields) -> None:
        fields["last_checked"] = time.time()
        cols = ", ".join(f"{k}=:{k}" for k in fields)
        self.db.execute(f"UPDATE items SET {cols} WHERE item_id=:item_id", {**fields, "item_id": item_id})
        self.db.commit()

    def after_complete_sweep(self, sweep_started: float, misses_to_missing: int = 2) -> int:
        """完整扫描结束后:本轮没出现的在售链接 miss_count+1,连续 N 次没出现就判定下架。

        要求连续多次才判定,是因为翻页期间有商品下架会让后面的结果整体前移,
        偶尔会漏掉一两条 —— 只漏一次不算数,避免误报"重新上架"。
        """
        cur = self.db.execute(
            "UPDATE items SET miss_count=miss_count+1 WHERE missing_since IS NULL AND last_seen < ?",
            (sweep_started,),
        )
        self.db.execute(
            "UPDATE items SET missing_since=? WHERE missing_since IS NULL AND miss_count >= ?",
            (time.time(), misses_to_missing),
        )
        self.db.commit()
        return cur.rowcount

    def due_for_stock_check(self, multi_every: float, single_every: float, limit: int) -> list[sqlite3.Row]:
        now = time.time()
        return self.db.execute(
            """SELECT * FROM items
               WHERE missing_since IS NULL AND buying_options LIKE '%FIXED_PRICE%'
                 AND (last_checked IS NULL
                      OR (multi=1 AND last_checked < ?)
                      OR (multi=0 AND last_checked < ?))
               ORDER BY last_checked IS NOT NULL, multi DESC, last_checked
               LIMIT ?""",
            (now - multi_every, now - single_every, limit),
        ).fetchall()

    def counts(self) -> tuple[int, int]:
        row = self.db.execute(
            "SELECT COUNT(*) AS total, SUM(missing_since IS NULL) AS active FROM items"
        ).fetchone()
        return row["total"] or 0, row["active"] or 0

    # ---------- events ----------
    def log_event(self, item_id: str, kind: str, detail: str = "") -> None:
        self.db.execute(
            "INSERT INTO events(ts, item_id, kind, detail) VALUES (?, ?, ?, ?)",
            (time.time(), item_id, kind, detail),
        )
        self.db.commit()
