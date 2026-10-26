// Offline check of the pure parsers in Code.gs: `node test.js`
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ctx = { module: { exports: {} } };
vm.createContext(ctx);
// Apps Script loads every .gs file into one global scope; mimic that.
['Code.gs', 'Direct.gs'].forEach((f) =>
  vm.runInContext(fs.readFileSync(path.join(__dirname, f), 'utf8'), ctx, { filename: f }));
const { parseOrderEmail, parseStatusSubject, parseSpec, parseInvoiceText, parseLineItems, checkTotals, partFromDesc, parseInvoiceDate, summarize } = ctx.module.exports;

// Trimmed from a real eBay UK "Order confirmed" email (offer accepted, with
// Buyer Protection fee), with tracking params and the delivery address removed.
const html = `
<html><head><style>.x{color:red}</style></head><body>
<h1>Your offer is accepted and your order is confirmed.</h1>
<h3>Estimated delivery:</h3><p>Wed, 28 Oct - Thu, 29 Oct</p>
<table>
<tr><td><a href="https://www.ebay.co.uk/itm/178499324206?mkevt=1&amp;x=1"><h3>SK Hynix HMA84GR7CJR4N-WM 32GB 2Rx4 DDR4-2933 ECC RDIMM Memory Module (1 x 32GB)</h3></a></td></tr>
<tr><td><a href="https://www.ebay.co.uk/itm/178499324206?mkevt=1&amp;y=2"><img src="x.jpg"></a></td></tr>
<tr><td><h4>Price: &pound;205.00 incl. &pound;8.56 Buyer Protection fee<a href="https://pages.ebay.co.uk/buyerprotection/"></a></h4></td><td>&pound;205.00</td></tr>
<tr><td><h4>Item ID: 178499324206</h4></td><td>178499324206</td></tr>
<tr><td><h4>Order number: 23-15316-96581</h4></td><td>23-15316-96581</td></tr>
<tr><td><h4>Seller: jay5hah<a href="https://www.ebay.co.uk/ulk/usr/jay5hah"></a></h4></td><td>jay5hah</td></tr>
</table>
<h2>Popular in your interests</h2>
<a href="https://www.ebay.co.uk/itm/198728054472"><h3>128GB SK Hynix…</h3></a><p>&pound;54.50</p>
<h3>Order total:</h3>
<table><tr><td>Subtotal</td><td>&pound;205.00</td></tr>
<tr><td>Postage</td><td>&pound;5.79</td></tr>
<tr><td>Total charged to x -2886</td><td>&pound;210.79</td></tr></table>
</body></html>`;

const r = parseOrderEmail(html);
assert.strictEqual(r.items.length, 1);
const it = r.items[0];
assert.strictEqual(it.orderNo, '23-15316-96581');
assert.strictEqual(it.itemId, '178499324206');
assert.strictEqual(it.seller, 'jay5hah');
assert.strictEqual(it.price, 205);
assert.strictEqual(it.fee, 8.56);
assert.strictEqual(it.qty, 1);
assert.ok(it.title.startsWith('SK Hynix HMA84GR7CJR4N-WM 32GB'), it.title);
assert.strictEqual(r.postage, 5.79);
assert.strictEqual(r.total, 210.79);
assert.strictEqual(r.subtotal, 205);
assert.strictEqual(r.eta, 'Wed, 28 Oct - Thu, 29 Oct');

// Plain-text rendering of a second real email (Buy It Now, seller address
// after the username), two items in one checkout with a quantity line.
const text = `# Thanks for shopping. Your order is confirmed.
### Estimated delivery:

Wed, 28 Oct - Thu, 29 Oct
[ ### 5x SK Hynix 32GB Memory RC4-2400T - HMA84GR7MFR4N-UH ](https://www.ebay.co.uk/itm/287671398501)
| #### Price: £500.00 | £500.00 |
| #### Item ID: 287671398501 | 287671398501 |
| #### Order number: 23-15316-89621 | 23-15316-89621 |
| #### Seller: assetlifecycle[](https://www.ebay.co.uk/ulk/usr/assetlifecycle) 3 Christleton Court Runcorn |
| #### Quantity: 3 | 3 |
| #### Price: £40.00 | £40.00 |
| #### Item ID: 111111111111 | 111111111111 |
| #### Order number: 23-15316-89622 | 23-15316-89622 |
| #### Seller: other_seller |
### Order total:

| Subtotal | £540.00 |
| Postage | £5.00 |
| Total charged to x -2886 | £545.00 |`;

const t = parseOrderEmail(text);
assert.strictEqual(t.items.length, 2);
assert.strictEqual(t.items[0].orderNo, '23-15316-89621');
assert.strictEqual(t.items[0].seller, 'assetlifecycle');
assert.strictEqual(t.items[0].price, 500);
assert.strictEqual(t.items[0].fee, '');
assert.strictEqual(t.items[0].qty, 3);
assert.strictEqual(t.items[1].qty, 1);
assert.strictEqual(t.items[1].itemId, '111111111111');
assert.strictEqual(t.items[1].seller, 'other_seller');
assert.strictEqual(t.total, 545);
assert.strictEqual(t.postage, 5);

assert.deepStrictEqual(
  JSON.parse(JSON.stringify(parseStatusSubject('Refund issued for order 10-15334-30382'))),
  { orderNo: '10-15334-30382', status: '已退款' });
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(parseStatusSubject('Order 117470945429-10085847774517 has been cancelled'))),
  { itemId: '117470945429', status: '已取消' });
assert.strictEqual(parseStatusSubject('🚚 Order update: Dell 32GB'), null);

const s1 = parseSpec('5x SK Hynix 32GB Memory RC4-2400T - HMA84GR7MFR4N-UH');
assert.strictEqual(s1.brand, 'SK Hynix');
assert.strictEqual(s1.capacity, '32GB');
assert.strictEqual(s1.speed, '2400');
assert.strictEqual(s1.partNo, 'HMA84GR7MFR4N-UH');
assert.strictEqual(s1.perListing, 5);

const s2 = parseSpec('128GB (4x32GB) DDR4 2666 ECC RDIMM');
assert.strictEqual(s2.capacity, '32GB');
assert.strictEqual(s2.perListing, 4);
assert.strictEqual(s2.speed, '2666');

const s3 = parseSpec('Samsung 16GB 2Rx4 PC4-17000P M393A2G40DB0-CPB');
assert.strictEqual(s3.brand, 'Samsung');
assert.strictEqual(s3.speed, '2133');
assert.strictEqual(s3.partNo, 'M393A2G40DB0-CPB');

// Titles from the first real backfill where the part number was missed.
const cases = [
  ['HYNIX 128GB SERVER RAM KIT (4X 32GB) PC4-2666V-RB2 RDIMM HMA84GR7CJR4N', 'HMA84GR7CJR4N'],
  ['SAMSUNG 64GB SERVER RAM KIT (4x16GB) PC4-2133P RDIMM REG ECC 2Rx4 M393A2G40DB0', 'M393A2G40DB0'],
  ['SK Hynix 32GB PC4-2666V DDR4 ECC Registered Server Memory HMA84GR7CJR4N -VK RAM', 'HMA84GR7CJR4N-VK'],
  ['Micron 32GB 2Rx4 PC4-2133P Registered ECC Server RAM MTA36ASF4G72PZ-2G1A1 2016', 'MTA36ASF4G72PZ-2G1A1'],
  ['6 x Integral 32GB DDR4 2666MHZ IN4T32GREMSX2 J21 10 SPH Server RAM', 'IN4T32GREMSX2'],
];
cases.forEach(([title, pn]) => assert.strictEqual(parseSpec(title).partNo, pn, title));
assert.strictEqual(parseSpec('6 x Integral 32GB DDR4 2666MHZ IN4T32GREMSX2').brand, 'Integral');
assert.strictEqual(parseSpec('6 x Integral 32GB DDR4 2666MHZ IN4T32GREMSX2').perListing, 6);
assert.strictEqual(parseSpec('Micron 32GB 2RX4 PC4-3200AA-RB2-12 Server RAM MTA36ASF4G72PZ X 16').perListing, 16);
assert.strictEqual(parseSpec('SERVER MEMORY MODULES - 2 x 32GB (total 64GB) pc4 2133mz').perListing, 2);

// --- Direct-supplier invoices (Direct.gs) ---
// Shaped like Drive OCR output of a typical UK trade invoice (fictional data).
const ocr = `INVOICE
Acme IT Recycling Ltd
Unit 4, Example Park, Leeds LS1 1AA
VAT Reg No: GB 123 4567 89
Bill To: Blue Bridgewell LTD
Invoice No: INV-2041
Invoice Date: 21/10/2026
Qty Description Unit Price Amount
20 Samsung 32GB 2Rx4 PC4-2666V M393A4K40CB2-CTD £40.00 £800.00
10 SK Hynix 16GB PC4-2133P HMA42GR7AFR4N-TF £25.00 £250.00
Subtotal £1,050.00
VAT @ 20% £210.00
Total £1,260.00
Company No: 01234567`;
const inv = parseInvoiceText(ocr);
assert.strictEqual(inv.supplier, 'Acme IT Recycling Ltd');
assert.strictEqual(inv.invoiceNo, 'INV-2041');
assert.strictEqual(inv.date, '21/10/2026');
assert.strictEqual(inv.vatNo, 'GB123456789');
assert.strictEqual(inv.net, 1050);
assert.strictEqual(inv.vat, 210);
assert.strictEqual(inv.total, 1260);

// Amounts on the line after the label (common when OCR splits table cells),
// "Total VAT" wording, no VAT number, date written out.
const ocr2 = `Steve's Server Parts
Invoice # 5531
Date: 3 October 2026
1 Job lot of 50x DDR4 RDIMM 16GB
Net Total
900.00
Total VAT
180.00
Amount Due
1,080.00`;
const inv2 = parseInvoiceText(ocr2);
assert.strictEqual(inv2.supplier, '');
assert.strictEqual(inv2.invoiceNo, '5531');
assert.strictEqual(inv2.date, '3 October 2026');
assert.strictEqual(inv2.vatNo, '');
assert.strictEqual(inv2.net, 900);
assert.strictEqual(inv2.vat, 180);
assert.strictEqual(inv2.total, 1080);

// Garbage in -> blanks out, never a throw.
const empty = parseInvoiceText('');
assert.strictEqual(empty.total, '');


// --- Line items ---
// Header "Qty Description ..." and the seller VAT number above the items.
const fict = parseLineItems(ocr);
assert.strictEqual(fict.length, 2);
assert.strictEqual(fict[0].qty, 20);
assert.strictEqual(fict[0].desc, 'Samsung 32GB 2Rx4 PC4-2666V M393A4K40CB2-CTD');
assert.strictEqual(fict[1].amount, 250);

// Item table of a real supplier invoice (header/bank details left out).
const steve = `INVOICE
DATE Tuesday, September 22, 2026
INVOICE# 220926-1
DESCRIPTION QTY PRICE AMOUNT
Faulty 32gb PC4-3200 ECC Reg Samsung M393A4G43AB3-CWEGY 1 £54.00 £54.00
32gb PC4-2666 ECC Reg Hynix HMA84GR7AFR4N-VK 4 £54.00 £216.00
16gb PC4-2400 ECC Reg Hynix HMA82GR7AFR8N-UH 1 £22.00 £22.00
8gb PC4-2133 ECC Reg Kingston KTH-PL421/8G 4 £9.00 £36.00
8gb PC4-2400 ECC Reg Kingston KVR24R17S4/8 1Rx4 12 £9.00 £108.00
4gb PC4-2400 ECC Reg Crucial CT4G4RFS824A 2 £4.50 £9.00
SUB-TOTAL £445.00
VAT £89.00
TOTAL £534.00`;
const li = parseLineItems(steve);
assert.strictEqual(li.length, 6);
assert.deepStrictEqual(JSON.parse(JSON.stringify(li[1])),
  { desc: '32gb PC4-2666 ECC Reg Hynix HMA84GR7AFR4N-VK', qty: 4, price: 54, amount: 216 });
assert.strictEqual(li[4].desc, '8gb PC4-2400 ECC Reg Kingston KVR24R17S4/8 1Rx4');
assert.strictEqual(li[4].qty, 12);
const sInv = parseInvoiceText(steve);
assert.strictEqual(sInv.invoiceNo, '220926-1');
assert.strictEqual(checkTotals(li, sInv), '✓ 明细合计=发票小计');
assert.ok(checkTotals(li.slice(1), sInv).startsWith('⚠'));
assert.strictEqual(checkTotals([], sInv), '⚠ 未识别到明细');

// Same invoice as OCR sometimes lays it out: one column after another.
const cols = `DESCRIPTION
QTY
PRICE
AMOUNT
32gb PC4-2666 ECC Reg Hynix HMA84GR7AFR4N-VK
8gb PC4-2400 ECC Reg Kingston KVR24R17S4/8 1Rx4
4
12
£54.00
£9.00
£216.00
£108.00
SUB-TOTAL £324.00`;
const lc = parseLineItems(cols);
assert.strictEqual(lc.length, 2);
assert.strictEqual(lc[1].qty, 12);
assert.strictEqual(lc[1].price, 9);
assert.strictEqual(lc[1].amount, 108);
// Columnar with price/amount interleaved per row and qty glued to the text.
const inter = `DESCRIPTION QTY PRICE AMOUNT
32gb Hynix HMA84GR7AFR4N-VK 4
8gb Kingston KVR24R17S4/8 1Rx4 12
£54.00
£216.00
£9.00
£108.00
TOTAL £324.00`;
const lt = parseLineItems(inter);
assert.strictEqual(lt.length, 2);
assert.strictEqual(lt[0].qty, 4);
assert.strictEqual(lt[0].amount, 216);
assert.strictEqual(lt[1].price, 9);
// Tab-separated table cells (Doc table flattened by docToText_).
const tab = parseLineItems('Description\tQty\tPrice\tAmount\n16gb Micron MTA18ASF2G72PZ\t3\t£21.00\t£63.00\nTotal £63.00');
assert.strictEqual(tab.length, 1);
assert.strictEqual(tab[0].qty, 3);

assert.strictEqual(partFromDesc('8gb PC4-2133 ECC Reg Kingston KTH-PL421/8G'), 'KTH-PL421/8G');
assert.strictEqual(partFromDesc('8gb PC4-2400 ECC Reg Kingston KVR24R17S4/8 1Rx4'), 'KVR24R17S4/8');
assert.strictEqual(partFromDesc('4gb PC4-2400 ECC Reg Crucial CT4G4RFS824A'), 'CT4G4RFS824A');
assert.strictEqual(partFromDesc('16gb PC4-2400 desktop dimm'), '');

const d = (x) => { const v = parseInvoiceDate(x); return [v.getFullYear(), v.getMonth() + 1, v.getDate()].join('-'); };
assert.strictEqual(d('September 22, 2026'), '2026-9-22');
assert.strictEqual(d('21/10/2026'), '2026-10-21');
assert.strictEqual(d('3 Oct 2026'), '2026-10-3');
assert.strictEqual(d('2026-10-03'), '2026-10-3');
assert.strictEqual(parseInvoiceDate('soon'), 'soon');

// Chaps IT layout (text of a real invoice, bank details left out): qty with
// ".00", 4-decimal unit price, VAT % column, part numbers broken at wraps,
// two totals on one line.
const chaps = `Invoice
Invoice Date 10 Sep 2026
Invoice Number IV-582488
Reference VAT Number
390293290
Description Quantity Unit Price VAT Amount GBP
16GB SK Hynix HMA82GR7AFR8N-VK 16.00 41.6667 20% 666.67
16GB SK Hynix SK Hynix HMA82GR7AFR4N-VK 1.00 41.6667 20% 41.67
16GB SKHynix-HMA82GR7JJR8N- VK 2.00 41.6667 20% 83.33
16GB SK Hynix HMA82GR7CJR8N- VK 3.00 41.6667 20% 125.00
32GB Samsung M393A4K40BB0- CPB 16.00 80.00 20% 1,280.00
Subtotal 2,196.67
Total VAT 20% 439.33 Invoice Total GBP 2,636.00
Total GBP 2,636.00
Due Date: 17 Sep 2026`;
const ci = parseInvoiceText(chaps);
assert.strictEqual(ci.invoiceNo, 'IV-582488');
assert.strictEqual(ci.date, '10 Sep 2026');
assert.strictEqual(ci.net, 2196.67);
assert.strictEqual(ci.vat, 439.33);
assert.strictEqual(ci.total, 2636);
const cl = parseLineItems(chaps);
assert.strictEqual(cl.length, 5);
assert.deepStrictEqual(JSON.parse(JSON.stringify(cl[0])),
  { desc: '16GB SK Hynix HMA82GR7AFR8N-VK', qty: 16, price: 41.6667, amount: 666.67 });
assert.strictEqual(cl[2].desc, '16GB SKHynix-HMA82GR7JJR8N-VK');
assert.strictEqual(cl[4].desc, '32GB Samsung M393A4K40BB0-CPB');
assert.strictEqual(cl[4].qty, 16);
assert.strictEqual(cl[4].price, 80);
assert.strictEqual(checkTotals(cl, ci), '✓ 明细合计=发票小计');
assert.strictEqual(parseSpec(cl[2].desc).partNo, 'HMA82GR7JJR8N-VK');
assert.strictEqual(parseSpec(cl[4].desc).partNo, 'M393A4K40BB0-CPB');

const wrapped = parseLineItems(`Description Quantity Unit Price VAT Amount GBP
SK Hynix-HMA84GR7AFR4N-UH DDR 4 Memory 32GB 20.00 72.4917 20% 1,449.83
SKHynix-HMA84GR7MFR4N-UH DDR4 Memory 32GB 10.00 72.4917 20% 724.92
Subtotal 2,174.75`);
assert.strictEqual(wrapped.length, 2);
assert.strictEqual(wrapped[0].qty, 20);
assert.strictEqual(parseSpec(wrapped[0].desc).partNo, 'HMA84GR7AFR4N-UH');
assert.strictEqual(parseSpec(wrapped[1].desc).capacity, '32GB');

// --- 直采汇总 ---
const sum = summarize([
  { supplier: 'Steve', fileId: 'a', date: new Date(2026, 8, 22), desc: '32gb Hynix', qty: 4, net: 647.5, gross: 777, check: '✓' },
  { supplier: 'Steve', fileId: 'a', date: new Date(2026, 8, 22), desc: '8gb Kingston', qty: 12, net: '', gross: '', check: '✓' },
  { supplier: 'Steve', fileId: 'b', date: new Date(2026, 9, 17), desc: '16gb Hynix', qty: 48, net: 1000, gross: 1200, check: '⚠ 明细合计' },
  { supplier: 'Chaps IT', fileId: 'c', date: new Date(2026, 8, 7), desc: '32GB', qty: 16, net: 1160, gross: 1392, check: '✓' },
  { supplier: 'Chaps IT', fileId: 'c', date: new Date(2026, 8, 7), desc: 'Delivery', qty: 1, net: '', gross: '', check: '✓' },
  { supplier: '', fileId: 'x', date: '', desc: '', qty: '', net: '', gross: '', check: '' },
]);
const plain = (row) => row.map((v) => (Object.prototype.toString.call(v) === '[object Date]'
  ? v.toISOString().slice(0, 10) : v));
assert.deepStrictEqual(JSON.parse(JSON.stringify(sum.map(plain))), [
  ['Chaps IT', 1, 16, 1160, 1392, new Date(2026, 8, 7).toISOString().slice(0, 10), 0],
  ['Steve', 2, 64, 1647.5, 1977, new Date(2026, 9, 17).toISOString().slice(0, 10), 1],
  ['合计', 3, 80, 2807.5, 3369, '', 1],
]);

console.log('all parser tests passed');
