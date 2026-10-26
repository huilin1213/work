/**
 * Direct-supplier invoices -> one 「直采汇总」 tab, one row per invoice,
 * newest first.
 *
 * Sources:
 *   - PDF attachments emailed by the suppliers listed in 「直采供应商」
 *     (saved automatically to Drive 直采发票/<supplier>/; emailed PDFs that
 *     never say "invoice" - quotes, price lists - are binned);
 *   - any PDF/image put under Drive 直采发票/ by hand (e.g. from WhatsApp).
 *     The subfolder name is the supplier.
 *
 * Each file is OCR'd once (Drive's free PDF/image -> Google Doc conversion)
 * and parsed with regex rules for date, invoice number, net/gross totals
 * and the line items (only used to count units and to check the lines add
 * up to the subtotal). 出口日期/出口单号 are typed by hand; rows are keyed by
 * file ID and never rewritten, so hand-typed cells survive every sync.
 * Runs from dailySync() in Code.gs and from the 采购工具 menu.
 */

const DIRECT = {
  SHEET_NAME: '直采汇总',
  SUPPLIER_SHEET: '直采供应商',
  FOLDER: '直采发票',
  OCR_LANGUAGE: 'en',
  OWN_COMPANY: /blue\s*bridgewell/i,
  // Apps Script stops a run at 6 min; files not reached are read next run.
  MAX_RUN_MS: 4.5 * 60 * 1000,
};

const DIRECT_HEADERS = [
  '发票日期', '供应商', '发票号', '商品数量', '不含VAT', '含VAT', '发票链接',
  '出口日期(手填)', '出口单号(手填)', '核对', '文件ID',
];
const DCOL = {};
DIRECT_HEADERS.forEach(function (h, i) { DCOL[h] = i + 1; });

const SUPPLIER_HEADERS = ['供应商', '邮箱（多个用逗号隔开）', '备注'];
// Added to 「直采供应商」 once each (tracked by name in Script Properties),
// so a supplier the user later deletes is not re-added.
const SUPPLIER_SEED = [
  ['Steve', 'bozzy270681@gmail.com, bozzy_s@hotmail.com', ''],
  ['Chaps IT', 'dom.chappell@chapsitrecycling.co.uk', ''],
  ['SecureIT Recycling', 'reece@secure-itad.com', ''],
  ['Richard Pacific', '', '没有邮箱：发票请手动存进 Drive 的 直采发票/Richard Pacific 文件夹'],
  ['Jon (Alekus Repairs)', 'alekusrepairs@gmail.com', ''],
  ['Asif (Eruptive Brands)', 'asif@eruptivebrands.com', ''],
  ['IT Recycling', 'carl@itrecycling.uk.com', ''],
];

const OCR_MIME = ['application/pdf', 'image/jpeg', 'image/png', 'image/gif'];
const SKIPPED_KEY = 'direct.skippedAttachments';
const SEEDED_KEY = 'direct.seededSuppliers';

// ---------------------------------------------------------------------------
// Entry points
// ---------------------------------------------------------------------------

/** Adds the 采购工具 menu to the spreadsheet so nothing needs the editor. */
function onOpen() {
  SpreadsheetApp.getUi().createMenu('采购工具')
    .addItem('立即同步（eBay + 直采）', 'dailySync')
    .addItem('只同步直采发票', 'syncDirectNow')
    .addSeparator()
    .addItem('重新识别全部直采发票', 'rebuildDirect')
    .addToUi();
}

/** Run once by hand: creates folder and tabs, then syncs. */
function setupDirect() {
  // The per-line 直采记录 tabs of earlier versions are replaced by 直采汇总.
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  ss.getSheets().forEach(function (sh) {
    if (/^直采记录/.test(sh.getName())) ss.deleteSheet(sh);
  });
  const folder = getDirectFolder_();
  getSupplierSheet_();
  getDirectSheet_();
  const added = syncDirect_();
  Logger.log('Direct setup done. %s invoices added. Drive folder: %s', added, folder.getUrl());
}

function syncDirectNow() {
  Logger.log('Direct sync: %s new invoices.', syncDirect_());
}

/**
 * After the recognition rules improve: keeps the current 直采汇总 as a
 * renamed backup (hand-typed 出口 columns intact) and reads every invoice
 * in Drive again.
 */
function rebuildDirect() {
  backupSheet_(getDirectSheet_());
  syncDirectNow();
}

// ---------------------------------------------------------------------------
// Sync
// ---------------------------------------------------------------------------

function syncDirect_() {
  const started = Date.now();
  const root = getDirectFolder_();
  const suppliers = readSuppliers_();
  const skipped = loadSkipped_();
  importFromEmail_(root, suppliers, skipped);

  const sheet = getDirectSheet_();
  const seen = indexDirectRows_(sheet);
  const canonical = {};
  suppliers.forEach(function (s) { canonical[s.name.toLowerCase()] = s.name; });

  const rows = [];
  const files = listInvoiceFiles_(root, '');
  for (let i = 0; i < files.length; i++) {
    const f = files[i];
    if (seen[f.file.getId()]) continue;
    if (Date.now() - started > DIRECT.MAX_RUN_MS) {
      Logger.log('Time limit: remaining invoices will be read on the next run.');
      break;
    }
    seen[f.file.getId()] = true;
    const text = ocrFile_(f.file.getId());
    const tag = f.file.getDescription() || '';
    // An emailed PDF that never says "invoice" is a quote, price list, etc.
    if (tag.indexOf('gmail:') === 0 && !/invoice/i.test(text)) {
      skipped[tag] = true;
      f.file.setTrashed(true);
      continue;
    }
    const supplier = canonical[String(f.supplier).toLowerCase()] || f.supplier;
    rows.push(buildInvoiceRow_(text, f.file, supplier));
  }
  saveSkipped_(skipped);

  if (rows.length) {
    sheet.getRange(sheet.getLastRow() + 1, 1, rows.length, DIRECT_HEADERS.length).setValues(rows);
  }
  if (sheet.getLastRow() > 2) {
    sheet.getRange(2, 1, sheet.getLastRow() - 1, DIRECT_HEADERS.length)
      .sort({ column: DCOL['发票日期'], ascending: false });
  }
  return rows.length;
}

/**
 * Saves new PDF attachments from each supplier's addresses into
 * 直采发票/<supplier>/. An attachment is skipped when it was saved before
 * (tag in file description), binned before as a non-invoice, or already
 * sits anywhere under 直采发票 under its own name with the same size.
 */
function importFromEmail_(root, suppliers, skipped) {
  const tags = {}, sizes = {};
  listInvoiceFiles_(root, '').forEach(function (f) {
    tags[f.file.getDescription()] = true;
    sizes[f.file.getName() + '|' + f.file.getSize()] = true;
  });
  const sameFile = function (name, size) {
    return Object.keys(sizes).some(function (k) {
      const cut = k.lastIndexOf('|');
      return +k.slice(cut + 1) === size && k.slice(0, cut).slice(-name.length) === name;
    });
  };

  suppliers.forEach(function (s) {
    if (!s.emails.length) return;
    let folder = null;
    let count = 0;
    const query = 'from:(' + s.emails.join(' OR ') + ') has:attachment filename:pdf';
    searchMessages_(query).forEach(function (msg) {
      // Threads also hold our own replies, which quote their attachments.
      const from = msg.getFrom().toLowerCase();
      if (!s.emails.some(function (e) { return from.indexOf(e) >= 0; })) return;
      msg.getAttachments({ includeInlineImages: false }).forEach(function (att, i) {
        // Some mailers send PDFs as application/octet-stream.
        if (att.getContentType() !== 'application/pdf' && !/\.pdf$/i.test(att.getName())) return;
        const tag = 'gmail:' + msg.getId() + ':' + i;
        if (tags[tag] || skipped[tag]) return;
        const size = att.getBytes().length;
        if (sameFile(att.getName(), size)) return;
        folder = folder || findOrCreateSubfolder_(root, s.name);
        const date = Utilities.formatDate(msg.getDate(), CONFIG.TIMEZONE, 'yyyy-MM-dd');
        const name = date + ' ' + att.getName();
        folder.createFile(att.copyBlob()).setName(name).setDescription(tag);
        tags[tag] = true;
        sizes[name + '|' + size] = true;  // same PDF re-sent in a later email
        count++;
      });
    });
    if (count) Logger.log('%s: saved %s new PDF(s) from email', s.name, count);
  });
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

/** One 直采汇总 row for an invoice file. */
function buildInvoiceRow_(text, file, folderSupplier) {
  const inv = parseInvoiceText(text);
  const items = parseLineItems(text);
  const name = file.getName();
  // Supplier subfolder name beats OCR: it is what the user chose to call them.
  const supplier = folderSupplier || inv.supplier || name.replace(/\.[^.]+$/, '');
  let gross = inv.total, net = inv.net;
  if (net === '' && gross !== '' && inv.vat !== '') net = Math.round((gross - inv.vat) * 100) / 100;
  if (gross === '' && net !== '' && inv.vat !== '') gross = Math.round((net + inv.vat) * 100) / 100;
  return [
    parseInvoiceDate(inv.date), supplier, inv.invoiceNo, countUnits(items), net, gross,
    file.getUrl(), '', '', checkTotals(items, inv), file.getId(),
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

/** Renames a tab to "<name>(旧版 <time>)" so a fresh one can be built. */
function backupSheet_(sheet) {
  sheet.setName(sheet.getName() + '(旧版 ' +
    Utilities.formatDate(new Date(), CONFIG.TIMEZONE, 'yyyy-MM-dd HH:mm') + ')');
}

function getDirectSheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(DIRECT.SHEET_NAME);
  // A tab with another layout (including an earlier hand-made one) is kept
  // under a new name, and a fresh one is rebuilt from the Drive files.
  if (sheet && sheet.getLastRow() > 0) {
    const head = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
    if (head.join('|') !== DIRECT_HEADERS.join('|')) {
      backupSheet_(sheet);
      sheet = null;
    }
  }
  if (!sheet) sheet = ss.insertSheet(DIRECT.SHEET_NAME, 1);
  if (sheet.getLastRow() === 0) {
    sheet.getRange(1, 1, 1, DIRECT_HEADERS.length).setValues([DIRECT_HEADERS])
      .setFontWeight('bold').setBackground('#e2efda');
    sheet.setFrozenRows(1);
    const max = sheet.getMaxRows();
    sheet.getRange(1, DCOL['发票日期'], max, 1).setNumberFormat('yyyy-mm-dd');
    sheet.getRange(1, DCOL['发票号'], max, 1).setNumberFormat('@');
    sheet.getRange(1, DCOL['不含VAT'], max, 2).setNumberFormat('£#,##0.00');
    sheet.getRange(1, DCOL['出口日期(手填)'], max, 1).setNumberFormat('yyyy-mm-dd');
    sheet.getRange(1, DCOL['出口单号(手填)'], max, 1).setNumberFormat('@');
    sheet.setColumnWidth(DCOL['供应商'], 180);
    sheet.setColumnWidth(DCOL['发票链接'], 90);
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

/** Subfolder of `root` named `name`, matched case-insensitively ("steve" = "Steve"). */
function findOrCreateSubfolder_(root, name) {
  const it = root.getFolders();
  while (it.hasNext()) {
    const f = it.next();
    if (f.getName().toLowerCase() === name.toLowerCase()) return f;
  }
  return root.createFolder(name);
}

function getSupplierSheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(DIRECT.SUPPLIER_SHEET);
  if (!sheet) {
    sheet = ss.insertSheet(DIRECT.SUPPLIER_SHEET);
    const rows = [SUPPLIER_HEADERS].concat(SUPPLIER_SEED);
    sheet.getRange(1, 1, rows.length, SUPPLIER_HEADERS.length).setValues(rows);
    sheet.getRange(1, 1, 1, SUPPLIER_HEADERS.length).setFontWeight('bold').setBackground('#e2efda');
    sheet.setFrozenRows(1);
    sheet.setColumnWidth(1, 180);
    sheet.setColumnWidth(2, 380);
    sheet.setColumnWidth(3, 420);
    PropertiesService.getScriptProperties().setProperty(SEEDED_KEY,
      JSON.stringify(SUPPLIER_SEED.map(function (r) { return r[0]; })));
  }
  seedNewSuppliers_(sheet);
  return sheet;
}

/** Appends SUPPLIER_SEED entries added to the code since the tab was made. */
function seedNewSuppliers_(sheet) {
  const props = PropertiesService.getScriptProperties();
  const done = JSON.parse(props.getProperty(SEEDED_KEY) || '[]');
  const have = {};
  if (sheet.getLastRow() > 1) {
    sheet.getRange(2, 1, sheet.getLastRow() - 1, 1).getValues().forEach(function (r) {
      have[String(r[0]).trim().toLowerCase()] = true;
    });
  }
  // Tabs made before seeding was tracked: treat what is there as done.
  if (!props.getProperty(SEEDED_KEY)) Object.keys(have).forEach(function (k) { done.push(k); });
  const fresh = SUPPLIER_SEED.filter(function (r) {
    return done.indexOf(r[0]) < 0 && done.indexOf(r[0].toLowerCase()) < 0 && !have[r[0].toLowerCase()];
  });
  if (fresh.length) {
    sheet.getRange(sheet.getLastRow() + 1, 1, fresh.length, SUPPLIER_HEADERS.length).setValues(fresh);
  }
  props.setProperty(SEEDED_KEY, JSON.stringify(done.concat(SUPPLIER_SEED.map(function (r) { return r[0]; }))));
}

/** [{name, emails}] from 「直采供应商」; blank names are ignored. */
function readSuppliers_() {
  const sheet = getSupplierSheet_();
  const n = sheet.getLastRow() - 1;
  if (n < 1) return [];
  return sheet.getRange(2, 1, n, 2).getValues()
    .filter(function (r) { return String(r[0]).trim(); })
    .map(function (r) {
      return {
        name: String(r[0]).trim(),
        emails: String(r[1]).toLowerCase().split(/[\s,;，；]+/)
          .filter(function (e) { return e.indexOf('@') > 0; }),
      };
    });
}

/** Gmail attachment tags already binned as non-invoices, so they aren't re-imported. */
function loadSkipped_() {
  const raw = PropertiesService.getScriptProperties().getProperty(SKIPPED_KEY);
  return raw ? JSON.parse(raw) : {};
}

function saveSkipped_(skipped) {
  PropertiesService.getScriptProperties().setProperty(SKIPPED_KEY, JSON.stringify(skipped));
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

/** First amount after the label on its line (else first on the next line). */
function labelledAmount_(lines, labelRe, skipRe) {
  let found = '';
  lines.forEach(function (line, i) {
    if (!labelRe.test(line) || (skipRe && skipRe.test(line))) return;
    // After the label, not anywhere on the line: OCR can join two labels on
    // one line ("Total VAT 20% 439.33 Invoice Total GBP 2,636.00").
    const own = amountsIn_(line.slice(line.search(labelRe)));
    const next = i + 1 < lines.length ? amountsIn_(lines[i + 1]) : [];
    const v = own.length ? own[0] : (next.length ? next[0] : '');
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

  // The number must contain a digit: when OCR puts labels on one line and
  // values on the next ("Invoice Date Invoice Number"), "Num" + "ber" would
  // otherwise read as an invoice number.
  const CODE = '([A-Z0-9][A-Z0-9\\-\\/]{2,})(?<=\\d[A-Z0-9\\-\\/]*)';
  const invNo = all.match(new RegExp('invoice\\s*(?:no\\.?|number|#)\\s*[:#.]?\\s*' + CODE, 'i')) ||
    all.match(new RegExp('\\b(?:inv|invoice)\\s*[:#]\\s*' + CODE, 'i')) ||
    all.match(/\b(INV[-\s]?\d{3,})\b/i) ||
    all.match(/\b([A-Z]{2,4}-\d{4,})\b/);

  const DATE = '(\\d{1,2}[\\/.\\-]\\d{1,2}[\\/.\\-]\\d{2,4}|\\d{1,2}(?:st|nd|rd|th)?\\s+[A-Za-z]{3,9},?\\s+\\d{4}|[A-Za-z]{3,9}\\s+\\d{1,2},?\\s+\\d{4}|\\d{4}-\\d{2}-\\d{2})';
  // Invoice/tax-point date first; never the due date.
  const date = all.match(new RegExp('(?:invoice|tax\\s*point)\\s*date\\s*[:\\-]?\\s*' + DATE, 'i')) ||
    // "Invoice Date Invoice Number" / "10 Sep 2026 IV-582488"
    all.match(new RegExp('invoice\\s*date[^\\n]*\\n\\s*' + DATE, 'i')) ||
    all.match(new RegExp('(?:^|[^a-z])(?<!due\\s)date\\s*[:\\-]?\\s*' + DATE, 'im')) ||
    all.match(new RegExp('^(?!.*\\bdue\\b).*?' + DATE, 'im'));

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
// Unit prices may carry 4 decimals ("41.6667"), quantities ".00", and a VAT
// rate column ("20%") may sit between unit price and amount.
const QTY = '(\\d{1,5}(?:\\.\\d{1,2})?)';
const PRICE = '£?\\s?(\\d{1,3}(?:,\\d{3})+\\.\\d{2,4}|\\d+\\.\\d{2,4})';
const VAT_RATE = '(?:\\s+\\d{1,2}(?:\\.\\d+)?\\s?%)?';
const ROW_RE = new RegExp('^(.*?[A-Za-z].*?)\\s+' + QTY + '\\s+' + PRICE + VAT_RATE + '\\s+' + MONEY + '\\s*$');
// "20 Samsung 32GB ... £40.00 £800.00" (quantity column first).
const ROW_QTY_FIRST_RE = new RegExp('^' + QTY + '\\s+(.*?[A-Za-z].*?)\\s+' + PRICE + VAT_RATE + '\\s+' + MONEY + '\\s*$');
// A column title on its own line ("Unit Price", "VAT", "Amount GBP").
const HEADER_WORDS_RE = /^((qty|quantity|unit|price|rate|vat|tax|amount|total|net|gross|gbp|£|ex|inc|excl|incl|%|\(|\))\s*)+$/i;
const PRICE_ONLY_RE = new RegExp('^' + PRICE + '$');
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
    // A totals line carries an amount; a bare "VAT" is just a column title.
    const end = lines.findIndex(function (l) {
      return /^(sub\s*-?\s*total|total|net\b|vat\b)/i.test(l) && /\d/.test(l);
    });
    if (end >= 0) lines = lines.slice(0, end);
  }

  // Layout 1: whole row on one line.
  const rows = [];
  lines.forEach(function (l) {
    const m = l.match(ROW_RE);
    const q = !m && l.match(ROW_QTY_FIRST_RE);
    if (m) rows.push({ desc: cleanDesc_(m[1]), qty: +m[2], price: num_(m[3]), amount: num_(m[4]) });
    else if (q) rows.push({ desc: cleanDesc_(q[2]), qty: +q[1], price: num_(q[3]), amount: num_(q[4]) });
  });
  if (rows.length) return rows;

  // Layout 2: columns. Descriptions may carry the qty at their end.
  const descs = [], qtys = [], money = [];
  lines.forEach(function (l) {
    if (/^\d{1,5}$/.test(l)) qtys.push(+l);
    else if (/^\d{1,2}(\.\d+)?\s?%$/.test(l)) return;
    else if (PRICE_ONLY_RE.test(l)) money.push(num_(l.match(PRICE_ONLY_RE)[1]));
    else if (/[A-Za-z]/.test(l) && !HEADER_WORDS_RE.test(l)) descs.push(l);
  });
  const n = descs.length;
  if (!n) return [];
  // Chaps-style columns: qty "16.00" looks like money, so all n quantities,
  // prices and amounts land in `money` (VAT "20%" lines are dropped above).
  if (!qtys.length && money.length === 3 * n) {
    const cand = [
      descs.map(function (d, i) { return [money[i], money[n + i], money[2 * n + i]]; }),
      descs.map(function (d, i) { return [money[3 * i], money[3 * i + 1], money[3 * i + 2]]; }),
    ];
    const ok = cand.filter(function (c) { return c.every(function (x) { return near_(x[0] * x[1], x[2]); }); });
    const use3 = ok.length ? ok[0] : cand[0];
    return descs.map(function (d, i) {
      return { desc: cleanDesc_(d), qty: use3[i][0], price: use3[i][1], amount: use3[i][2] };
    });
  }
  if (money.length !== 2 * n) return [];
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
  return descs.map(function (d, i) { return { desc: cleanDesc_(d), qty: qtys[i], price: use[i].p, amount: use[i].a }; });
}

/** Re-joins part numbers broken at a line wrap: "HMA82GR7JJR8N- VK" -> "HMA82GR7JJR8N-VK". */
function cleanDesc_(d) {
  return String(d).replace(/([A-Z0-9])-\s+(?=[A-Z0-9])/g, '$1-').replace(/\s+/g, ' ').trim();
}

/** Units on an invoice: line quantities, not counting postage/delivery lines. */
function countUnits(items) {
  if (!items.length) return '';
  return items.reduce(function (n, it) {
    return /postage|delivery|shipping|carriage/i.test(it.desc) ? n : n + it.qty;
  }, 0);
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
    countUnits: countUnits,
    checkTotals: checkTotals, parseInvoiceDate: parseInvoiceDate });
}
