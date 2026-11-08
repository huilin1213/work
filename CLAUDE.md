# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository contents

Two independent tools:

- `tools/xero-dhl-invoice/` — parses DHL
Commercial Invoice PDFs (generated per shipment) and creates matching **Draft**
sales invoices in Xero via the Accounting API. Everything below refers to that
tool unless noted otherwise.
- `tools/ebay-ram-monitor/` — polls the eBay Browse API (app token, no
  buyer auth, never places orders) for DDR4 server RAM listings and pushes
  new / restocked / reappeared / price-dropped listings to ntfy or Telegram.
  See the "eBay RAM monitor" section at the end.

## Commands

All commands run from `tools/xero-dhl-invoice/`.

```bash
pip install -r requirements.txt      # deps: pdfplumber, requests, python-dotenv
cp .env.example .env                 # then fill in XERO_CLIENT_ID etc.

python3 xero_auth.py                 # one-time OAuth2 PKCE browser login; writes .xero_tokens.json
python3 list_tax_rates.py            # queries Xero /TaxRates for this org's real TaxType codes
python3 main.py <pdf> [<pdf> ...]    # parse -> validate -> dedupe -> create Draft invoice(s)
python3 watch_folder.py [dir]        # polls a folder every 10s and runs main.process_one() on new PDFs
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

## Secrets

`.env` and `.xero_tokens.json` (both under `tools/xero-dhl-invoice/`) hold the
Xero client ID and OAuth tokens respectively and must never be committed —
both are gitignored at the repo root, along with `*.pdf` since DHL invoices
contain customer PII.

## eBay RAM monitor (`tools/ebay-ram-monitor/`)

```bash
python3 monitor.py --test-notify   # check push channel
python3 monitor.py --once          # one search, print newest 20
python3 monitor.py                 # daemon; first run silently baselines all results
python3 find_category.py "<q>"     # Taxonomy API category suggestions
python3 check_item.py <id|url>      # why was/wasn't this listing alerted (1 API call + local DB)
```

- `ebay_client.py` (token + token-bucket `RateLimiter` sized from
  `EBAY_DAILY_CALL_BUDGET`; every API call goes through it), `store.py`
  (SQLite `ebay_monitor.db`, gitignored), `notifier.py`, `monitor.py`
  (scheduler + detection logic).
- Three interleaved tasks: fast poll (`sort=newlyListed`, page 1 of 200),
  full sweep one page per loop iteration (Browse API caps offset+limit at
  10000), and `getItems` stock rechecks (20 ids/call) using leftover budget.
- An item is only marked missing after 2 consecutive sweeps it was absent
  from (pagination shifts during a sweep can skip an item once). Missing is
  evaluated per source category (`items.src_cat`); for a query whose results
  exceed the 10000 window, only items created after the oldest one actually
  scanned are eligible (`SweepState.cutoffs`), so aging out of the window is
  never mistaken for "sold out"; `getItems`
  omitting an item does NOT mark it missing, to avoid false "reappeared"
  alerts. Restock = (available + sold) increased, or OUT_OF_STOCK -> in stock.
- Batch `getItems` is a limited-release method: a normal production keyset
  gets 403 (errorId 1100). `Monitor.stock_check()` then permanently falls back
  to single `getItem` calls, checks each single-quantity listing only once,
  and spends the stock budget on multi-quantity listings.
- `EBAY_QUERIES` entries are `query @ categoryId` (Browse search takes one
  category per call, so multiple categories = multiple queries). Each
  (query, category, filters) combo gets its own silent baseline
  (`meta` key `baseline:...`) the first time it's seen, so adding a
  category/query later never floods the phone with existing listings.
- `EXCLUDE_SELLERS` becomes an `excludeSellers:{a|b}` Browse filter
  (`Config.search_filters`), so eBay drops those listings server-side. It is
  deliberately NOT part of the baseline key (`Config.filters` is), so editing
  the seller list never triggers a re-baseline. Seller usernames are never
  persisted (Marketplace Account Deletion opt-out); already-stored items from
  an excluded seller are `retire()`d when a stock check sees the seller.
- Daily summary push at `DAILY_SUMMARY_TIME` (local HH:MM, default 21:00,
  sent once per day via `meta` key `last_summary`, so a Mac asleep at that
  time sends it on wake the same day). Counts come from the `events` table;
  alerts suppressed by EXCLUDE_KEYWORDS/EXCLUDE_SELLERS are logged as
  `<KIND>_SKIP` so they don't count as pushes.
- No test suite; validated by a fake-client simulation driving
  `Monitor.baseline/fast_poll/sweep_step/stock_check`.
