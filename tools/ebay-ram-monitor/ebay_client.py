"""eBay Browse API 的轻量封装:应用级 token、限速、搜索、批量查商品库存。

只用到「Application token」(client credentials),不需要买家登录授权,
也不会下单 —— 自动下单需要 eBay 单独审批 Order API,这里刻意不碰。
"""
from __future__ import annotations

import base64
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import requests

API_HOSTS = {
    "production": "https://api.ebay.com",
    "sandbox": "https://api.sandbox.ebay.com",
}
SCOPE = "https://api.ebay.com/oauth/api_scope"

# Browse API 的硬限制:offset + limit 不能超过 10000
MAX_RESULT_WINDOW = 10000
MAX_PAGE_SIZE = 200
MAX_GET_ITEMS = 20


class EbayAPIError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class RateLimiter:
    """令牌桶:把每天的调用配额均匀摊开,允许少量突发。"""

    def __init__(self, calls_per_day: int, burst: int = 10) -> None:
        self.rate = calls_per_day / 86400.0
        self.capacity = float(burst)
        self.tokens = float(burst)
        self.updated = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def available(self) -> float:
        self._refill()
        return self.tokens

    def acquire(self) -> None:
        while True:
            self._refill()
            if self.tokens >= 1:
                self.tokens -= 1
                return
            time.sleep((1 - self.tokens) / self.rate)


class EbayClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        marketplace: str = "EBAY_US",
        env: str = "production",
        daily_budget: int = 4500,
    ) -> None:
        if not client_id or not client_secret:
            raise RuntimeError("缺少 EBAY_CLIENT_ID / EBAY_CLIENT_SECRET,请先填好 .env")
        self.client_id = client_id
        self.client_secret = client_secret
        self.marketplace = marketplace
        self.host = API_HOSTS[env]
        self.limiter = RateLimiter(daily_budget)
        self.session = requests.Session()
        self._token: str | None = None
        self._token_expiry = 0.0
        self.calls_today = 0
        self._calls_day = datetime.now(timezone.utc).date()

    # ---------- 认证 ----------
    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 120:
            return self._token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        resp = self.session.post(
            f"{self.host}/identity/v1/oauth2/token",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": SCOPE},
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"获取 eBay token 失败 ({resp.status_code}): {resp.text[:500]}")
        body = resp.json()
        self._token = body["access_token"]
        self._token_expiry = time.time() + int(body.get("expires_in", 7200))
        return self._token

    # ---------- 通用 GET ----------
    def _count_call(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self._calls_day:
            self._calls_day = today
            self.calls_today = 0
        self.calls_today += 1

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        backoff = 5
        for attempt in range(5):
            self.limiter.acquire()
            self._count_call()
            try:
                resp = self.session.get(
                    f"{self.host}{path}",
                    params=params,
                    headers={
                        "Authorization": f"Bearer {self._get_token()}",
                        "X-EBAY-C-MARKETPLACE-ID": self.marketplace,
                    },
                    timeout=30,
                )
            except requests.RequestException as exc:
                print(f"⚠️  网络错误,{backoff}s 后重试: {exc}")
                time.sleep(backoff)
                backoff *= 2
                continue

            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 401 and attempt == 0:
                self._token = None  # token 过期/失效,刷新后重试一次
                continue
            if resp.status_code == 429:
                print("⚠️  eBay 返回 429(超出调用配额/频率),暂停 5 分钟")
                time.sleep(300)
                continue
            if resp.status_code >= 500:
                print(f"⚠️  eBay 服务端错误 {resp.status_code},{backoff}s 后重试")
                time.sleep(backoff)
                backoff *= 2
                continue
            raise EbayAPIError(resp.status_code, f"eBay API {path} 返回 {resp.status_code}: {resp.text[:500]}")
        raise RuntimeError(f"eBay API {path} 多次重试仍失败")

    # ---------- Browse API ----------
    def search(
        self,
        q: str,
        category_ids: str = "",
        filters: str = "",
        sort: str = "newlyListed",
        limit: int = MAX_PAGE_SIZE,
        offset: int = 0,
    ) -> dict:
        params: dict[str, Any] = {"q": q, "sort": sort, "limit": limit, "offset": offset}
        if category_ids:
            params["category_ids"] = category_ids
        if filters:
            params["filter"] = filters
        return self.get("/buy/browse/v1/item_summary/search", params)

    def get_items(self, item_ids: list[str]) -> list[dict]:
        """批量查(一次最多 20 个)。getItems 是 eBay 的受限接口,普通密钥会返回 403,
        调用方遇到 403 应改用 get_item 逐个查。"""
        if not item_ids:
            return []
        body = self.get("/buy/browse/v1/item/", {"item_ids": ",".join(item_ids[:MAX_GET_ITEMS])})
        return body.get("items") or []

    def get_item(self, item_id: str) -> dict | None:
        """查单个商品(带库存 estimatedAvailabilities)。已结束/不存在返回 None。"""
        try:
            return self.get(f"/buy/browse/v1/item/{quote(item_id, safe='')}")
        except EbayAPIError as exc:
            if exc.status == 404:
                return None
            raise

    # ---------- Taxonomy API(查类目 ID 用) ----------
    def category_suggestions(self, q: str) -> list[dict]:
        tree = self.get(
            "/commerce/taxonomy/v1/get_default_category_tree_id",
            {"marketplace_id": self.marketplace},
        )
        tree_id = tree["categoryTreeId"]
        body = self.get(
            f"/commerce/taxonomy/v1/category_tree/{tree_id}/get_category_suggestions",
            {"q": q},
        )
        return body.get("categorySuggestions") or []
