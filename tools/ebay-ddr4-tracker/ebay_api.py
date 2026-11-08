"""eBay Browse API 的轻量封装(只读,用 Client Credentials 应用级 token,不需要卖家登录)。

只用到两个接口:
- GET /buy/browse/v1/item_summary/search  按关键词 + 类目搜索在售商品
- GET /buy/browse/v1/item/?item_ids=...  批量(每次最多 20 个)取商品详情,
  里面的 estimatedAvailabilities[].estimatedSoldQuantity 就是累计售出数量
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field

import requests

API_ROOT = "https://api.ebay.com"
TOKEN_URL = f"{API_ROOT}/identity/v1/oauth2/token"
SEARCH_URL = f"{API_ROOT}/buy/browse/v1/item_summary/search"
GET_ITEMS_URL = f"{API_ROOT}/buy/browse/v1/item/"
SCOPE = "https://api.ebay.com/oauth/api_scope"

# Browse search 单个查询最多只能翻到第 10,000 条(offset + limit <= 10000)
SEARCH_RESULT_CAP = 10_000
PAGE_SIZE = 200
GET_ITEMS_BATCH = 20


class CallBudgetExceeded(RuntimeError):
    pass


@dataclass
class EbayBrowseClient:
    client_id: str
    client_secret: str
    marketplace: str = "EBAY_GB"
    currency: str = "GBP"
    max_calls: int = 4500  # Browse API 默认额度是每天 5,000 次,留点余量
    calls: int = 0
    _token: str | None = field(default=None, repr=False)
    _token_expiry: float = 0.0
    session: requests.Session = field(default_factory=requests.Session, repr=False)

    # ---------- 认证 ----------
    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        r = self.session.post(
            TOKEN_URL,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": SCOPE},
            timeout=30,
        )
        if r.status_code != 200:
            raise RuntimeError(f"eBay token 获取失败 {r.status_code}: {r.text[:300]}")
        j = r.json()
        self._token = j["access_token"]
        self._token_expiry = time.time() + int(j.get("expires_in", 7200))
        return self._token

    def _get(self, url: str, params: dict) -> dict:
        if self.calls >= self.max_calls:
            raise CallBudgetExceeded(f"已用掉 {self.calls} 次 API 调用,达到上限 {self.max_calls}")
        for attempt in range(4):
            self.calls += 1
            r = self.session.get(
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {self._get_token()}",
                    "X-EBAY-C-MARKETPLACE-ID": self.marketplace,
                },
                timeout=60,
            )
            if r.status_code == 401 and attempt == 0:
                self._token = None  # token 过期,刷新后重试一次
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** (attempt + 1))
                continue
            if r.status_code == 404:
                return {}
            if r.status_code != 200:
                raise RuntimeError(f"eBay API {r.status_code} {url}: {r.text[:300]}")
            return r.json()
        raise RuntimeError(f"eBay API 多次重试仍失败: {url}")

    # ---------- 搜索 ----------
    def _search_page(self, q: str, category_id: str | None, filters: str, offset: int) -> dict:
        params = {"q": q, "filter": filters, "limit": PAGE_SIZE, "offset": offset}
        if category_id:
            params["category_ids"] = category_id
        return self._get(SEARCH_URL, params)

    def search_all(self, q: str, category_id: str | None, extra_filter: str = "",
                   price_lo: float = 0.0, price_hi: float = 10_000.0) -> dict[str, dict]:
        """搜出所有匹配的在售商品,返回 {itemId: item_summary}。

        单个查询超过 10,000 条时,按价格区间对半拆分后分别查,绕过翻页上限。
        """
        results: dict[str, dict] = {}
        stack = [(price_lo, price_hi)]
        while stack:
            lo, hi = stack.pop()
            filters = f"price:[{lo:.2f}..{hi:.2f}],priceCurrency:{self.currency}"
            if extra_filter:
                filters += "," + extra_filter
            first = self._search_page(q, category_id, filters, 0)
            total = int(first.get("total", 0))
            if total > SEARCH_RESULT_CAP and hi - lo > 0.02:
                mid = round((lo + hi) / 2, 2)
                stack.append((lo, mid))
                stack.append((mid + 0.01, hi))
                continue
            for it in first.get("itemSummaries", []) or []:
                results[it["itemId"]] = it
            offset = PAGE_SIZE
            while offset < min(total, SEARCH_RESULT_CAP):
                page = self._search_page(q, category_id, filters, offset)
                items = page.get("itemSummaries", []) or []
                if not items:
                    break
                for it in items:
                    results[it["itemId"]] = it
                offset += PAGE_SIZE
        return results

    # ---------- 详情(含累计销量) ----------
    def get_items(self, item_ids: list[str]) -> dict[str, dict]:
        """批量取商品详情,返回 {itemId: item}。已下架/查不到的商品不会出现在结果里。"""
        out: dict[str, dict] = {}
        for i in range(0, len(item_ids), GET_ITEMS_BATCH):
            batch = item_ids[i:i + GET_ITEMS_BATCH]
            j = self._get(GET_ITEMS_URL, {"item_ids": ",".join(batch)})
            for it in j.get("items", []) or []:
                out[it["itemId"]] = it
        return out


def sold_quantity(item: dict) -> int | None:
    """从商品详情里取累计售出数量(eBay 给的是估计值)。"""
    for a in item.get("estimatedAvailabilities", []) or []:
        if a.get("estimatedSoldQuantity") is not None:
            return int(a["estimatedSoldQuantity"])
    return None


def available_quantity(item: dict) -> int | None:
    for a in item.get("estimatedAvailabilities", []) or []:
        if a.get("estimatedAvailableQuantity") is not None:
            return int(a["estimatedAvailableQuantity"])
    return None
