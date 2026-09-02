"""首次登录脚本:打开一个真实的浏览器窗口,你手动登录一次 eBay(账号密码 +
可能的两步验证/人机验证 由你自己在浏览器里完成),脚本把登录后的 cookies 存到
本地文件,后面 main.py / watch_sync.py 就能直接复用这份登录状态,不用每次都
重新登录。

只需要在第一次使用、或者 cookies 过期导致读不到订单时运行一次:
    python3 ebay_login.py

跟 xero_auth.py 的定位一样:只应该在真人坐在电脑前的终端里手动跑,不要放进
watch_sync.py 或任何自动化里 —— 它需要你在弹出的浏览器窗口里完成登录。
"""
from __future__ import annotations

import os

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

DOMAIN = os.environ.get("EBAY_DOMAIN", "ebay.co.uk")
STORAGE_STATE_PATH = os.environ.get("STORAGE_STATE_PATH", "./storage_state.json")


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(f"https://www.{DOMAIN}/signin/")

        print(f"👉 请在弹出的浏览器窗口里正常登录你的 eBay 账号(https://www.{DOMAIN})")
        print("   如果有两步验证/人机验证,也在窗口里按提示完成。")
        input("   登录完成、能看到 eBay 首页/My eBay 之后,回到这里按回车键继续...")

        page.goto(f"https://www.{DOMAIN}/mye/myebay/purchase", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        if page.get_by_role("link", name="Sign in").count() > 0 or "signin" in page.url:
            print("⚠️  这个页面看起来还是没登录成功,先别关浏览器,确认登录状态后再回车重试。")
            input("   确认已登录后,回车继续保存登录状态...")

        context.storage_state(path=STORAGE_STATE_PATH)
        browser.close()

    print(f"✅ 登录状态已保存到 {STORAGE_STATE_PATH}(已加入 .gitignore,不会被提交)。")
    print("   之后运行 python3 main.py 就会直接用这份登录状态抓购买历史,不用再登录。")
    print("   如果之后某天 main.py 提示读不到订单/被跳转回登录页,再重新跑一次这个脚本。")


if __name__ == "__main__":
    main()
