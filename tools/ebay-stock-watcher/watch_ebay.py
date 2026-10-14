"""
监控 eBay 商品页面是否补货,补货时立即通知(macOS 桌面通知 + 手机推送 + 自动打开页面)。

用法:
    python3 watch_ebay.py https://www.ebay.co.uk/itm/327393276571
    python3 watch_ebay.py <url> --interval 120 --ntfy-topic my-secret-topic
    python3 watch_ebay.py <url> --once          # 只检查一次并打印状态(调试用)

判定方式:
    1. 如果设置了 EBAY_CLIENT_ID / EBAY_CLIENT_SECRET 环境变量,优先走 eBay 官方
       Browse API(getItemByLegacyId),最稳定,不会被反爬拦截。
    2. 否则抓取商品网页 HTML,根据 "This item is out of stock" / "X available" /
       "Buy it now" 等文字判断。

只在状态从「缺货/未知」变成「有货」时通知一次;之后再次缺货、再次补货会再通知。
遇到验证码/被拦截时状态记为 UNKNOWN,不会误报。
"""

import argparse
import base64
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime

import requests

IN_STOCK = "IN_STOCK"
OUT_OF_STOCK = "OUT_OF_STOCK"
UNKNOWN = "UNKNOWN"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def item_id_from_url(url: str) -> str:
    m = re.search(r"/itm/(?:[^/]+/)?(\d{9,})", url)
    if not m:
        raise ValueError(f"无法从链接中解析出商品编号: {url}")
    return m.group(1)


def marketplace_from_url(url: str) -> str:
    if "ebay.co.uk" in url:
        return "EBAY_GB"
    if "ebay.de" in url:
        return "EBAY_DE"
    return "EBAY_US"


# ---------- 方式 1:官方 Browse API ----------

_token_cache = {"token": None, "expires": 0.0}


def _api_token(client_id: str, client_secret: str) -> str:
    if _token_cache["token"] and time.time() < _token_cache["expires"] - 60:
        return _token_cache["token"]
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires"] = time.time() + int(data.get("expires_in", 7200))
    return _token_cache["token"]


def check_via_api(url: str, client_id: str, client_secret: str) -> tuple[str, str]:
    token = _api_token(client_id, client_secret)
    resp = requests.get(
        "https://api.ebay.com/buy/browse/v1/item/get_item_by_legacy_id",
        params={"legacy_item_id": item_id_from_url(url)},
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": marketplace_from_url(url),
        },
        timeout=20,
    )
    if resp.status_code == 404:
        return UNKNOWN, "API 404(商品已下架或编号错误)"
    resp.raise_for_status()
    data = resp.json()
    price = data.get("price", {})
    price_str = f"{price.get('currency', '')} {price.get('value', '?')}".strip()
    for avail in data.get("estimatedAvailabilities", []):
        status = avail.get("estimatedAvailabilityStatus")
        qty = avail.get("estimatedAvailableQuantity")
        if status in ("IN_STOCK", "LIMITED_STOCK"):
            return IN_STOCK, f"API: {status}, 数量≈{qty}, 价格 {price_str}"
        if status == "OUT_OF_STOCK":
            return OUT_OF_STOCK, f"API: OUT_OF_STOCK, 价格 {price_str}"
    return UNKNOWN, "API 返回中没有库存信息"


# ---------- 方式 2:抓取网页 ----------

_session = requests.Session()
_session.headers.update(
    {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
    }
)


def check_via_html(url: str) -> tuple[str, str]:
    resp = _session.get(url, timeout=30)
    if resp.status_code != 200:
        return UNKNOWN, f"HTTP {resp.status_code}"
    html = resp.text
    low = html.lower()

    if "pardon our interruption" in low or "captcha" in resp.url.lower():
        return UNKNOWN, "被 eBay 反爬/验证码拦截"

    if re.search(r"this item is out of stock|item is no longer available|"
                 r"this listing (?:has )?ended|this listing was ended", low):
        return OUT_OF_STOCK, "页面显示缺货/已结束"

    qty = re.search(r"(more than \d+|\d+) available", low)
    if qty:
        return IN_STOCK, f"页面显示 {qty.group(0)}"
    if "last one" in low:
        return IN_STOCK, "页面显示 Last one"
    if re.search(r">\s*buy it now\s*<|>\s*add to basket\s*<|>\s*add to cart\s*<", low):
        return IN_STOCK, "页面出现购买按钮"

    return UNKNOWN, "页面里没找到可识别的库存信息"


# ---------- 通知 ----------

def notify(url: str, detail: str, ntfy_topic: str | None, open_browser: bool) -> None:
    title = "eBay 补货了!"
    body = f"{detail}\n{url}"
    log(f"*** {title} {detail} ***")

    if sys.platform == "darwin":
        script = (
            f'display notification {_applescript_str(detail)} '
            f'with title {_applescript_str(title)} sound name "Glass"'
        )
        subprocess.run(["osascript", "-e", script], check=False)
        if open_browser:
            subprocess.run(["open", url], check=False)

    if ntfy_topic:
        try:
            requests.post(
                f"https://ntfy.sh/{ntfy_topic}",
                data=body.encode("utf-8"),
                headers={
                    "Title": "eBay restocked!",
                    "Priority": "urgent",
                    "Tags": "rotating_light",
                    "Click": url,
                },
                timeout=15,
            )
        except requests.RequestException as e:
            log(f"ntfy 推送失败: {e}")


def _applescript_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ---------- 主循环 ----------

def check(url: str) -> tuple[str, str]:
    cid = os.environ.get("EBAY_CLIENT_ID")
    secret = os.environ.get("EBAY_CLIENT_SECRET")
    if cid and secret:
        return check_via_api(url, cid, secret)
    return check_via_html(url)


def main() -> None:
    p = argparse.ArgumentParser(description="eBay 补货监控")
    p.add_argument("url", help="eBay 商品链接")
    p.add_argument("--interval", type=int, default=120,
                   help="检查间隔秒数(默认 120,会加随机抖动;太频繁容易被拦截)")
    p.add_argument("--ntfy-topic", default=os.environ.get("NTFY_TOPIC"),
                   help="ntfy.sh 主题名,手机装 ntfy App 订阅同名主题即可收到推送")
    p.add_argument("--no-open", action="store_true", help="补货时不自动打开浏览器")
    p.add_argument("--once", action="store_true", help="只检查一次并退出")
    args = p.parse_args()

    item_id_from_url(args.url)  # 提前校验链接格式

    if args.once:
        status, detail = check(args.url)
        log(f"{status}: {detail}")
        return

    log(f"开始监控 {args.url},间隔约 {args.interval}s")
    last = None
    while True:
        try:
            status, detail = check(args.url)
        except Exception as e:  # 网络抖动等,不退出
            status, detail = UNKNOWN, f"检查出错: {e}"

        if status != last:
            log(f"{status}: {detail}")
        # UNKNOWN 不改变"上一次确定的状态",避免 缺货→被拦截→缺货 这种抖动造成误报
        if status == IN_STOCK and last != IN_STOCK:
            notify(args.url, detail, args.ntfy_topic, not args.no_open)
        if status != UNKNOWN:
            last = status

        time.sleep(args.interval * random.uniform(0.8, 1.2))


if __name__ == "__main__":
    main()
