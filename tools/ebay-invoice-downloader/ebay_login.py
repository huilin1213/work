"""首次登录脚本:弹出一个真实的浏览器窗口,你手动登录一次 eBay(包括遇到的任何
验证码/短信验证/两步验证,都由你本人在这个窗口里完成),登录状态会保存到本地的
`.ebay_state.json`(不是密码,是登录后的会话信息;已加入 .gitignore,不会被提交)。

之后跑 download_invoices.py 会直接复用这个登录状态,不用重复登录 —— 除非隔太久
没用、eBay 把这个会话登出了,到时候重新跑一次这个脚本再登录一次就行。

用法:
    python3 ebay_login.py
"""
from __future__ import annotations

import os

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

STATE_FILE = os.path.join(os.path.dirname(__file__), ".ebay_state.json")


def main() -> None:
    site = os.environ.get("EBAY_SITE", "https://www.ebay.co.uk")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(f"{site}/signin")

        print("👉 浏览器窗口已经打开,请在里面手动登录你的 eBay 账号(包括任何验证码/")
        print("   短信验证都在这个窗口里完成)。")
        print("   登录成功、能看到 eBay 首页或者你的账号页面之后,回到这个终端窗口,")
        input("   按一下回车键继续... ")

        context.storage_state(path=STATE_FILE)
        browser.close()

    print(f"✅ 登录状态已保存到 {STATE_FILE}")
    print("   之后跑 download_invoices.py 会自动用这个状态,不用再登录一次。")


if __name__ == "__main__":
    main()
