# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository contents

This repo holds two independent tools under `tools/`:

- `tools/xero-dhl-invoice/` — parses DHL Commercial Invoice PDFs (generated per
  shipment) and creates matching **Draft** sales invoices in Xero via the
  Accounting API.
- `tools/ebay-purchase-sync/` — scrapes the signed-in user's own eBay Purchase
  history web pages (no official buyer-side purchase API exists) and syncs
  per-order line items (seller, order date/number, tracking number, item
  price/qty, VAT, buyer protection fee, postage fee) into a local Excel file,
  with a polling watcher for "new order shows up automatically" sync.

The "Architecture" and "gotchas" sections below are organized per tool.

## Commands

### `tools/xero-dhl-invoice/`

```bash
pip install -r requirements.txt      # deps: pdfplumber, requests, python-dotenv
cp .env.example .env                 # then fill in XERO_CLIENT_ID etc.

python3 xero_auth.py                 # one-time OAuth2 PKCE browser login; writes .xero_tokens.json
python3 list_tax_rates.py            # queries Xero /TaxRates for this org's real TaxType codes
python3 main.py <pdf> [<pdf> ...]    # parse -> validate -> dedupe -> create Draft invoice(s)
python3 watch_folder.py [dir]        # polls a folder every 10s and runs main.process_one() on new PDFs
```

### `tools/ebay-purchase-sync/`

```bash
pip install -r requirements.txt      # deps: playwright, openpyxl, python-dotenv
playwright install chromium          # first-time Playwright browser download
cp .env.example .env                 # then fill in EBAY_DOMAIN etc.

python3 ebay_login.py                # one-time interactive login; writes storage_state.json
python3 main.py [year ...]           # scrape purchase history -> upsert rows into EXCEL_PATH
python3 watch_sync.py                # polls every SYNC_INTERVAL_MINUTES and runs main.sync_once()
```

There is no test suite or linter configured. The way this codebase has been
validated so far: `python3 -m py_compile <file>.py` for a syntax check, plus
dry-running `dhl_parser.parse_dhl_invoice()` / `main.build_invoice_payload()`
against real sample PDFs and comparing the computed line-item total against
the PDF's own "Total Invoice Amount" field before touching the Xero API.

## Architecture

Four independent modules, wired together only through `main.py`:

- **`dhl_parser.py`** — pure PDF-to-dataclass parsing (`DHLInvoice`,
  `LineItem`, `ShipToParty`). No Xero/network dependency, safe to run/test in
  isolation.
- **`xero_client.py`** — thin `requests`-based wrapper for the Xero
  Accounting API (token load/refresh, `find_invoice_by_reference`,
  `create_invoice`). Reads/writes `.xero_tokens.json` next to itself.
- **`xero_auth.py`** — one-shot OAuth2 PKCE flow (spins up a local
  `http.server` on `XERO_REDIRECT_URI`'s port to catch the callback). Only
  ever run interactively by a human in a real terminal/browser — never from
  `watch_folder.py` or any automation, since it blocks on user login.
- **`main.py`** — glues the two together (`build_invoice_payload`,
  `process_one`) and is also the CLI entrypoint. `watch_folder.py` imports and
  calls `main.process_one()` directly rather than shelling out, so the two
  entrypoints (manual CLI vs. folder-polling daemon) share identical
  parse/validate/dedupe/create logic.

### PDF parsing gotchas (`dhl_parser.py`)

- DHL's "SHIP FROM / SHIP TO" block is two visual columns. Naively flattening
  page text interleaves both columns line-by-line and misaligns when one
  side has fewer lines than the other. `_extract_ship_to()` instead buckets
  `page.extract_words()` by rounded `top` (row) and splits left/right by
  `x0` vs. page midpoint — this is the only reliable way to isolate the
  ship-to (customer) side.
- DHL sometimes bundles a Waybill/shipping-label page *before* the actual
  Commercial Invoice pages in the same PDF. `parse_dhl_invoice()` therefore
  scans all pages for the one whose text contains an `AWB No:` line rather
  than assuming page 0 is the invoice.
- The header regex must be matched against a single extracted line, not the
  whole page text — `Invoice No:` is frequently blank, and letting `\s*`
  span a newline lets it swallow the next line's content.
- Line items come from `page.extract_tables()` (this DHL template has clean
  gridlines, so table extraction is reliable), not text regex. Rows are kept
  only when the first cell is a bare digit, which filters out the repeated
  table header on continuation pages.
- After building an invoice, `main.process_one()` sums `LineItem.sub_total`
  and compares it to the PDF's own "Total Invoice Amount" as a sanity check
  (warns, does not abort).

### Xero integration gotchas

- **Scopes**: as of the 2026-04-29 Xero scope migration, the old broad
  `accounting.transactions` scope no longer works for newly created apps —
  use the split scopes instead. Current set (`xero_auth.py` `SCOPES`):
  `openid profile email offline_access accounting.invoices
  accounting.contacts accounting.settings.read`. `accounting.settings.read`
  is required for `/TaxRates` (used by `list_tax_rates.py`), not just
  invoices/contacts. Changing scopes requires re-running `xero_auth.py` to
  re-consent — a stored token doesn't retroactively gain new scopes.
- **TaxType is org-specific**: never hardcode a guessed value. The UI only
  shows a human-readable tax rate *name*; the API needs the internal
  `TaxType` code (e.g. `ZERORATEDOUTPUT` for "Zero Rated Income" on a UK
  ledger), and codes differ by region. `list_tax_rates.py` queries
  `/TaxRates` directly and prints the actual valid codes for the connected
  org — use that instead of guessing from the web UI.
- **ItemCode is optional-but-fragile**: `build_invoice_payload()` sets
  `LineItem.ItemCode` to the DHL row number for cross-referencing. Some Xero
  orgs reject item codes that aren't pre-registered in their Items master
  data. `process_one()` catches that specific `RuntimeError`, strips
  `ItemCode` from every line, and retries once rather than failing the whole
  invoice.
- Dedup key is `Reference = f"DHL AWB {awb_no}"` on the Xero invoice,
  checked via `find_invoice_by_reference()` before creating — reprocessing
  the same shipment's PDF (e.g. via `watch_folder.py` re-scanning) is a
  no-op, not a duplicate invoice.
- Invoices are always created with `Status=DRAFT` (`XERO_INVOICE_STATUS` in
  `.env`) — nothing in this codebase sends/authorises an invoice
  automatically; that's a deliberate manual step in the Xero web UI.

### Deployment

`watch_folder.py` is meant to run unattended, typically installed as a macOS
LaunchAgent (`~/Library/LaunchAgents/com.bluebridgewell.dhl-invoice-watcher.plist`,
not checked into this repo) with `python3 -u` (unbuffered, so the log file
updates in real time) and stdout/stderr redirected to log files outside the
repo. `xero_auth.py` must still be run manually/interactively at least once
before that, and again if `.xero_tokens.json`'s refresh token expires from 60
days of inactivity.

## `tools/ebay-purchase-sync/` architecture

Same "independent modules wired together only through `main.py`" shape as the
DHL tool:

- **`ebay_scraper.py`** — pure Playwright-page-to-dataclass parsing
  (`EbayOrder`, `LineItem`). No Excel dependency.
- **`excel_sync.py`** — thin `openpyxl` wrapper: creates the workbook/sheet if
  missing, appends only rows whose dedup key (`order number + product name +
  qty + item price`) isn't already present. Never rewrites/overwrites existing
  rows, so manual edits/pivot tables the user adds to the file survive re-syncs.
- **`ebay_login.py`** — one-shot interactive login (launches a *headed*
  browser, blocks on `input()` until the human finishes login/2FA in it, saves
  `context.storage_state()`). Only ever run interactively by a human — never
  from `watch_sync.py`, since it blocks on user login. Same role as
  `xero_auth.py` in the DHL tool.
- **`main.py`** — glues scraper + excel_sync together (`sync_once`) and is the
  CLI entrypoint. `watch_sync.py` imports and calls `main.sync_once()`
  directly, same pattern as `watch_folder.py` in the DHL tool.

### eBay scraping gotchas (`ebay_scraper.py`)

- There is no official eBay API for a buyer's own purchase history, so this
  scrapes the rendered Purchase history / order-detail pages of a real logged-in
  session. eBay's CSS class names are obfuscated and change across deploys, so
  parsing intentionally matches on the page's *visible label text* ("Order
  number", "Tracking number", "Buyer protection", ...) via `LABELS` at the top
  of the file, rather than fixed selectors — closer to how a human reads the
  page, and less likely to break on a pure styling/markup change. If a field
  comes back empty, the fix is almost always adding eBay's actual wording as a
  new candidate to `LABELS`, not touching the parsing logic.
- Order list pages are fetched per year via `?filter=year:YYYY` on
  `/mye/myebay/purchase`; `list_order_detail_links()` also clicks any "show
  more/load more" button repeatedly to page through a year's full order list
  before collecting "order details" links.
- One order can contain several distinct products at different qty/price;
  `_extract_line_items()` finds each "Item price" occurrence and looks
  backward/forward across a few lines for the nearest title and quantity
  rather than assuming a fixed row layout.
- Money parsing (`_parse_money`) has to handle both point-decimal locales
  (`ebay.co.uk`/`.com`, e.g. `1,234.56`) and comma-decimal locales (some EU
  sites, e.g. `12,34 €`) — it treats a trailing `,dd` with no `.` present as
  the decimal separator, otherwise treats `,` as a thousands separator.
- After parsing, `parse_order_detail()` sums line-item prices + postage + VAT
  + buyer protection and compares against the page's own "Order total" as a
  sanity check (warns with the order's detail URL, does not abort) — same
  spirit as the DHL tool's line-item-sum-vs-invoice-total check.
- **This was built without the ability to log into a real eBay account and
  verify selectors against the live DOM.** Treat the first real run's output
  as unverified until spot-checked against a few orders' actual detail pages
  (see README "六、第一次跑完务必人工核对").

### Deployment

`watch_sync.py` is meant to run unattended on the user's own machine (not this
cloud session — cloud sessions/containers are ephemeral and don't stay up to
poll), same LaunchAgent pattern as `tools/xero-dhl-invoice/watch_folder.py`.
`ebay_login.py` must be run manually/interactively at least once before that,
and again whenever `storage_state.json`'s session cookies expire or get
invalidated (`main.py` detects a bounce back to the sign-in page and exits
with a message to re-run it rather than silently syncing nothing).

## Secrets

- `tools/xero-dhl-invoice/.env` and `.xero_tokens.json` hold the Xero client ID
  and OAuth tokens and must never be committed.
- `tools/ebay-purchase-sync/.env` and `storage_state.json` hold eBay site
  config and the logged-in session's cookies and must never be committed.
- All of the above are gitignored at the repo root, along with `*.pdf` (DHL
  invoices contain customer PII) and `tools/ebay-purchase-sync/*.xlsx` (the
  synced purchase history is personal purchasing data).
