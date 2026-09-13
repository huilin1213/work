# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository contents

This repo holds two independent tools, each self-contained under its own
`tools/<name>/` directory with its own `requirements.txt`:

- `tools/xero-dhl-invoice/` — parses DHL Commercial Invoice PDFs (generated
  per shipment) and creates matching **Draft** sales invoices in Xero via the
  Accounting API.
- `tools/vat-reconciliation/` — matches VAT purchase invoices (kept in a
  manually-maintained Excel ledger) against HSBC/Revolut bank statement
  exports, so every bank expense can be traced to the VAT amount on its
  invoice, and flags eBay-order expenses that don't have an invoice yet so
  they can be chased with the seller.

The "Commands" and "Architecture" sections below cover `xero-dhl-invoice`;
see `tools/vat-reconciliation/README.md` for that tool's own commands and
design notes (it isn't duplicated here).

## Commands

All commands in this section run from `tools/xero-dhl-invoice/`.

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

Under `tools/vat-reconciliation/`, any real invoice ledger or bank statement
export (`*.xlsx` / `*.csv`, apart from the blank `ledger_template.xlsx`) is
gitignored at the repo root too — those files carry real vendor names,
amounts and account activity.
