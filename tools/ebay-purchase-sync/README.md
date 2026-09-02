# eBay 购买历史 → Excel 自动同步

eBay 没有给普通买家提供"购买报表"导出(只能一单一单点进详情页看),这个工具用
Playwright 打开一个真实浏览器、复用你手动登录一次后保存的登录状态,自动把
Purchase history 里每一单的详情页抓下来,写进本地 Excel,常驻轮询就能实现"有
新订单自动补进表格"。

包含字段:卖家名称、订单日期、订单号、物流 Tracking Number、产品名称、产品金
额、产品数量、VAT 金额、Buyer Protection Fee、Postage Fee,外加订单详情链接
(方便核对)和同步时间。一个订单里有几件不同商品,就会写几行,订单级别的字
段(卖家/订单号/tracking/VAT/postage/buyer protection)在这几行上重复填。

## 重要声明

- 这是非官方的**页面抓取**,不是 eBay 官方 API(eBay 没有面向普通买家的购买历
  史 API)。只应该用来同步**你自己账号**的购买记录做个人记账,不要拿去抓别人
  的数据、不要调太高频率。
- eBay 页面改版可能会让抓取失效或抓错字段。`ebay_scraper.py` 里刻意没有依赖
  固定的 CSS 选择器,而是按页面上的英文文案("Order number"、"Tracking
  number"...)去匹配,相对更抗改版,但没法保证 100% 不受影响 —— **我没有你的
  eBay 账号,没法登录实际跑一遍验证选择器**,第一次用的时候务必按下面"六、
  第一次跑完务必人工核对"这步操作。
- 抓到的金额、VAT、fee 这些数字仅供个人记账参考,报税/对账等正式用途请以 eBay
  官方页面/账单为准。

## 目录结构

```
ebay_scraper.py   核心抓取逻辑:打开订单列表 -> 逐单打开详情页 -> 解析成结构化数据
excel_sync.py     把订单数据增量写入 Excel,按"订单号+商品+数量+金额"去重
ebay_login.py     首次登录:弹出浏览器,你手动登录一次,把登录状态存到本地
main.py           跑一次同步(抓取 + 写 Excel)
watch_sync.py     常驻轮询,定时自动跑 main.py 的同步逻辑
```

## 一、环境准备

```bash
cd tools/ebay-purchase-sync
pip install -r requirements.txt
playwright install chromium   # 第一次用 Playwright 需要下载一份 Chromium
cp .env.example .env
```

打开 `.env`,把 `EBAY_DOMAIN` 改成你实际购物用的 eBay 站点(比如 `ebay.co.uk`、
`ebay.com`),其余默认值先不用动。

## 二、首次登录(一次性,之后自动复用)

```bash
python3 ebay_login.py
```

会弹出一个真实的浏览器窗口,你在里面正常登录你的 eBay 账号(账号密码、两步验
证、人机验证都在这个窗口里手动完成)。登录完成后回到终端按回车,脚本会把登录
后的 cookies 存到 `storage_state.json`(已加入 `.gitignore`,不会被提交)。

之后 `main.py` / `watch_sync.py` 都会直接复用这份登录状态,不用再登录 —— 除非
某天 cookies 过期或者你在别处把这个会话登出了,届时重新跑一次这个脚本就行。

## 三、跑一次同步

```bash
python3 main.py              # 默认抓今年 + 去年
python3 main.py 2026 2025 2024   # 指定年份
```

跑完会打印类似:

```
✅ 本次共抓到 23 笔订单,新增 31 行商品明细到 ./ebay_purchases.xlsx
   5 笔订单已经同步过,跳过(没有新商品行)
```

`ebay_purchases.xlsx` 就在当前文件夹下(路径由 `.env` 里 `EXCEL_PATH` 控制),
用 Excel/Numbers 打开就能看到「购买记录」这张表。

## 四、进阶:常驻自动同步(你要的"新订单自动同步")

eBay 没有"有新订单就推送通知"这种机制,所以只能定时轮询。让脚本一直留在后台
跑:

```bash
python3 watch_sync.py
```

默认每 6 小时(`.env` 里 `SYNC_INTERVAL_MINUTES`)自动跑一次同步,已经同步过的
订单/商品行不会重复写入,新订单会自动补进 Excel。这个终端窗口要一直留着(可以
缩小,别关掉),按 `Control+C` 停止。

想做成开机自动在后台运行(不用手动开终端),可以配置成 macOS 的 LaunchAgent 登
录启动项 —— 具体做法可以参考同一个仓库里 `tools/xero-dhl-invoice` 的部署方式,
想帮你配的话告诉我。

## 五、Excel 文件是"增量写"的,可以放心手动编辑

同步逻辑只会往表格**末尾追加新行**,不会动你已有的行,所以你可以在这份 Excel
里自己加筛选、加汇总公式、按卖家做透视表,不用担心下次同步把你的改动覆盖掉。

## 六、第一次跑完务必人工核对

因为没法在开发时登录你的账号实际测试,`main.py` 跑出来的第一批数据,建议:

1. 挑 2-3 笔订单,对照 Excel 里的行和 eBay 网页上的订单详情页(表格最后一列
   有直接链接),核对卖家名称、金额、VAT、buyer protection fee、postage fee
   是不是都对得上。
2. 如果发现某个字段抓空了或者抓错了,大概率是 eBay 那个字段的英文文案跟
   `ebay_scraper.py` 顶部 `LABELS` 字典里列的候选词对不上 —— 把页面上实际看到
   的文案加进对应的候选词列表里就行,不用改其他解析逻辑。把你看到的实际页面
   文案告诉我,我也可以帮你直接改。

## 已知限制 / 后续可以做的事

- 年份筛选用的是 eBay 购买历史列表页的 `?filter=year:YYYY` 这个参数,如果 eBay
  之后改了列表页的筛选方式,`list_order_detail_links()` 里的 URL 拼接和"加载
  更多"按钮识别可能需要跟着调整。
- 金额解析对英镑/美元(点做小数点)和欧元区常见的"逗号做小数点"两种写法都做
  了处理,但没覆盖到的小语种站点写法可能还是会解析错,发现了告诉我加进去。
- 目前商品行的去重键是"订单号+商品名+数量+单价"四项都相同才算重复;如果同一
  单里买了两件"完全同名同价同数量"的不同批次商品,可能会被误判成重复而漏
  记 —— 这种情况比较少见,真遇到了可以在 `excel_sync.py` 的 `DEDUP_COLUMNS`
  里加一列区分度更高的字段(比如订单详情链接)。
