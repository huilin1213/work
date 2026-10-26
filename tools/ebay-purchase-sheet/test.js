// Offline check of the pure parsers in Code.gs: `node test.js`
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ctx = { module: { exports: {} } };
vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'Code.gs'), 'utf8'), ctx);
const { parseOrderEmail, parseStatusSubject, parseSpec } = ctx.module.exports;

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

console.log('all parser tests passed');
