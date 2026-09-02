"""常驻后台,每隔一段时间自动跑一次同步 —— 这就是"每当我有新的 purchase 就自动
同步到 Excel"的实现方式:eBay 没有 webhook/推送通知新订单,所以只能定时轮询。

用法:
    python3 watch_sync.py

工作方式:
  - 每隔 SYNC_INTERVAL_MINUTES 分钟(.env 里配置,默认 360 = 6 小时)跑一次跟
    main.py 完全一样的抓取 + 同步逻辑,只抓今年 + 去年,新订单会自动补进 Excel,
    已经同步过的订单/商品行不会重复写入
  - 这个脚本要一直留着运行(终端窗口开着、缩小都行,别关掉),按 Control+C 停止
  - 想开机自动后台运行(不用手动开终端敲命令),可以配置成 macOS 的 LaunchAgent
    登录启动项,想做的话告诉我,我再给你写具体步骤(用法可以参考同一个仓库里
    tools/xero-dhl-invoice 的 LaunchAgent 部署方式)
"""
from __future__ import annotations

import os
import time
from datetime import date, datetime

from dotenv import load_dotenv

from main import sync_once

load_dotenv()

INTERVAL_MINUTES = float(os.environ.get("SYNC_INTERVAL_MINUTES", "360"))


def watch() -> None:
    print(f"👀 开始定时同步 eBay 购买历史,每 {INTERVAL_MINUTES:.0f} 分钟跑一次")
    print("   按 Control+C 停止\n")
    while True:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{now}] 开始同步...")
        try:
            this_year = date.today().year
            sync_once([this_year, this_year - 1])
        except SystemExit as exc:
            # 登录状态失效这类"需要人工处理"的情况:打印出来但不退出常驻进程,
            # 等你重新登录后,下一轮轮询会自动恢复正常
            print(f"⚠️  本轮同步中止: {exc}")
        except Exception as exc:  # noqa: BLE001 - 单轮失败不影响下一轮继续跑
            print(f"❌ 本轮同步失败: {exc}")
        time.sleep(INTERVAL_MINUTES * 60)


def main() -> None:
    try:
        watch()
    except KeyboardInterrupt:
        print("\n👋 已停止定时同步")


if __name__ == "__main__":
    main()
