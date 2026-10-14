# eBay 补货监控

定时检查一个 eBay 商品链接,从缺货变成有货时:

- macOS 桌面通知(带提示音)
- 自动在浏览器打开商品页
- 可选:通过 [ntfy.sh](https://ntfy.sh) 推送到手机(免费、不用注册)

## 快速开始

```bash
cd tools/ebay-stock-watcher
pip install -r requirements.txt

# 先跑一次,确认能正确识别当前状态(应输出 OUT_OF_STOCK)
python3 watch_ebay.py https://www.ebay.co.uk/itm/327393276571 --once

# 持续监控,约每 2 分钟检查一次
python3 watch_ebay.py https://www.ebay.co.uk/itm/327393276571 --ntfy-topic 起一个别人猜不到的名字
```

手机推送:在手机上安装 ntfy App(iOS/Android),订阅和 `--ntfy-topic`
同名的主题即可。主题名相当于密码,任何知道它的人都能看到/发送消息,所以别用太简单的名字。

## 更稳定的方式:eBay 官方 API(推荐)

直接抓网页偶尔会被 eBay 的反爬拦截(脚本会记为 UNKNOWN,不会误报,但可能漏掉补货窗口)。
到 <https://developer.ebay.com> 免费注册开发者账号,创建一个 Production keyset,然后:

```bash
export EBAY_CLIENT_ID=你的 App ID
export EBAY_CLIENT_SECRET=你的 Cert ID
python3 watch_ebay.py <链接> --once   # 输出里应带 "API:" 前缀
```

设置了这两个环境变量后,脚本会自动改用 Browse API 查询库存。

## 开机常驻(macOS LaunchAgent)

和 `xero-dhl-invoice/watch_folder.py` 一样,可以做成 LaunchAgent 后台运行。
`~/Library/LaunchAgents/com.bluebridgewell.ebay-stock-watcher.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.bluebridgewell.ebay-stock-watcher</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>-u</string>
    <string>/完整路径/tools/ebay-stock-watcher/watch_ebay.py</string>
    <string>https://www.ebay.co.uk/itm/327393276571</string>
    <string>--ntfy-topic</string><string>你的主题名</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/ebay-stock-watcher.log</string>
  <key>StandardErrorPath</key><string>/tmp/ebay-stock-watcher.err</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.bluebridgewell.ebay-stock-watcher.plist
tail -f /tmp/ebay-stock-watcher.log
# 买到之后停掉:
launchctl unload ~/Library/LaunchAgents/com.bluebridgewell.ebay-stock-watcher.plist
```

需要 API 方式的话,在 plist 里加 `EnvironmentVariables` 字典放 `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET`。

## 注意

- 间隔不要设得太短(默认 120 秒 ±20% 随机抖动),太频繁容易被 eBay 拦截。
- 脚本只负责通知,不会自动下单。
- 电脑睡眠时脚本也会暂停;想 24 小时监控需要让 Mac 保持唤醒,或者放到一直开着的机器上运行。
