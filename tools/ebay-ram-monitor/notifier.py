"""手机推送:ntfy / Telegram / Pushover,都没配置就只打印到终端。

Pushover 是唯一能在 iPhone 静音模式下也响铃的通道:紧急优先级(2)的消息会走
苹果的 Critical Alerts(需要在 Pushover App 里允许),并且每隔 PUSHOVER_RETRY 秒
重复提醒,直到你在手机上点「确认」或超过 PUSHOVER_EXPIRE 秒。

PUSHOVER_NIGHT(如 00:00-08:00,按这台 Mac 的本地时间)时段内,紧急推送改用
PUSHOVER_NIGHT_PRIORITY(默认 -1 = 静默送达、不响不震),早上在 App 里照样能看到。
"""
from __future__ import annotations

import os
from datetime import datetime, time

import requests


class Notifier:
    def __init__(self) -> None:
        self.ntfy_topic = os.getenv("NTFY_TOPIC", "").strip()
        self.ntfy_server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        self.tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.tg_chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.po_token = os.getenv("PUSHOVER_APP_TOKEN", "").strip()
        self.po_user = os.getenv("PUSHOVER_USER_KEY", "").strip()
        # 紧急推送用的优先级:2 = 紧急(重复提醒直到确认);1 = 高(只响一次)。
        # 静音/勿扰下会不会响,取决于 Pushover App 里对应优先级的 Critical Alerts 开关。
        self.po_urgent_priority = _int_env("PUSHOVER_URGENT_PRIORITY", 2, lo=-2, hi=2)
        self.po_retry = _int_env("PUSHOVER_RETRY", 60, lo=30, hi=10800)      # Pushover 要求 >= 30 秒
        self.po_expire = _int_env("PUSHOVER_EXPIRE", 600, lo=30, hi=10800)   # 最长 3 小时
        self.po_sound = os.getenv("PUSHOVER_SOUND", "").strip()
        self.po_night = _parse_window(os.getenv("PUSHOVER_NIGHT", ""))
        self.po_night_priority = _int_env("PUSHOVER_NIGHT_PRIORITY", -1, lo=-2, hi=2)

    def channels(self) -> list[str]:
        out = []
        if self.ntfy_topic:
            out.append(f"ntfy({self.ntfy_topic})")
        if self.tg_token and self.tg_chat:
            out.append("Telegram")
        if self.po_token and self.po_user:
            desc = f"Pushover(紧急推送优先级 {self.po_urgent_priority}"
            if self.po_night:
                a, b = self.po_night
                desc += f",夜间 {a:%H:%M}-{b:%H:%M} 改为 {self.po_night_priority}"
                if self.in_night():
                    desc += ",现在是夜间时段"
            out.append(desc + ")")
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

        if self.po_token and self.po_user:
            self._send_pushover(title, body, url, urgent)

    def _send_pushover(self, title: str, body: str, url: str, urgent: bool) -> None:
        if not urgent:
            priority = -1  # 每日汇总静默送达
        elif self.in_night():
            priority = self.po_night_priority
        else:
            priority = self.po_urgent_priority
        payload = {
            "token": self.po_token,
            "user": self.po_user,
            "title": title[:250],
            "message": (body or title)[:1024],
            "priority": priority,
        }
        if url:
            payload["url"] = url[:512]
            payload["url_title"] = "打开 eBay"
        if priority == 2:
            payload["retry"] = self.po_retry
            payload["expire"] = self.po_expire
        if self.po_sound and priority >= 0:
            payload["sound"] = self.po_sound
        try:
            r = requests.post("https://api.pushover.net/1/messages.json", data=payload, timeout=10)
            if r.status_code != 200:
                # 只打印 Pushover 返回的错误说明,不打印请求内容(里面有 token)
                try:
                    errors = r.json().get("errors")
                except ValueError:
                    errors = r.text[:200]
                print(f"⚠️  Pushover 推送失败: HTTP {r.status_code} {errors}")
        except requests.RequestException as exc:
            print(f"⚠️  Pushover 推送失败: {type(exc).__name__}")


    def in_night(self, now: datetime | None = None) -> bool:
        if not self.po_night:
            return False
        start, end = self.po_night
        t = (now or datetime.now()).time()
        if start <= end:
            return start <= t < end
        return t >= start or t < end  # 跨午夜,如 23:00-07:00


def _parse_window(raw: str) -> tuple[time, time] | None:
    """'00:00-08:00' -> (00:00, 08:00);留空或格式不对返回 None(不启用夜间时段)。"""
    raw = raw.strip()
    if not raw:
        return None
    try:
        a, b = (x.strip() for x in raw.split("-"))
        start = datetime.strptime(a, "%H:%M").time()
        end = datetime.strptime(b, "%H:%M").time()
    except ValueError:
        print(f"⚠️  PUSHOVER_NIGHT={raw!r} 格式不对(应为 00:00-08:00),不启用夜间时段")
        return None
    return (start, end) if start != end else None


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(os.getenv(name, "").strip() or default)
    except ValueError:
        print(f"⚠️  {name} 不是整数,改用默认值 {default}")
        return default
    return max(lo, min(hi, v))
