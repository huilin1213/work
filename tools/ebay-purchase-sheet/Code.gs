/**
 * eBay purchase log -> Google Sheet.
 *
 * Reads eBay "Order confirmed" emails from Gmail, appends one row per
 * purchased item to the sheet, and marks rows cancelled/refunded when the
 * matching eBay notification arrives. Runs daily at 09:00 and 20:00 (Europe/London)
 * via a time-driven trigger installed by setup().
 *
 * Install: bind this script to the target spreadsheet (Extensions > Apps
 * Script), paste this file, then run setup() once and grant permissions.
 */

const CONFIG = {
  SHEET_NAME: 'eBay采购记录',
  TIMEZONE: 'Europe/London',
  // Hours (24h, TIMEZONE) at which dailySync runs every day.
  TRIGGER_HOURS: [9, 20],
  // setup() backfills everything since this date (Gmail search syntax).
  BACKFILL_SINCE: '2026/07/01',
  // Daily runs re-scan this window; already-recorded orders are skipped, so
  // a missed day or two is caught up automatically.
  DAILY_WINDOW: 'newer_than:7d',
  SENDERS: 'from:(ebay@ebay.com OR ebay@ebay.co.uk)',
};

const HEADERS = [
  '下单时间', '订单号', 'Item ID', '商品标题', '品牌', '单条容量', '频率',
  '料号', '每件条数(估)', '购买数量', '卖家', '单价', '其中买家保护费',
  '运费(整单)', '实付总额(整单)', '状态', '预计送达', 'eBay订单链接',
  'Gmail邮件', '备注(手填)',
];
const COL = {};
HEADERS.forEach(function (h, i) { COL[h] = i + 1; });

const STATUS = { ORDERED: '已下单', CANCELLED: '已取消', REFUNDED: '已退款' };

// ---------------------------------------------------------------------------
// Entry points
// ---------------------------------------------------------------------------

/** Run once by hand: creates the tab, backfills history, installs the trigger. */
function setup() {
  getSheet_();
  const added = sync_('after:' + CONFIG.BACKFILL_SINCE);
  installTrigger_();
  Logger.log('Setup done. Backfilled %s item rows. Daily triggers at %s:00 %s.',
    added, CONFIG.TRIGGER_HOURS.join(':00, '), CONFIG.TIMEZONE);
}

/** Run by hand after changing CONFIG.TRIGGER_HOURS: reinstalls the schedule only. */
function updateSchedule() {
  installTrigger_();
  Logger.log('Daily triggers now at %s:00 %s.', CONFIG.TRIGGER_HOURS.join(':00, '), CONFIG.TIMEZONE);
}

/** Called by the daily trigger. */
function dailySync() {
  const added = sync_(CONFIG.DAILY_WINDOW);
  Logger.log('Daily sync: %s new item rows.', added);
  // Direct-supplier invoices (Direct.gs) share this trigger; a failure there
  // must not look like an eBay sync failure, so it is logged separately.
  if (typeof syncDirect_ === 'function') {
    try {
      Logger.log('Direct sync: %s new rows.', syncDirect_());
    } catch (e) {
      Logger.log('Direct sync failed: %s', e);
    }
  }
}

/**
 * Run by hand after updating the parser: re-derives 品牌/单条容量/频率/料号/
 * 每件条数 from each row's title, filling only cells that are still empty so
 * hand-entered values are kept.
 */
function reparseSpecs() {
  const sheet = getSheet_();
  const n = sheet.getLastRow() - 1;
  if (n < 1) return;
  const titles = sheet.getRange(2, COL['商品标题'], n, 1).getValues();
  const range = sheet.getRange(2, COL['品牌'], n, 5);
  const cells = range.getValues();
  let filled = 0;
  titles.forEach(function (r, i) {
    const s = parseSpec(String(r[0]));
    [s.brand, s.capacity, s.speed, s.partNo, s.perListing].forEach(function (v, j) {
      if (cells[i][j] === '' && v !== '') { cells[i][j] = v; filled++; }
    });
  });
  range.setValues(cells);
  Logger.log('reparseSpecs: filled %s empty cells.', filled);
}

// ---------------------------------------------------------------------------
// Sync
// ---------------------------------------------------------------------------

function sync_(window) {
  const sheet = getSheet_();
  const index = indexRows_(sheet);

  const newRows = [];
  const confirmQuery = CONFIG.SENDERS + ' subject:"Order confirmed" ' + window;
  searchMessages_(confirmQuery).forEach(function (msg) {
    const parsed = parseOrderEmail(msg.getBody());
    parsed.items.forEach(function (item) {
      const key = item.orderNo + '|' + item.itemId;
      if (index.byKey[key]) return;
      index.byKey[key] = true;
      newRows.push(buildRow_(msg, parsed, item));
    });
  });

  if (newRows.length) {
    sheet.getRange(sheet.getLastRow() + 1, 1, newRows.length, HEADERS.length)
      .setValues(newRows);
  }

  applyStatusUpdates_(sheet, window);

  if (sheet.getLastRow() > 2) {
    sheet.getRange(2, 1, sheet.getLastRow() - 1, HEADERS.length)
      .sort({ column: COL['下单时间'], ascending: false });
  }
  return newRows.length;
}

function buildRow_(msg, parsed, item) {
  const spec = parseSpec(item.title);
  return [
    msg.getDate(),
    item.orderNo,
    item.itemId,
    item.title,
    spec.brand,
    spec.capacity,
    spec.speed,
    spec.partNo,
    spec.perListing,
    item.qty,
    item.seller,
    item.price,
    item.fee,
    parsed.postage,
    parsed.total,
    STATUS.ORDERED,
    parsed.eta,
    item.orderNo ? 'https://order.ebay.co.uk/ord/show?orderid=' + item.orderNo : '',
    'https://mail.google.com/mail/u/0/#all/' + msg.getId(),
    '',
  ];
}

/** Marks rows cancelled / refunded from eBay's notification emails. */
function applyStatusUpdates_(sheet, window) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return;
  const query = CONFIG.SENDERS +
    ' (subject:cancelled OR subject:"Refund issued") ' + window;
  const updates = [];
  searchMessages_(query).forEach(function (msg) {
    const u = parseStatusSubject(msg.getSubject());
    if (u) updates.push(u);
  });
  if (!updates.length) return;

  const data = sheet.getRange(2, 1, lastRow - 1, HEADERS.length).getValues();
  const statusRange = sheet.getRange(2, COL['状态'], lastRow - 1, 1);
  const statuses = statusRange.getValues();
  let changed = false;
  data.forEach(function (row, i) {
    const orderNo = String(row[COL['订单号'] - 1]);
    const itemId = String(row[COL['Item ID'] - 1]);
    updates.forEach(function (u) {
      const hit = (u.orderNo && u.orderNo === orderNo) ||
                  (u.itemId && u.itemId === itemId);
      // Only overwrite the automatic default, never a status typed by hand.
      if (hit && statuses[i][0] === STATUS.ORDERED) {
        statuses[i][0] = u.status;
        changed = true;
      }
    });
  });
  if (changed) statusRange.setValues(statuses);
}

// ---------------------------------------------------------------------------
// Sheet helpers
// ---------------------------------------------------------------------------

function getSheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(CONFIG.SHEET_NAME);
  if (!sheet) sheet = ss.insertSheet(CONFIG.SHEET_NAME, 0);
  if (sheet.getLastRow() === 0) {
    sheet.getRange(1, 1, 1, HEADERS.length).setValues([HEADERS])
      .setFontWeight('bold').setBackground('#d9e2f3');
    sheet.setFrozenRows(1);
    sheet.getRange('A:A').setNumberFormat('yyyy-mm-dd hh:mm');
    sheet.getRange(1, COL['单价'], sheet.getMaxRows(), 4).setNumberFormat('£#,##0.00');
    sheet.getRange(1, COL['订单号'], sheet.getMaxRows(), 2).setNumberFormat('@');
    sheet.setColumnWidth(COL['商品标题'], 380);
    const rule = SpreadsheetApp.newDataValidation()
      .requireValueInList(['已下单', '已发货', '已收到/已检查', '有瑕疵', '已取消', '已退款'], true)
      .setAllowInvalid(true).build();
    sheet.getRange(2, COL['状态'], sheet.getMaxRows() - 1, 1).setDataValidation(rule);
  }
  return sheet;
}

function indexRows_(sheet) {
  const byKey = {};
  const lastRow = sheet.getLastRow();
  if (lastRow >= 2) {
    sheet.getRange(2, COL['订单号'], lastRow - 1, 2).getValues().forEach(function (r) {
      byKey[String(r[0]) + '|' + String(r[1])] = true;
    });
  }
  return { byKey: byKey };
}

function searchMessages_(query) {
  const out = [];
  for (let start = 0; ; start += 100) {
    const threads = GmailApp.search(query, start, 100);
    threads.forEach(function (t) {
      t.getMessages().forEach(function (m) { out.push(m); });
    });
    if (threads.length < 100) break;
  }
  return out;
}

function installTrigger_() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'dailySync') ScriptApp.deleteTrigger(t);
  });
  CONFIG.TRIGGER_HOURS.forEach(function (hour) {
    ScriptApp.newTrigger('dailySync').timeBased()
      .atHour(hour).nearMinute(0).everyDays(1)
      .inTimezone(CONFIG.TIMEZONE).create();
  });
}

// ---------------------------------------------------------------------------
// Pure parsers (no Apps Script services; unit-tested with node in test.js)
// ---------------------------------------------------------------------------

const MONEY_RE = '(?:£|€|US \\$|AU \\$|C \\$|\\$)\\s?[\\d,]+\\.\\d{2}';

function parseMoney(s) {
  if (!s) return '';
  const n = parseFloat(String(s).replace(/[^\d.]/g, ''));
  return isNaN(n) ? '' : n;
}

function decodeEntities(s) {
  return s
    .replace(/&nbsp;/g, ' ').replace(/&pound;/g, '£').replace(/&euro;/g, '€')
    .replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&#(\d+);/g, function (_, d) { return String.fromCharCode(+d); })
    .replace(/&#x([0-9a-f]+);/gi, function (_, h) { return String.fromCharCode(parseInt(h, 16)); })
    .replace(/&amp;/g, '&');
}

function htmlToText(html) {
  const noBlocks = html.replace(/<(style|script|head)\b[\s\S]*?<\/\1>/gi, ' ');
  return decodeEntities(noBlocks
      .replace(/<br\s*\/?>/gi, '\n')
      .replace(/<\/(p|div|tr|td|h\d|li|table)>/gi, '\n')
      .replace(/<[^>]+>/g, ' '))
    .replace(/[ \t ͏​]+/g, ' ')
    .replace(/ *\n[\s]*/g, '\n')
    .trim();
}

/** Title of item `itemId`: text of the first non-empty link to /itm/<itemId>. */
function findTitle(html, itemId) {
  const re = new RegExp('<a\\b[^>]*/itm/' + itemId + '[^>]*>([\\s\\S]*?)</a>', 'gi');
  let m;
  while ((m = re.exec(html)) !== null) {
    const t = htmlToText(m[1]).replace(/\s+/g, ' ').trim();
    if (t) return t;
  }
  return '';
}

/**
 * Parses an eBay "Order confirmed" email body (HTML, or the plain-text
 * rendering of it). One entry in `items` per "Price: ... Item ID ... Order
 * number ... Seller" block, so multi-item checkouts give multiple rows.
 */
function parseOrderEmail(body) {
  const isHtml = /<[a-z][\s\S]*>/i.test(body);
  const text = isHtml ? htmlToText(body) : body;

  const totalsAt = text.search(/Order total:/i);
  const itemsText = totalsAt >= 0 ? text.slice(0, totalsAt) : text;
  const totalsText = totalsAt >= 0 ? text.slice(totalsAt) : text;

  const priceRe = new RegExp('Price:\\s*(' + MONEY_RE + ')' +
    '(?:\\s*incl\\.\\s*(' + MONEY_RE + ')\\s*Buyer Protection fee)?', 'gi');
  const starts = [];
  let m;
  while ((m = priceRe.exec(itemsText)) !== null) {
    starts.push({ at: m.index, price: m[1], fee: m[2] });
  }

  const items = [];
  starts.forEach(function (s, i) {
    const end = i + 1 < starts.length ? starts[i + 1].at : itemsText.length;
    const own = itemsText.slice(s.at, end);
    const itemId = (own.match(/Item ID:\s*(\d{6,})/i) || [])[1] || '';
    const orderNo = (own.match(/Order number:\s*([\d-]{8,})/i) || [])[1] || '';
    if (!itemId && !orderNo) return;
    const seller = (own.match(/Seller:\s*\[?([^\s\[\]|(]+)/i) || [])[1] || '';
    const qty = (own.match(/Quantity:\s*(\d+)/i) || [])[1];
    items.push({
      itemId: itemId,
      orderNo: orderNo,
      seller: seller,
      price: parseMoney(s.price),
      fee: parseMoney(s.fee),
      qty: qty ? +qty : 1,
      title: isHtml ? findTitle(body, itemId) : '',
    });
  });

  const money = function (label) {
    const r = new RegExp(label + '[^£€$]{0,40}?(' + MONEY_RE + ')', 'i');
    return parseMoney((totalsText.match(r) || [])[1]);
  };
  const eta = (text.match(/Estimated delivery:\s*\n?\s*([^\n]+)/i) || [])[1] || '';

  return {
    items: items,
    subtotal: money('Subtotal'),
    postage: money('Postage'),
    total: money('Total charged'),
    eta: eta.trim(),
  };
}

/** Status change from a cancellation / refund email subject, or null. */
function parseStatusSubject(subject) {
  let m = subject.match(/Refund issued for order ([\d-]{8,})/i);
  if (m) return { orderNo: m[1], status: STATUS.REFUNDED };
  m = subject.match(/Order (\d{2}-\d{5}-\d{5}).*cancel/i);
  if (m) return { orderNo: m[1], status: STATUS.CANCELLED };
  m = subject.match(/Order (\d{9,})-\d+ has been cancel/i);
  if (m) return { itemId: m[1], status: STATUS.CANCELLED };
  return null;
}

const PC4_SPEED = { '17000': '2133', '19200': '2400', '21300': '2666', '23400': '2933', '25600': '3200' };
const BRANDS = [
  [/sk\s*-?hynix|hynix/i, 'SK Hynix'], [/samsung/i, 'Samsung'], [/micron/i, 'Micron'],
  [/crucial/i, 'Crucial'], [/integral/i, 'Integral'], [/kingston/i, 'Kingston'], [/\bdell\b/i, 'Dell'],
  [/\bhpe?\b/i, 'HP'], [/lenovo/i, 'Lenovo'], [/nanya/i, 'Nanya'],
];

/** Best-effort RAM spec extraction from a listing title; blanks when unsure. */
function parseSpec(title) {
  const t = title || '';
  let brand = '';
  for (let i = 0; i < BRANDS.length; i++) {
    if (BRANDS[i][0].test(t)) { brand = BRANDS[i][1]; break; }
  }

  let perListing = '';
  let capacity = '';
  const kit = t.match(/(\d+)\s*x\s*(\d+)\s*GB/i);            // "4x32GB"
  const lead = t.match(/^\s*(\d+)\s*x\s+/i);                  // "5x SK Hynix 32GB"
  if (kit) { perListing = +kit[1]; capacity = kit[2] + 'GB'; }
  else {
    const tail = t.match(/\bx\s*(\d+)\s*$/i);                // "... MTA36ASF4G72PZ X 16"
    if (lead) perListing = +lead[1];
    else if (tail) perListing = +tail[1];
    const cap = t.match(/(\d+)\s*GB/i);
    if (cap) capacity = cap[1] + 'GB';
  }

  let speed = '';
  const pc4 = t.match(/PC4-?(\d{5})/i);
  const mhz = t.match(/\b(1600|1866|2133|2400|2666|2933|3200|4800|5600|6400)\s*(?:MHz|MT\/s|[A-Z]{0,2}\b)/i);
  if (pc4 && PC4_SPEED[pc4[1]]) speed = PC4_SPEED[pc4[1]];
  else if (mhz) speed = mhz[1];
  else {
    const rc = t.match(/(?:PC4|RC4|DDR4)-?(2133|2400|2666|2933|3200)/i);
    if (rc) speed = rc[1];
  }

  // Dash suffix is optional and sellers sometimes put a space before it
  // ("HMA84GR7CJR4N -VK").
  const part = t.match(/\b((?:HMAA?|HMCG|M3[29]3|M321|MTA|IN4T)[0-9A-Z]{6,}(?:\s?-[0-9A-Z]+)*|KSM[0-9A-Z\/]+)/i);

  return {
    brand: brand,
    capacity: capacity,
    speed: speed,
    partNo: part ? part[1].replace(/\s/g, '').toUpperCase() : '',
    perListing: perListing,
  };
}

if (typeof module !== 'undefined') {
  module.exports = { parseOrderEmail: parseOrderEmail, parseStatusSubject: parseStatusSubject,
    parseSpec: parseSpec, htmlToText: htmlToText, findTitle: findTitle };
}
