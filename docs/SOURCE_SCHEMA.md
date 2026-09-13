# Collector source and research mapping

Portfolio Review keeps the Yahoo collector as the sole owner of holdings writes.
The analytical adapter opens `data/portfolio.sqlite3` with SQLite URI `mode=ro`,
`query_only=ON`, `trusted_schema=OFF`, a restricted SQL authorizer, and one pinned
read transaction. It does not instantiate the writable collector `Store`, migrate
its tables, copy a live database file, or read the Chrome pairing key.

The research database must be separate. The application checks resolved paths and
filesystem identity so neither a symlink nor a hardlink can alias the source.
WAL pages are read through SQLite's transaction machinery. The normalized bundle
hash, exact snapshot IDs, and captured CSV hashes describe the replay input;
hashing a live main SQLite file alone would not describe its transactional state.

## Observed source schema

| Object | Keys and important fields | Meaning |
| --- | --- | --- |
| `sources` | `id` primary key; unique `url`; `name`, `account`, `currency`, `created_at`, `last_checked`, `last_error`, `selected`, `navigation_url`, `display_order`, legacy `managed` | One collector portfolio boundary. `account` is an unvalidated label, not an account type. Source currency is a label, not an FX conversion. |
| `snapshots` | `id` primary key; `source_id` foreign key; `captured_at`, `sha256`, `filename`, `raw_csv`, `rows_json`, `warnings_json`, `positions_indexed`, `capture_json` | Immutable per-account source records after a successful import. `rows_json` preserves cash and quantity-missing rows. |
| `positions` | Primary key `(snapshot_id,row_number)`; `source_id`, symbol/name, quantity, price, purchase_price, average_cost, total_cost, market_value, trade_date, currency | A derived collector index. Only rows with a known quantity are indexed; it is not the complete research input. Decimal quantities and amounts are text. |
| `latest_positions` | Selected sources joined to each source's independently greatest snapshot ID | Useful collector inspection, but cannot establish a common portfolio snapshot after a partial refresh. The research adapter does not use this view. |
| `preferences` | `key` primary key, `value_json` | An observed legacy table. This integration leaves it untouched. |
| `import_batches` | Opaque `id`; created/completed timestamps; status; requested source IDs; original selected scope; completeness | Ingestion-owned publication manifest added by the collector migration. |
| `import_batch_members` | Primary key `(batch_id,source_id)`; snapshot ID, receipt time, status, completeness, error | Exact immutable input membership when a batch publishes. |

The source contains no validated security/issuer registry, account types,
independently reported NAV, quote valuation dates, FX rates, transaction ledger,
corporate actions, or true open tax lots. Missing facts remain missing. No private
account identifiers, holdings, totals, or source URLs are included in this document.

## Publication and dates

A refresh freezes its requested source IDs and the selected scope. Each successful
account import atomically saves its snapshot/index/last-check state and batch
membership. Other accounts can still finish if one fails, preserving the existing
collector workflow. The batch publishes only when every requested account has
succeeded. A timeout, disconnect, failed capture, or restart cannot publish a
partial batch. Restarted unfinished batches are marked abandoned by the importer.

`published` means the requested collection finished successfully. It is separate
from Yahoo row coverage: `count-verified` requires a reconciled table count;
`end-observed` means the loaded table ended without an exposed count. A published
batch can therefore retain unverified coverage. A supplemental explicit account
reconciliation can establish analytical completeness, while the original capture
quality remains recorded and visible.

The adapter chooses the newest publication covering the requested account scope.
A single-account pull cannot replace a full-portfolio batch. Failed newer batches
leave the prior published batch visible with its original timestamps and a warning.
With no usable publication, independently captured legacy holdings remain visible
as **partial input** and portfolio-wide allocation stays blocked. The application
does not invent a historical batch to make old data look complete.

Both `snapshots.captured_at` and `sources.last_checked` are server receipt/import
timestamps. They are not Yahoo quote times. The extension's capture timestamp is
also not a quote valuation date. An identical payload and capture metadata reuse
the last snapshot ID and original snapshot timestamp; a new batch member still
records the new successful receipt time. This does not advance a supplied quote
valuation date automatically.

`load_collector(..., as_of)` limits received source records through the requested
calendar date in America/New_York. The monthly application service separately
records its trading-calendar decision cutoff and generation time. Only explicitly
supplied valuation dates can date positions. All accounts must share one explicit
valuation date; missing or conflicting dates block a complete portfolio conclusion.

## Canonical input and money

The adapter returns `accounts`, `positions`, `securities`, and `tax_lots` pandas
frames compatible with the shared research calculations, plus `as_of`,
`valuation_date`, `issues`, `sources`, `collector`, `scope`, and `mode='offline'`.
`ledger` contains the exact canonical decimal strings, source row references,
capture provenance, and quantity-missing records excluded from numerical holdings.
Monetary/quantity/basis precision is preserved up to the source parser's bounded
decimal range; there is no silent cents rounding during ingestion. Floating-point
conversion occurs only when producing numerical frames. Account reconciliation
first uses exact Decimal arithmetic and a one-cent mismatch threshold.

`Total Cash` with absent quantity is an explicit Yahoo cash row. Its market value
is used if present; for the known Yahoo table capture method, the observed balance
in Last Price is accepted when market value is absent. Missing cash is not zero.
The unexplained difference between NAV and securities is never cash. Captured row
totals are not independently reported account NAV. Source average cost and total
cost are not tax lots.

Value-bearing rows with unknown quantity remain holdings with a blocking sizing
issue. Rows with neither held quantity nor market value remain archived as
watchlist/unknown records. Tickers are raw aliases, not identities. Unmapped
securities receive deterministic opaque source-local unresolved IDs; name
similarity never maps unknown funds. Explicit dated mappings can keep GOOG and
GOOGL as different security IDs under one issuer ID.

## Supplemental JSON, version 1

The Settings template is created by
`supplemental_template(source_path, account_ids=None)`. It contains only editable
account evidence and security mappings, with missing values set to JSON `null`.
It contains no filesystem paths, Yahoo URLs, credentials, pairing keys, or raw
source JSON. It is still a **private local document** because account names and
owned symbols are personal information.

`validate_supplemental(payload)` accepts only these top-level fields:

| Group | Fields |
| --- | --- |
| `version` | Integer `1` |
| `accounts` | `source_id`, `snapshot_id`, `name`, `account_type`, `currency`, `position_currency`, `cash`, `total_value`, `complete`, `valuation_date`, `tax_rate`, `tax_jurisdiction` |
| `securities` | `source_id`, `raw_symbol`, `security_id`, `issuer_id`, `name`, `ticker`, `sector`, `instrument_type`, `currency`, `cik`, `eligible`, `market_cap`, `market_cap_as_of`, `market_cap_available_at`, `market_cap_received_at`, `exchange`, `share_class`, `domicile`, `equity_type`, `valid_from`, `valid_to` |
| `tax_lots` | `source_id`, `snapshot_id`, `security_id`, `lot_id`, `acquired_date`, `quantity`, `basis_per_share`, `currency` |

Accounts and lots bind an **exact snapshot ID**. A changed snapshot cannot inherit
old balances, lot scope, or a completeness declaration. An unchanged reused
snapshot keeps its supplied valuation date until the user supplies new evidence.
`currency` declares the account balance currency. `position_currency` separately
attests the unit of otherwise unlabeled values in that snapshot; it must only be
filled when all such values use that currency. It does not convert currencies.
Explicit row currencies are retained. Unsupported multi-currency evidence blocks
reconciliation until a verified conversion path is provided.

Amounts, quantities, lot basis, market cap, and tax rates accept finite decimal
strings or JSON numbers and normalize to exact decimal strings. Use strings to
avoid a JSON producer rounding a decimal first. Rates are fractions in `[0,1]`.
Currency codes are uppercase three-letter codes. Dates are `YYYY-MM-DD`.
`complete` and `eligible` are booleans or null, never truthy strings. Declaring an
account complete requires snapshot ID, valuation date, cash, NAV, and currency.
The supported proposal account types are `taxable` and `retirement`; other or
missing types cannot authorize allocation. Eligibility is a separate explicit
decision from whether an instrument is owned.

Security IDs and issuer IDs are stable opaque IDs supplied by a verified mapping;
they must not be reused for a different instrument. `valid_from` and `valid_to`
describe the raw-symbol alias period. An absent `valid_from` remains unresolved
for analytical purposes. Conflicting or overlapping mappings raise issues.
Different classes retain separate IDs even when their issuer matches.
The ledger separately preserves every applied source alias, venue, share class,
and effective-date interval. Domicile and equity type are nullable explicit facts:
the US ordinary-common company screen requires `domicile: "US"` and
`equity_type: "ordinary_common"`. A missing value never becomes either by default.
Market capitalization must describe issuer-wide common equity, with its explicit
`market_cap_as_of` date and actual publication/receipt timestamps. The latter two
require a full ISO timestamp with an offset; a calendar date never establishes
intraday availability. The analytical screen checks these dates against its
cutoff and freshness policy instead of treating an undated cap as current.

Tax lots must describe quantities still open at the exact source snapshot. Every
lot requires its stable identity, acquisition date, positive open quantity,
adjusted basis per share, and currency. Average cost does not fill these fields.
Lots from other snapshots, future acquisitions, and unmatched securities are
excluded with explicit issues; the conditional tax service additionally checks
lot coverage and the relevant account's tax assumptions.

Example using only synthetic data:

```json
{
  "version": 1,
  "accounts": [{
    "source_id": 1,
    "snapshot_id": 1,
    "account_type": "retirement",
    "currency": "USD",
    "position_currency": "USD",
    "valuation_date": "2026-08-31",
    "cash": "10.00",
    "total_value": "110.00",
    "complete": true
  }],
  "securities": [{
    "source_id": 1,
    "raw_symbol": "DEMO",
    "security_id": "security-demo-001",
    "issuer_id": "issuer-demo-001",
    "ticker": "DEMO",
    "instrument_type": "equity",
    "currency": "USD",
    "valid_from": "2020-01-01",
    "eligible": false
  }],
  "tax_lots": []
}
```

Collector exports remain private local holdings exports. Shareable research
responses/exports use deliberately selected public fields rather than exposing
the original source records, saved private paths, or SQL configuration.
