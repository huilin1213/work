"""监听一个文件夹,自动处理新出现的 DHL 发票 PDF —— 不用再手动敲命令、拖文件。

用法:
    python3 watch_folder.py                  # 默认监听 ~/DHL发票/待处理
    python3 watch_folder.py 自定义文件夹路径    # 监听指定文件夹

工作方式:
  - 每隔几秒扫描一次这个文件夹,发现新的 .pdf 文件就用跟 main.py 完全一样的
    逻辑处理(解析 -> 校验金额 -> 查重 -> 在 Xero 建 Draft 发票)
  - 处理成功的文件会被移到同目录下的「处理完成」子文件夹
  - 处理失败的文件会被移到「处理失败」子文件夹,并打印失败原因,不会反复重试
  - 这个脚本要一直留着运行(终端窗口开着、缩小都行,别关掉),之后你只需要把
    从 DHL 下载的发票 PDF 存进"待处理"这个文件夹,其余全自动。
    按 Control+C 可以随时停止监听。
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

from main import process_one

DEFAULT_WATCH_DIR = Path.home() / "DHL发票" / "待处理"
POLL_INTERVAL_SECONDS = 10


def watch(watch_dir: Path) -> None:
    processed_dir = watch_dir / "处理完成"
    failed_dir = watch_dir / "处理失败"
    watch_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(exist_ok=True)
    failed_dir.mkdir(exist_ok=True)

    print(f"👀 正在监听文件夹:{watch_dir}")
    print(f"   把 DHL 发票 PDF 存进这个文件夹就行,脚本每 {POLL_INTERVAL_SECONDS} 秒自动检查一次新文件")
    print("   按 Control+C 停止监听\n")

    while True:
        for pdf_path in sorted(watch_dir.glob("*.pdf")):
            print(f"\n发现新文件:{pdf_path.name}")
            try:
                process_one(str(pdf_path))
                shutil.move(str(pdf_path), str(processed_dir / pdf_path.name))
            except Exception as exc:  # noqa: BLE001 - 单个文件出错不影响继续监听
                print(f"❌ 处理 {pdf_path.name} 失败,移到「处理失败」文件夹,不会重试: {exc}")
                shutil.move(str(pdf_path), str(failed_dir / pdf_path.name))
        time.sleep(POLL_INTERVAL_SECONDS)


def main() -> None:
    watch_dir = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else DEFAULT_WATCH_DIR
    try:
        watch(watch_dir)
    except KeyboardInterrupt:
        print("\n👋 已停止监听")


if __name__ == "__main__":
    main()
