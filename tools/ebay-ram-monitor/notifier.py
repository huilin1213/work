"""手机推送:ntfy / Telegram,都没配置就只打印到终端。"""
from __future__ import annotations

import os
from datetime import datetime

import requests


class Notifier:
    def __init__(self) -> None:
        self.ntfy_topic = os.getenv("NTFY_TOPIC", "").strip()
        self.ntfy_server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        self.tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.tg_chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    def channels(self) -> list[str]:
        out = []
        if self.ntfy_topic:
            out.append(f"ntfy({self.ntfy_topic})")
        if self.tg_token and self.tg_chat:
            out.append("Telegram")
        return out

    def send(self, title: str, body: str, url: str = "", urgent: bool = True) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        print(f"\n[{stamp}] 🔔 {title}\n{body}\n{url}\n", flush=True)

        if self.ntfy_topic:
            try:
                # 用 JSON 方式发布,标题里有中文/emoji 也不会出问题
                payload = {
                    "topic": self.ntfy_topic,
                    "title": title,
                    "message": body,
                    "priority": 5 if urgent else 3,
                }
                if url:
                    payload["click"] = url
                    payload["actions"] = [{"action": "view", "label": "打开 eBay", "url": url}]
                requests.post(self.ntfy_server, json=payload, timeout=10).raise_for_status()
            except requests.RequestException as exc:
                print(f"⚠️  ntfy 推送失败: {exc}")

        if self.tg_token and self.tg_chat:
            try:
                text = f"{title}\n{body}\n{url}".strip()
                requests.post(
                    f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                    json={"chat_id": self.tg_chat, "text": text, "disable_notification": not urgent},
                    timeout=10,
                ).raise_for_status()
            except requests.RequestException as exc:
                # 不直接打印 exc:里面的 URL 带 bot token,会被写进日志
                code = getattr(getattr(exc, "response", None), "status_code", "")
                print(f"⚠️  Telegram 推送失败: {type(exc).__name__} {code}")
