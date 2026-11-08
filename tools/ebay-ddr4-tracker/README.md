# eBay DDR4 内存条每日销量追踪

每天晚上 23:00 自动运行,汇总**过去 24 小时** eBay 上 DDR4 内存条的成交情况:
**卖家 / 商品 / 单价 / 数量 / 估算销售额**,输出 Excel + CSV 日报。

## 原理(先看这个,决定了数据能有多准)

eBay 已经不对普通开发者开放"成交记录"接口(旧的 Finding API 已下线,
Marketplace Insights API 需要单独申请商业授权)。所以这个工具用的是公开的
**Browse API**,每天拍一次"快照":

1. 按关键词 + 类目搜出所有在售的 DDR4 一口价商品;
2. 逐个读取 eBay 给出的**累计售出数量**(`estimatedSoldQuantity`);
3. 和前一天的快照相减,差值就是这 24 小时卖出的件数,乘以标价得到估算销售额。

因此:

- **第一次运行只建立基线,没有日报**;从第二天 23:00 起才有数据。
- 单价是快照时的**标价**,Best Offer 议价、运费、退款都不在其中。
- 只统计**一口价**商品;拍卖不统计。
- 统计区间内**新上架**的商品,当天销量没有基线,从第二天才开始算。
- 最后一件卖掉后商品下架,如果 eBay 已查不到它的详情,这最后一笔可能漏计。
- `estimatedSoldQuantity` 本身是 eBay 给的估计值。

整体上,日报是**接近真实成交的下限估计**,用来看谁在卖、卖什么、什么价位走量足够了。

## 申请 eBay API Key(一次性)

1. 打开 <https://developer.ebay.com>,用 eBay 账号注册 Developer Program(免费)。
2. 进入 **Application Keys**,创建一个应用,拿到 **Production** 那一组的
   **App ID (Client ID)** 和 **Cert ID (Client Secret)**。
3. 首次使用 Production key 可能需要先完成
   **Marketplace Account Deletion** 通知设置(或申请豁免),按页面提示操作即可。
4. Browse API 默认额度为每天 5,000 次调用。

## 安装和配置

在 `tools/ebay-ddr4-tracker/` 目录下:

```bash
pip3 install -r requirements.txt
```

```bash
cp .env.example .env
```

然后编辑 `.env`,至少填好 `EBAY_CLIENT_ID` 和 `EBAY_CLIENT_SECRET`。
其他常用选项:

| 变量 | 默认 | 说明 |
|---|---|---|
| `EBAY_MARKETPLACE` / `EBAY_CURRENCY` | `EBAY_GB` / `GBP` | 站点和币种 |
| `SEARCH_QUERY` | `DDR4` | 只看服务器内存可改成 `DDR4 ECC` |
| `EBAY_CATEGORY_ID` | `170083` | Memory (RAM) 类目 |
| `SELLERS` | 空 = 全部卖家 | 只盯几个同行时填用户名,逗号分隔 |
| `PRICE_MIN` / `PRICE_MAX` | `1` / `5000` | 价格范围 |
| `EBAY_MAX_CALLS` | `4500` | 单次运行 API 调用上限 |

先试跑一次,只搜索、不写库,看看能搜到多少商品:

```bash
python3 tracker.py --dry-run
```

**API 用量估算**:搜索每 200 个商品 1 次,读详情每 20 个商品 1 次,
即每 1 万个商品约 550 次调用。如果全站 DDR4 商品太多、超过 4,500 次,
可以把 `SEARCH_QUERY` 改窄(如 `DDR4 ECC`)、提高 `PRICE_MIN`,或只盯 `SELLERS`。

## 运行

```bash
python3 tracker.py
```

- 快照存在 `data/snapshots.sqlite3`(所有历史都在里面,可自行用 SQL 分析)。
- 日报在 `reports/ddr4_sales_YYYY-MM-DD.xlsx` 和 `.csv`,
  Excel 里有「销售明细」「按卖家」「说明」三页。
- 终端/日志里也会打印卖家 Top 10、商品 Top 10。

`data/`、`reports/`、`.env` 都已在 `.gitignore` 里,不会被提交。

## 每天 23:00 自动运行(macOS)

1. 复制模板,并把里面的 `/Users/YOU/...` 改成你电脑上的真实路径:

   ```bash
   cp com.bluebridgewell.ebay-ddr4-tracker.plist.example ~/Library/LaunchAgents/com.bluebridgewell.ebay-ddr4-tracker.plist
   ```

2. 加载:

   ```bash
   launchctl load ~/Library/LaunchAgents/com.bluebridgewell.ebay-ddr4-tracker.plist
   ```

3. 想马上手动触发一次测试:

   ```bash
   launchctl start com.bluebridgewell.ebay-ddr4-tracker
   ```

4. 看日志:

   ```bash
   tail -f ~/Library/Logs/ebay-ddr4-tracker.log
   ```

注意:23:00 时电脑如果在睡眠,macOS 会在唤醒后补跑一次;日报会按实际的
"上次快照 → 本次快照"时间区间统计,区间写在报表的「说明」页里。
