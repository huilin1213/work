# eBay 发票批量下载

翻你的 eBay 购买记录(Purchase history),把有发票/Tax invoice 链接的订单页面
存成 PDF,省掉一张一张手动截图的功夫。存下来的 PDF 拖进(或者直接指向)Google
Drive 的 `VAT ACCOUNTING/AUGUST/eBay simple invoice` 文件夹,后面就能接
`tools/vat-reconciliation/` 那个对账工具。

## 为什么这个脚本要在你自己电脑上跑,不是我在云端直接跑

- eBay 对陌生 IP/陌生设备登录基本必然会要求验证码/短信验证,云端无界面环境没法
  替你完成这一步
- 登录状态存在你自己电脑本地(`.ebay_state.json`,不是密码,已加入 .gitignore
  不会被提交),不经过任何第三方

## ⚠️ 重要:这版还没在真实 eBay 页面上跑通过

写这个脚本的环境连不了 ebay.co.uk(网络出口被挡了),没法照着真实页面结构调
选择器。用的是"按链接文字里有没有 invoice 字样""按钮文字是不是 show more"这种
相对通用、不依赖具体 CSS class 的写法,理论上比精确 class 名字抗版式更新,但
**没法保证第一次跑就一次成功**。第一次用请务必按下面"调试模式"跑,卡住的地方
会自动停在 Playwright Inspector 里,把真实情况告诉我,我再照着改。

## 一、环境准备

```bash
cd tools/ebay-invoice-downloader
pip install -r requirements.txt
cp .env.example .env   # 填 EBAY_MONTH / EBAY_OUTPUT_DIR
```

`EBAY_OUTPUT_DIR` 建议直接填 Google Drive 桌面同步版本地映射出来的
`eBay simple invoice` 文件夹路径 —— 这样下载完就是自动同步到云端,不用再手动拖
一次。没装 Drive 桌面同步版的话,先填个本地文件夹,下完自己拖到 Drive 网页版。

## 二、首次登录(一次性)

```bash
python3 ebay_login.py
```

会弹出一个真实浏览器窗口,你在里面手动登录 eBay(包括任何验证码/短信验证都在
这个窗口里完成)。登录成功后回终端按回车,登录状态就保存好了。

## 三、下载发票

**第一次务必先用调试模式,小范围试跑:**

```bash
python3 download_invoices.py --debug --limit 3
```

`--debug` 会打开一个可见的浏览器窗口,你能看到脚本在做什么;如果购买记录页面
没有按预期显示订单列表(比如默认只显示最近 3 个月,翻不到 8 月),脚本会先停
下来等你在这个窗口里手动操作(比如手动把日期筛选调对),操作完回终端按回车让
脚本继续。`--limit 3` 只处理前 3 张,不会一次把所有订单都跑一遍。

确认前几张下载下来的 PDF 内容、文件名(按订单号命名)都对,再去掉这两个参数,
跑全量:

```bash
python3 download_invoices.py
```

脚本会:
1. 打开购买记录页面,翻页/滚动收集所有"链接文字带 invoice"的订单
2. 依次打开每一个,读页面上的日期,只保留 `.env` 里 `EBAY_MONTH` 那个月份的
3. 存成 PDF,文件名是订单号(比如 `eBay_27-15097-28255.pdf`),方便跟银行流水
   按订单号对应
4. 记一份"已下载订单号"清单(`.ebay_downloaded.json`),重复跑不会重复下载

## 已知限制

- 没有真实页面验证过,选择器大概率需要跟着实际情况调一两次(见上面"⚠️ 重要")
- 只抓"链接文字里带 invoice"的订单,没有发票链接的订单(比如 private seller
  又没有 Buyer Protection Fee 的那种)本来就不会有对应文档,不会出现在下载结果
  里——不是漏抓,是这类订单确实没有 eBay 生成的发票文件
- 如果购买记录默认只显示最近几个月,翻不到目标月份,需要手动把 `.env` 里的
  `EBAY_PURCHASE_URL` 填成你在浏览器里手动调好日期筛选后地址栏的完整网址
