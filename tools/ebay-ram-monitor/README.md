# eBay DDR4 服务器内存监控

用 eBay 官方 Browse API 盯「DDR4 server RAM」,发现以下情况立刻推送到手机(ntfy / Telegram):

| 推送 | 含义 |
|---|---|
| 🆕 新上架 | 刚上架、第一次出现的链接 |
| 🆕 新上架(补漏) | eBay 索引延迟、快速轮询没赶上,全量扫描补到的 |
| 📦 老链接重新出现(可能补货) | 很早以前创建、但之前一直搜不到的链接突然出现 —— 通常是卖完隐藏的 GTC 链接补了货 |
| 📦 重新上架/补货 | 之前见过、后来从搜索里消失,现在又出现了 |
| 📦 补货 | 多数量链接的可售数量变多 / 从缺货恢复有货 |
| 💲 降价 | 已知链接降价超过设定百分比 |

不按型号定价、不做筛选判断 —— 目标是**一条都不漏**,买不买你自己看推送决定。
程序不会自动下单(eBay 的下单 API 需要单独审批,用脚本模拟网页下单违反用户协议,会危及账号)。

## 安装

```bash
cd tools/ebay-ram-monitor
pip install -r requirements.txt
cp .env.example .env      # 填 EBAY_CLIENT_ID / EBAY_CLIENT_SECRET 和推送渠道
```

`EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` 在 developer.ebay.com → Application Keys →
**Production** 那一栏(App ID / Cert ID)。

Production 密钥要先处理 Marketplace Account Deletion 通知才能启用。本工具**不保存任何
eBay 用户数据**(卖家信息只出现在推送里,不写进数据库),所以可以选择豁免(opt out)。

推送二选一(或都配):
- **ntfy**(最简单):手机装 ntfy App,订阅一个别人猜不到的 topic 名(如 `ram-bbw-8f3k2x`),
  把同样的名字填进 `NTFY_TOPIC`。
- **Telegram**:@BotFather 建 bot 拿 token;给 bot 发一条消息后,用 @userinfobot 查自己的 chat_id。

## 使用

```bash
python3 monitor.py --test-notify   # 1. 测试手机能否收到推送
python3 find_category.py           # 2.(可选)查服务器内存类目 ID,填 EBAY_CATEGORY_IDS
python3 monitor.py --once          # 3. 看搜索词搜出来的结果对不对、总共多少条
python3 monitor.py                 # 4. 常驻运行
```

某条 listing 没收到推送?运行 `python3 check_item.py <item 号或链接>`,
会逐项对照类目/发货地/购买方式/价格/搜索词/排除词,并显示程序什么时候第一次看到它。

第一次运行会先把现有的所有链接静默扫一遍建立基线(不推送),之后才推送变化。
数据存在 `ebay_monitor.db`(SQLite,已 gitignore)。删掉它 = 重新建立基线。

## 工作原理

三个任务共用一个限速器,保证不超过每日配额(`EBAY_DAILY_CALL_BUDGET`):

1. **快速轮询**(每 `FAST_POLL_SECONDS` 秒,最高优先级):按最新上架排序拉第一页 200 条。
2. **全量扫描**(每 `SWEEP_MINUTES` 分钟):翻完所有结果(API 上限 10000 条),
   补漏 + 发现隐藏后重新出现的老链接。连续 2 次完整扫描都没出现才判定"消失",避免翻页抖动误报。
3. **库存复查**(用剩余配额):`getItems` 每次 20 条,对比可售/已售数量。
   已售数量增加(别人买走)不推送,只记在 `events` 表里,以后可以分析哪些链接竞争最激烈。

启动时会打印配额估算。默认 5000 次/天的配额下,每分钟轮询一次 ≈ 1440 次,
全量扫描取决于结果数量(5000 条 = 25 页/次,每小时一次 ≈ 600 次/天)。
**想要更快,最重要的一步是在 eBay 开发者后台申请 Application Growth Check 提高配额**,
提额后调大 `EBAY_DAILY_CALL_BUDGET`、调小 `FAST_POLL_SECONDS`。

## 调整搜索范围

- 默认搜索词 `DDR4 (server,ECC,RDIMM,LRDIMM,registered,REG)` = 含 DDR4,并且含括号里任意一个词。
  多个搜索词用 `;` 分隔,例如再加一条 `PC4 (2400T,2666V,2933Y,3200AA) R`。
- 结果超过 10000 条时全量扫描只能看到最新的 10000 条,启动日志会提示;
  加 `EBAY_CATEGORY_IDS`、`MAX_PRICE` 或拆成更窄的搜索词即可。
- `EXCLUDE_KEYWORDS` 里的词出现在标题中就不推送(仍会记录)。

## 部署成后台常驻(macOS)

```bash
bash launchagent.sh install     # 装成 LaunchAgent:登录自动运行、出错 60 秒后自动重启
bash launchagent.sh status      # 是否在运行 + 最近日志
bash launchagent.sh log         # 实时看日志(~/Library/Logs/ebay-ram-monitor.log)
bash launchagent.sh restart     # 改了 .env 或 git pull 之后
bash launchagent.sh uninstall
```

用 `caffeinate -i` 包着运行,服务在跑时 Mac 不会自动睡眠(合盖仍会睡)。
项目不要放在 桌面/文稿/下载 里,macOS 隐私保护会挡住后台服务读取。
安装前先停掉终端里手动运行的那个,两个同时跑会重复推送、共用 API 配额。
