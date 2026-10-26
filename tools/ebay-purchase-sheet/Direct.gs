/**
 * Direct-supplier invoices -> "直采记录" tab.
 *
 * Source: the Drive folder 「直采发票」 and its subfolders (one per supplier,
 * e.g. 直采发票/Steve). Every PDF/image put there (saved from email,
 * WhatsApp, etc.) is OCR'd once and becomes one row; moving a file between
 * subfolders later does not create a second row (rows are keyed by file ID). Email is
 * deliberately not read: supplier threads mix invoices with quotes and
 * chat, and one folder is a predictable single place to drop invoices.
 *
 * OCR uses Drive's free built-in PDF/image -> Google Doc conversion; fields
 * are then pulled out with regex rules, so every row starts as 「待核对」 for
 * a human to check. Runs from dailySync() in Code.gs (same triggers).
 */

const DIRECT = {
  SHEET_NAME: '直采记录',
  FOLDER: '直采发票',
  OCR_LANGUAGE: 'en',
  OWN_COMPANY: /blue\s*bridgewell/i,
};

// One row per invoice line, so each part number has its own 数量/单价.
const DIRECT_HEADERS = [
  '发票日期', '供应商', '发票号', '行号', '商品描述', '品牌', '单条容量', '频率',
  '料号', '数量', '单价', '金额', '发票总额(含VAT)', '不含VAT', '核对', '备注(手填)', '发票文件',
  '同步时间', '文件ID',
];
const DCOL = {};
DIRECT_HEADERS.forEach(function (h, i) { DCOL[h] = i + 1; });

const OCR_MIME = ['application/pdf', 'image/jpeg', 'image/png', 'image/gif'];

// ---------------------------------------------------------------------------
// Entry points
// ---------------------------------------------------------------------------

/** Run once by hand: creates the Drive folder and tab, then syncs. */
function setupDirect() {
  const folder = getDirectFolder_();
  getDirectSheet_();
  const added = syncDirect_();
  Logger.log('Direct setup done. %s rows added. Drive folder: %s', added, folder.getUrl());
}

/** Manual "sync now" for direct invoices only. */
function syncDirectNow() {
  Logger.log('Direct sync: %s new rows.', syncDirect_());
}

// ---------------------------------------------------------------------------
// Sync
// ---------------------------------------------------------------------------

function syncDirect_() {
  const sheet = getDirectSheet_();
  const seen = indexDirectRows_(sheet);
  const rows = [];

  listInvoiceFiles_(getDirectFolder_(), '').forEach(function (f) {
    if (seen[f.file.getId()]) return;
    seen[f.file.getId()] = true;
    Array.prototype.push.apply(rows, buildDirectRows_(ocrFile_(f.file.getId()), f.file, f.supplier));
  });

  if (rows.length) {
    sheet.getRange(sheet.getLastRow() + 1, 1, rows.length, DIRECT_HEADERS.length).setValues(rows);
  }
  sortDirect_(sheet);
  colorInvoices_(sheet);
  return rows.length;
}

/** Manual: re-sort and re-colour, e.g. after editing or deleting rows by hand. */
function recolorDirect() {
  const sheet = getDirectSheet_();
  sortDirect_(sheet);
  colorInvoices_(sheet);
}

/** Newest invoice first; each invoice's lines kept together and in order. */
function sortDirect_(sheet) {
  if (sheet.getLastRow() < 3) return;
  sheet.getRange(2, 1, sheet.getLastRow() - 1, DIRECT_HEADERS.length).sort([
    { column: DCOL['发票日期'], ascending: false },
    { column: DCOL['文件ID'], ascending: true },
    { column: DCOL['行号'], ascending: true },
  ]);
}

// Light colours cycled per invoice; consecutive invoices never share one.
const INVOICE_COLORS = ['#fff2cc', '#ddebf7', '#e2efda', '#fce4d6', '#ede2f6', '#d9f2f2'];

/** Gives every row of the same invoice file one background colour. */
function colorInvoices_(sheet) {
  const n = sheet.getLastRow() - 1;
  if (n < 1) return;
  const ids = sheet.getRange(2, DCOL['文件ID'], n, 1).getValues();
  // Rows of one invoice share a file ID and sit together after sortDirect_.
  const colors = [];
  let k = -1;
  ids.forEach(function (r, i) {
    if (i === 0 || r[0] !== ids[i - 1][0]) k++;
    const row = [];
    for (let c = 0; c < DIRECT_HEADERS.length; c++) row.push(INVOICE_COLORS[k % INVOICE_COLORS.length]);
    colors.push(row);
  });
  sheet.getRange(2, 1, n, DIRECT_HEADERS.length).setBackgrounds(colors);
}

/**
 * Every PDF/image in `folder` and its subfolders. A file inside a subfolder
 * (e.g. 直采发票/Steve/) gets that top-level subfolder's name as supplier.
 */
function listInvoiceFiles_(folder, supplier) {
  const out = [];
  const files = folder.getFiles();
  while (files.hasNext()) {
    const file = files.next();
    if (OCR_MIME.indexOf(file.getMimeType()) >= 0) out.push({ file: file, supplier: supplier });
  }
  const subs = folder.getFolders();
  while (subs.hasNext()) {
    const sub = subs.next();
    Array.prototype.push.apply(out, listInvoiceFiles_(sub, supplier || sub.getName()));
  }
  return out;
}

/** Sheet rows for one invoice file: one per line item, or one placeholder. */
function buildDirectRows_(text, file, folderSupplier) {
  const inv = parseInvoiceText(text);
  const items = parseLineItems(text);
  const name = file.getName();
  // Supplier subfolder name beats OCR: it is what the user chose to call them.
  const supplier = folderSupplier || inv.supplier || name.replace(/\.[^.]+$/, '');
  const check = checkTotals(items, inv);
  const date = parseInvoiceDate(inv.date);
  // Invoice totals go on the invoice's first row only, so summing the
  // columns over the sheet doesn't count an invoice once per line.
  let gross = inv.total, net = inv.net;
  if (net === '' && gross !== '' && inv.vat !== '') net = Math.round((gross - inv.vat) * 100) / 100;
  if (gross === '' && net !== '' && inv.vat !== '') gross = Math.round((net + inv.vat) * 100) / 100;
  const base = function (i, item) {
    const spec = item ? parseSpec(item.desc) : {};
    return [
      date, supplier, inv.invoiceNo, i + 1,
      item ? item.desc : '(未识别到明细，请打开发票)',
      spec.brand || '', spec.capacity || '', spec.speed || '',
      item ? (spec.partNo || partFromDesc(item.desc)) : '',
      item ? item.qty : '', item ? item.price : '', item ? item.amount : '',
      i === 0 ? gross : '', i === 0 ? net : '',
      check, '', file.getUrl(), new Date(), file.getId(),
    ];
  };
  return items.length ? items.map(function (it, i) { return base(i, it); }) : [base(0, null)];
}

/**
 * OCR via Drive: copying a PDF/image as a Google Doc makes Drive run OCR.
 * The temporary Doc is trashed straight after reading.
 */
function ocrFile_(fileId) {
  const resp = UrlFetchApp.fetch(
    'https://www.googleapis.com/drive/v3/files/' + fileId +
      '/copy?fields=id&ocrLanguage=' + DIRECT.OCR_LANGUAGE, {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify({ name: 'tmp-ocr', mimeType: 'application/vnd.google-apps.document' }),
      headers: { Authorization: 'Bearer ' + ScriptApp.getOAuthToken() },
      muteHttpExceptions: true,
    });
  if (resp.getResponseCode() !== 200) {
    Logger.log('OCR failed for %s: %s', fileId, resp.getContentText());
    return '';
  }
  const docId = JSON.parse(resp.getContentText()).id;
  try {
    return docToText_(DocumentApp.openById(docId).getBody());
  } finally {
    DriveApp.getFileById(docId).setTrashed(true);
  }
}

/**
 * Body text with any tables flattened one table row per line (cells joined
 * by tabs), so a table-shaped invoice still reads "desc qty price amount".
 */
function docToText_(body) {
  const out = [];
  for (let i = 0; i < body.getNumChildren(); i++) {
    const el = body.getChild(i);
    const type = el.getType();
    if (type === DocumentApp.ElementType.PARAGRAPH) {
      out.push(el.asParagraph().getText());
    } else if (type === DocumentApp.ElementType.LIST_ITEM) {
      out.push(el.asListItem().getText());
    } else if (type === DocumentApp.ElementType.TABLE) {
      const t = el.asTable();
      for (let r = 0; r < t.getNumRows(); r++) {
        const row = t.getRow(r);
        const cells = [];
        for (let c = 0; c < row.getNumCells(); c++) {
          cells.push(row.getCell(c).getText().replace(/\s+/g, ' ').trim());
        }
        out.push(cells.join('\t'));
      }
    }
    // Anything else (images, drawings, page breaks: "UNSPECIFIED"/
    // "UNSUPPORTED" in OCR output) has no text and cannot be cast; skip it.
  }
  return out.join('\n');
}

// ---------------------------------------------------------------------------
// Sheet / Drive helpers
// ---------------------------------------------------------------------------

function getDirectSheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(DIRECT.SHEET_NAME);
  if (!sheet) sheet = ss.insertSheet(DIRECT.SHEET_NAME, 1);
  if (sheet.getLastRow() === 0) {
    sheet.getRange(1, 1, 1, DIRECT_HEADERS.length).setValues([DIRECT_HEADERS])
      .setFontWeight('bold').setBackground('#e2efda');
    sheet.setFrozenRows(1);
    const max = sheet.getMaxRows();
    sheet.getRange(1, DCOL['发票日期'], max, 1).setNumberFormat('yyyy-mm-dd');
    sheet.getRange(1, DCOL['发票号'], max, 1).setNumberFormat('@');
    sheet.getRange(1, DCOL['单价'], max, 4).setNumberFormat('£#,##0.00');
    sheet.getRange(1, DCOL['同步时间'], max, 1).setNumberFormat('yyyy-mm-dd hh:mm');
    sheet.setColumnWidth(DCOL['商品描述'], 380);
    sheet.setColumnWidth(DCOL['行号'], 50);
    sheet.hideColumns(DCOL['文件ID']);
  }
  return sheet;
}

function indexDirectRows_(sheet) {
  const seen = {};
  const n = sheet.getLastRow() - 1;
  if (n >= 1) {
    sheet.getRange(2, DCOL['文件ID'], n, 1).getValues().forEach(function (r) {
      if (r[0]) seen[r[0]] = true;
    });
  }
  return seen;
}

function getDirectFolder_() {
  const it = DriveApp.getFoldersByName(DIRECT.FOLDER);
  return it.hasNext() ? it.next() : DriveApp.createFolder(DIRECT.FOLDER);
}

// ---------------------------------------------------------------------------
// Pure parsers (tested in test.js)
// ---------------------------------------------------------------------------

const AMOUNT_RE = /(?:£|GBP)?\s?(\d{1,3}(?:,\d{3})+\.\d{2}|\d+\.\d{2})(?!\d)/g;

function amountsIn_(line) {
  const out = [];
  let m;
  AMOUNT_RE.lastIndex = 0;
  while ((m = AMOUNT_RE.exec(line)) !== null) out.push(parseFloat(m[1].replace(/,/g, '')));
  return out;
}

/** Amount on the line matching `labelRe` (last on that line, else first on the next). */
function labelledAmount_(lines, labelRe, skipRe) {
  let found = '';
  lines.forEach(function (line, i) {
    if (!labelRe.test(line) || (skipRe && skipRe.test(line))) return;
    const own = amountsIn_(line);
    const next = i + 1 < lines.length ? amountsIn_(lines[i + 1]) : [];
    const v = own.length ? own[own.length - 1] : (next.length ? next[0] : '');
    if (v !== '') found = v;  // keep the last one: totals sit at the bottom
  });
  return found;
}

/**
 * Best-effort fields from OCR'd invoice text.
 * Anything not found is left '' for the user to fill in.
 */
function parseInvoiceText(text) {
  const lines = String(text || '').split(/\r?\n/)
    .map(function (l) { return l.replace(/^[\s>]+/, '').replace(/\s+/g, ' ').trim(); })
    .filter(function (l) { return l; });
  const all = lines.join('\n');

  const supplierLine = lines.find(function (l) {
    return /\b(ltd|limited|llp|plc)\b\.?/i.test(l) && !DIRECT.OWN_COMPANY.test(l) &&
      l.length <= 60 && !/registered|company\s*(no|number)|vat/i.test(l);
  });

  const invNo = all.match(
    /invoice\s*(?:no\.?|number|num|#)\s*[:#.]?\s*([A-Z0-9][A-Z0-9\-\/]{2,})/i) ||
    all.match(/\b(?:inv|invoice)\s*[:#]\s*([A-Z0-9][A-Z0-9\-\/]{2,})/i) ||
    all.match(/\b(INV[-\s]?\d{3,})\b/i);

  const DATE = '(\\d{1,2}[\\/.\\-]\\d{1,2}[\\/.\\-]\\d{2,4}|\\d{1,2}(?:st|nd|rd|th)?\\s+[A-Za-z]{3,9},?\\s+\\d{4}|[A-Za-z]{3,9}\\s+\\d{1,2},?\\s+\\d{4}|\\d{4}-\\d{2}-\\d{2})';
  const date = all.match(new RegExp('(?:invoice|tax\\s*point)?\\s*date\\s*[:\\-]?\\s*\\n?\\s*' + DATE, 'i')) ||
    all.match(new RegExp(DATE));

  const vatNo = all.match(/\bVAT\s*(?:reg(?:istration)?\.?\s*)?(?:no\.?|number|#)?\s*[:.]?\s*(GB\s?\d{3}\s?\d{4}\s?\d{2}|\d{3}\s?\d{4}\s?\d{2})\b/i);

  const net = labelledAmount_(lines, /\b(sub\s*-?\s*total|net(\s*total|\s*amount)?|total\s*(ex|excl)\.?\s*vat)\b/i);
  const vat = labelledAmount_(lines, /\b(vat|tax)\b/i, /(reg|registration|number|no\.?\s*[:.]?\s*(GB)?\d|\binc|ex\.?\s*vat|excl|sub\s*-?\s*total)/i);
  const total = labelledAmount_(lines,
    /\b(total|amount\s*due|balance\s*due|grand\s*total)\b/i, /\bsub\s*-?\s*total\b|ex\.?\s*vat|excl|total\s*vat\b/i);

  return {
    supplier: supplierLine || '',
    invoiceNo: invNo ? invNo[1] : '',
    date: date ? date[1] : '',
    vatNo: vatNo ? vatNo[1].replace(/\s/g, '') : '',
    net: net,
    vat: vat,
    total: total,
  };
}

const MONEY = '£?\\s?(\\d{1,3}(?:,\\d{3})+\\.\\d{2}|\\d+\\.\\d{2})';
const ROW_RE = new RegExp('^(.*?[A-Za-z].*?)\\s+(\\d{1,5})\\s+' + MONEY + '\\s+' + MONEY + '\\s*$');
// "20 Samsung 32GB ... £40.00 £800.00" (quantity column first).
const ROW_QTY_FIRST_RE = new RegExp('^(\\d{1,5})\\s+(.*?[A-Za-z].*?)\\s+' + MONEY + '\\s+' + MONEY + '\\s*$');
const MONEY_ONLY_RE = new RegExp('^' + MONEY + '$');
const num_ = function (s) { return parseFloat(String(s).replace(/,/g, '')); };
const near_ = function (a, b) { return Math.abs(a - b) < 0.015; };

/**
 * Invoice lines as {desc, qty, price, amount}. Handles both layouts Drive
 * OCR produces: one line per row ("desc 4 £54.00 £216.00", or tab-separated
 * table cells), and column-by-column (all descriptions, then all quantities,
 * then prices, then amounts). Only the part between the DESCRIPTION header
 * and SUB-TOTAL/TOTAL is read.
 */
function parseLineItems(text) {
  let lines = String(text || '').split(/\r?\n/)
    .map(function (l) { return l.replace(/\s+/g, ' ').trim(); })
    .filter(function (l) { return l; });
  // Header is "DESCRIPTION QTY PRICE AMOUNT" on one line, or (column layout)
  // "DESCRIPTION" on its own line followed by the other column titles.
  const head = lines.findIndex(function (l) {
    return /^(item\s+)?description\b/i.test(l) ||
      /\bdescription\b/i.test(l) && /\b(qty|quantity)\b/i.test(l);
  });
  // Without a header, don't trim at the first "VAT ..." line: that can be the
  // seller's VAT number above the items. ROW_RE alone is strict enough then.
  if (head >= 0) {
    lines = lines.slice(head + 1);
    const end = lines.findIndex(function (l) { return /^(sub\s*-?\s*total|total|net\b|vat\b)/i.test(l); });
    if (end >= 0) lines = lines.slice(0, end);
  }

  // Layout 1: whole row on one line.
  const rows = [];
  lines.forEach(function (l) {
    const m = l.match(ROW_RE);
    const q = !m && l.match(ROW_QTY_FIRST_RE);
    if (m) rows.push({ desc: m[1].trim(), qty: +m[2], price: num_(m[3]), amount: num_(m[4]) });
    else if (q) rows.push({ desc: q[2].trim(), qty: +q[1], price: num_(q[3]), amount: num_(q[4]) });
  });
  if (rows.length) return rows;

  // Layout 2: columns. Descriptions may carry the qty at their end.
  const descs = [], qtys = [], money = [];
  lines.forEach(function (l) {
    if (/^\d{1,5}$/.test(l)) qtys.push(+l);
    else if (MONEY_ONLY_RE.test(l)) money.push(num_(l.match(MONEY_ONLY_RE)[1]));
    else if (/[A-Za-z]/.test(l) && !/^(qty|quantity|price|unit price|amount|rate|total)$/i.test(l)) descs.push(l);
  });
  const n = descs.length;
  if (!n || money.length !== 2 * n) return [];
  if (qtys.length !== n) {
    // "8gb ... KVR24R17S4/8 1Rx4 12" -> qty 12 sits at the end of the text.
    const tail = descs.map(function (d) { return d.match(/^(.*\S)\s+(\d{1,5})$/); });
    if (tail.some(function (t) { return !t; })) return [];
    qtys.length = 0;
    tail.forEach(function (t, i) { descs[i] = t[1]; qtys.push(+t[2]); });
  }
  // Prices then amounts as two blocks, or interleaved price/amount pairs:
  // pick whichever makes qty x price = amount.
  const blocks = descs.map(function (d, i) { return { p: money[i], a: money[n + i] }; });
  const pairs = descs.map(function (d, i) { return { p: money[2 * i], a: money[2 * i + 1] }; });
  const fits = function (pa) { return pa.every(function (x, i) { return near_(qtys[i] * x.p, x.a); }); };
  const use = fits(pairs) && !fits(blocks) ? pairs : blocks;
  return descs.map(function (d, i) { return { desc: d, qty: qtys[i], price: use[i].p, amount: use[i].a }; });
}

/** Whether the lines add up to the invoice's own subtotal (or total). */
function checkTotals(items, inv) {
  if (!items.length) return '⚠ 未识别到明细';
  const sum = items.reduce(function (s, it) { return s + it.amount; }, 0);
  const ref = inv.net !== '' ? inv.net : inv.total;
  if (ref === '') return '⚠ 未找到发票小计，请核对';
  if (near_(sum, ref)) return '✓ 明细合计=发票小计';
  return '⚠ 明细合计£' + sum.toFixed(2) + ' ≠ 小计£' + Number(ref).toFixed(2);
}

/** Part number for lines parseSpec misses: last code-like token with letters and digits. */
function partFromDesc(desc) {
  const toks = String(desc).split(/\s+/).filter(function (t) {
    return t.length >= 6 && /[A-Z]/i.test(t) && /\d/.test(t) &&
      !/^PC\d|^DDR\d|^\d+(gb|tb|mhz)$/i.test(t) && !/^\dRx\d$/i.test(t);
  });
  return toks.length ? toks[toks.length - 1].toUpperCase() : '';
}

const MONTHS = { jan: 0, feb: 1, mar: 2, apr: 3, may: 4, jun: 5, jul: 6, aug: 7, sep: 8, oct: 9, nov: 10, dec: 11 };

/** "September 22, 2026" / "21/10/2026" (UK) / "3 Oct 2026" / "2026-10-03" -> Date; else the input. */
function parseInvoiceDate(s) {
  const t = String(s || '').trim();
  let m = t.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (m) return new Date(+m[1], +m[2] - 1, +m[3]);
  m = t.match(/^(\d{1,2})[\/.\-](\d{1,2})[\/.\-](\d{2,4})$/);
  if (m) return new Date(m[3].length === 2 ? 2000 + +m[3] : +m[3], +m[2] - 1, +m[1]);
  m = t.match(/^(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3})[A-Za-z]*,?\s+(\d{4})$/);
  if (m && MONTHS[m[2].toLowerCase()] !== undefined) return new Date(+m[3], MONTHS[m[2].toLowerCase()], +m[1]);
  m = t.match(/^([A-Za-z]{3})[A-Za-z]*\s+(\d{1,2}),?\s+(\d{4})$/);
  if (m && MONTHS[m[1].toLowerCase()] !== undefined) return new Date(+m[3], MONTHS[m[1].toLowerCase()], +m[2]);
  return t;
}

if (typeof module !== 'undefined') {
  Object.assign(module.exports, { parseInvoiceText: parseInvoiceText, parseLineItems: parseLineItems,
    checkTotals: checkTotals, partFromDesc: partFromDesc, parseInvoiceDate: parseInvoiceDate });
}
