/**
 * Direct-supplier invoices -> "直采记录" tab.
 *
 * Sources:
 *   1. Gmail messages labelled 「直采发票」: PDF attachments are saved
 *      into the Drive folder below, then OCR'd. Messages with no attachment
 *      are parsed from the email body instead.
 *   2. Drive folder 「直采发票」: any PDF/image dropped in by hand (e.g.
 *      invoices received on WhatsApp) is OCR'd.
 *
 * OCR uses Drive's free built-in PDF/image -> Google Doc conversion; fields
 * are then pulled out with regex rules, so every row starts as 「待核对」 for
 * a human to check. Runs from dailySync() in Code.gs (same 08:00 trigger).
 */

const DIRECT = {
  SHEET_NAME: '直采记录',
  LABEL: '直采发票',
  FOLDER: '直采发票',
  GMAIL_WINDOW: 'newer_than:60d',
  OCR_LANGUAGE: 'en',
  OWN_COMPANY: /blue\s*bridgewell/i,
};

const DIRECT_HEADERS = [
  '同步时间', '发票日期', '供应商', '发票号', '商品明细(识别)', '品牌', '单条容量',
  '频率', '料号', '数量(手填)', '净额', 'VAT', '总额', '卖家VAT号', '来源',
  '发票文件', '核对状态', '备注(手填)', '文件ID', '邮件ID',
];
const DCOL = {};
DIRECT_HEADERS.forEach(function (h, i) { DCOL[h] = i + 1; });

const OCR_MIME = ['application/pdf', 'image/jpeg', 'image/png', 'image/gif'];

// ---------------------------------------------------------------------------
// Entry points
// ---------------------------------------------------------------------------

/** Run once by hand: creates the label, Drive folder and tab, then syncs. */
function setupDirect() {
  if (!GmailApp.getUserLabelByName(DIRECT.LABEL)) GmailApp.createLabel(DIRECT.LABEL);
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
  const folder = getDirectFolder_();
  const seen = indexDirectRows_(sheet);
  const rows = [];

  // 1. Gmail label. Attachments go into the folder first so every invoice
  //    file ends up in one place.
  // Gmail labels whole conversations, so replies (ours and theirs) come back
  // too: skip our own messages, and only keep attachment-less messages whose
  // body actually reads like an invoice.
  const me = Session.getEffectiveUser().getEmail().toLowerCase();
  const query = 'label:' + DIRECT.LABEL + ' ' + DIRECT.GMAIL_WINDOW;
  searchMessages_(query).forEach(function (msg) {
    if (seen.msg[msg.getId()]) return;
    if (msg.getFrom().toLowerCase().indexOf(me) >= 0) return;
    seen.msg[msg.getId()] = true;
    const sender = senderName(msg.getFrom());
    // Email invoices are PDFs; photos in long supplier threads are usually
    // stock pictures, not invoices, so only PDFs are taken from Gmail.
    const files = msg.getAttachments({ includeInlineImages: false }).filter(function (a) {
      return a.getContentType() === 'application/pdf';
    });
    if (!files.length) {
      const body = msg.getPlainBody();
      const inv = parseInvoiceText(body);
      if (!/invoice/i.test(msg.getSubject() + ' ' + body) || inv.total === '') return;
      rows.push(buildDirectRow_(inv, sender || msg.getSubject(), '邮件正文', '', '', msg.getId()));
      return;
    }
    files.forEach(function (att) {
      const file = folder.createFile(att.copyBlob());
      file.setDescription('来自邮件: ' + msg.getSubject());
      seen.file[file.getId()] = true;
      const inv = parseInvoiceText(ocrFile_(file.getId()));
      rows.push(buildDirectRow_(inv, sender, '邮件附件', file.getUrl(), file.getId(), msg.getId()));
    });
  });

  // 2. Files dropped into the Drive folder by hand.
  const it = folder.getFiles();
  while (it.hasNext()) {
    const file = it.next();
    if (seen.file[file.getId()] || OCR_MIME.indexOf(file.getMimeType()) < 0) continue;
    seen.file[file.getId()] = true;
    const inv = parseInvoiceText(ocrFile_(file.getId()));
    const fallback = file.getName().replace(/\.[^.]+$/, '');
    rows.push(buildDirectRow_(inv, fallback, 'Drive上传', file.getUrl(), file.getId(), ''));
  }

  if (rows.length) {
    sheet.getRange(sheet.getLastRow() + 1, 1, rows.length, DIRECT_HEADERS.length).setValues(rows);
    sheet.getRange(2, 1, sheet.getLastRow() - 1, DIRECT_HEADERS.length)
      .sort({ column: DCOL['同步时间'], ascending: false });
  }
  return rows.length;
}

function buildDirectRow_(inv, fallbackSupplier, source, fileUrl, fileId, msgId) {
  const spec = parseSpec(inv.items.join(' / '));
  return [
    new Date(),
    inv.date,
    inv.supplier || fallbackSupplier,
    inv.invoiceNo,
    inv.items.join('\n'),
    spec.brand,
    spec.capacity,
    spec.speed,
    spec.partNo,
    '',
    inv.net,
    inv.vat,
    inv.total,
    inv.vatNo,
    source,
    fileUrl,
    '待核对',
    '',
    fileId,
    msgId,
  ];
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
    return DocumentApp.openById(docId).getBody().getText();
  } finally {
    DriveApp.getFileById(docId).setTrashed(true);
  }
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
    sheet.getRange(1, DCOL['同步时间'], max, 1).setNumberFormat('yyyy-mm-dd hh:mm');
    sheet.getRange(1, DCOL['发票日期'], max, 1).setNumberFormat('@');
    sheet.getRange(1, DCOL['发票号'], max, 1).setNumberFormat('@');
    sheet.getRange(1, DCOL['净额'], max, 3).setNumberFormat('£#,##0.00');
    sheet.setColumnWidth(DCOL['商品明细(识别)'], 380);
    sheet.hideColumns(DCOL['文件ID'], 2);
    const rule = SpreadsheetApp.newDataValidation()
      .requireValueInList(['待核对', '已核对', '重复/忽略'], true).setAllowInvalid(true).build();
    sheet.getRange(2, DCOL['核对状态'], max - 1, 1).setDataValidation(rule);
  }
  return sheet;
}

function indexDirectRows_(sheet) {
  const seen = { file: {}, msg: {} };
  const n = sheet.getLastRow() - 1;
  if (n >= 1) {
    sheet.getRange(2, DCOL['文件ID'], n, 2).getValues().forEach(function (r) {
      if (r[0]) seen.file[r[0]] = true;
      if (r[1]) seen.msg[r[1]] = true;
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

/** "Dominic <x@y.com>" -> "Dominic"; bare address -> its domain name. */
function senderName(from) {
  const m = String(from || '').match(/^\s*"?([^"<]+?)"?\s*<([^>]+)>/);
  if (m) return m[1].trim();
  const d = String(from || '').match(/@([^.>]+)/);
  return d ? d[1] : '';
}

/**
 * Best-effort fields from invoice text (OCR output or an email body).
 * Anything not found is left '' for the user to fill in.
 */
function parseInvoiceText(text) {
  const lines = String(text || '').split(/\r?\n/)
    .map(function (l) { return l.replace(/\s+/g, ' ').trim(); })
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

  // Product lines: anything mentioning a memory/storage size, else lines
  // carrying a quantity and a price. Capped so one bad OCR can't flood a cell.
  let items = lines.filter(function (l) { return /\b\d+\s?(GB|TB)\b/i.test(l); });
  if (!items.length) {
    items = lines.filter(function (l) {
      return amountsIn_(l).length && /^\d{1,4}\s+[A-Za-z]/.test(l) &&
        !/total|vat|due|balance/i.test(l);
    });
  }

  return {
    supplier: supplierLine || '',
    invoiceNo: invNo ? invNo[1] : '',
    date: date ? date[1] : '',
    vatNo: vatNo ? vatNo[1].replace(/\s/g, '') : '',
    net: net,
    vat: vat,
    total: total,
    items: items.slice(0, 8),
  };
}

if (typeof module !== 'undefined') {
  Object.assign(module.exports, { parseInvoiceText: parseInvoiceText, senderName: senderName });
}
