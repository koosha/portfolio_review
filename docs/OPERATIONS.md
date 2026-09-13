# Operating Portfolio Review

Run commands from the checkout. `uv run` uses the committed lockfile and project Python. Keep private inputs, outputs and backups beneath ignored `data/`, `private/` or `backups/`. Do not commit a filled account template or generated report.

## Installation and launch

```sh
uv sync --locked
uv run app.py
```

The default listener is `http://127.0.0.1:8765`, with `data/portfolio.sqlite3` as the collector store. Existing data, selections, snapshots and `.chrome-connector-key` stay in place. Stop the old server with Ctrl-C in its terminal before starting the replacement. A data-directory lock prevents two collector processes using the same database; a busy port is an error, not a reason to kill an unrelated process.

For a different existing data directory:

```sh
uv run app.py --data-dir /path/to/private/data --port 8765
```

The server uses `research-config.json` in that directory when present, otherwise conservative offline defaults. Research startup creates the separate research store. Collector startup adds batch tables without rewriting prior source snapshots. A previously interrupted pull is marked abandoned and cannot become a published batch.

For an isolated synthetic example, choose a directory that does not already exist:

```sh
uv run python -m portfolio_research demo --directory data/demo --serve --port 8766
```

This generates synthetic holdings, market/fundamental inputs, joint scenarios, an explicitly confirmed synthetic mandate, company assessment, EPS/DCF workspace, and a saved review. Its separate collector page initially has no Yahoo accounts. It is not connected to the real pairing key. Later launch the generated example with:

```sh
uv run python -m portfolio_research serve --config data/demo/config.json --data-dir data/demo/collector --port 8766
```

`python -m portfolio_lab` remains a compatibility alias for the new CLI. The reference archive and its independent dashboard are not required.

## Yahoo collection and reconciliation

Use **Data → Connection settings** to pair the Local Portfolio Chrome extension. Yahoo/Google sign-in remains in normal Chrome; no browser cookie export or automated login is used. **Find portfolios**, checkboxes, **Pull latest holdings**, per-portfolio retry, saved holdings/history, capture details, CSV and selected-holdings JSON exports retain their existing behavior.

A pull freezes its selected/requested account scope. Only all-success collection publishes a new batch. A later failed or single-account pull cannot silently replace a complete household batch. Publication records and source row-count verification are separate: `end-observed` is not `count-verified`. The adapter reports this distinction.

For real use, write the editable private configuration once:

```sh
uv run python -m portfolio_research init --data-dir data
uv run python -m portfolio_research supplemental-template --config data/research-config.json --out data/supplemental-template.json
```

`init` refuses to overwrite an existing configuration. The template uses actual source/snapshot IDs and leaves unknown facts null. Complete it from statements/source evidence, then import it in **Data → Account reconciliation**. The common account fields also have labeled forms. Consult [Source schema](SOURCE_SCHEMA.md) for exact allowed fields and decimal conventions.

Required owner evidence includes:

- A real valuation date, independent NAV including cash, explicit cash, account currency and type for each included account. Currency attestation for unlabeled position values is separate from account currency.
- Verified stable security/issuer mappings, dated ticker aliases, instrument type, currencies and any supported company-screen eligibility facts. Plan funds are never guessed from similar retail names.
- Snapshot-bound open tax lots and applicable treatment if a taxable sale is to be reviewed. Average cost alone is insufficient.
- Actual account permissions, dealing rules, locks, limits, benchmark, explicit cash-return assumption and sleeve membership/budget in **Settings**. A confirmed mandate is an owner decision. Reporting defaults to USD; the investment mandate remains unconfirmed.

A changed source snapshot invalidates snapshot-bound account/lot supplements until they are reviewed again. The unexplained NAV residual is not cash. USD is the default reporting currency and the target for conversions and combined totals. Preserve each amount's source currency: a security's quote currency may differ from Yahoo's reported market-value currency or the account balance currency. Automatic FX conversion is not implemented; mixed or unknown value currencies remain blocked without a verified conversion path. The display account filter does not change the analysis scope or denominators.

## Provider and evidence inputs

**Data** imports CSV prices, fundamentals, macro vintages, fund holdings, forecasts and universe records. Inputs are copied into private content-hashed files and versioned. Importing or changing provider settings affects a subsequent monthly review; it does not mutate a saved run or a frozen preview. CSV import validates columns immediately; semantic eligibility, dates, currency, coverage and accounting are checked during analysis.

| CSV | Key columns and conventions |
| --- | --- |
| Prices | `date,security_id,close,adjusted_close`; include `volume,currency,available_at,received_at,source_id` for the dependent liquidity/timing checks. Adjusted closes are total-return observations; do not also add cash distributions to forecast-price evaluation. |
| Fundamentals | `security_id,period_end,available_at`; supported normalized fields include `received_at,revenue,gross_profit,operating_income,net_income,income_common,earnings_definition,operating_cash_flow,capex,assets,assets_begin,debt,cash,currency,source_id`. TTM flows must already use compatible non-overlapping periods. Common earnings yield needs `earnings_definition=common_shareholders`. |
| Macro | `series_id,date,value,vintage_date`; retain `received_at,units,source_id`. A date-only vintage is not proof of intraday availability. |
| Fund holdings | `fund_id,issuer_id,weight,holdings_date,available_at`; weights are fractions of the wrapper, not percentages. Partial coverage retains an unknown residual. |
| Forecasts | `security_id,scenario,horizon_months,return_value,basis,forecast_date`; optional `probability,source,calibration_id`. Use total returns as fractions and one common dated state set across assets and benchmark. `basis=subjective` is the normal supported mode. |
| Universe | `security_id,ticker,issuer_id,instrument_type,currency`, plus explicit eligibility, domicile, share-class, capitalization and accounting metadata required by the screen. A current small universe is not a survivor-free historical universe. |

A blank scenario template is available without generating forecasts:

```sh
uv run python -m portfolio_research forecast-template --config data/research-config.json --as-of 2026-08-31 --out data/forecast-template.csv
```

Optional provider setup:

```sh
uv sync --locked --extra yahoo
```

Select **Live**, the Yahoo price provider and/or SEC/FRED in **Data** deliberately. Supply a short SEC application/contact string there. Supply a FRED key through the server's `FRED_API_KEY` environment variable in your own terminal/secret manager; do not paste it into advanced JSON, reports or Git. The application does not install shell-profile changes or retain API keys in source configuration. SEC/FRED adapters use their documented APIs; Yahoo price enrichment is optional and independent of the Chrome holdings reader. Their failures leave missing data/issues and never insert synthetic data.

Live providers need real network credentials/identities and sufficient history. They do not supply a complete investment universe, account cash, plan permissions or tax lots automatically. See [SEC API documentation](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) and [FRED observations](https://fred.stlouisfed.org/docs/api/fred/series_observations.html) for provider contracts. No successful personal live-provider run is implied by fixture tests.

Company research accepts manual, source-backed evidence and versioned assumptions in **Research**. A fact needs its source, units, actual publication/receipt timestamps and completed human review where required. EPS/multiple scenarios distinguish forward/trailing conventions; DCF requires explicit EV-to-equity bridge inputs, diluted shares, reinvestment and stock-compensation treatment. Review and record a rationale before linking EPS scenario returns. DCF value is not automatically a horizon price target. LLM/imported numbers do not become verified facts or calibrated forecasts merely because they are saved.

## Monthly review, draft and save

Finish a holdings pull first. **Run monthly review** and this CLI use the same tracked application service:

```sh
uv run python -m portfolio_research run --config data/research-config.json --as-of 2026-08-31 --request-key monthly-2026-08
```

Add `--refresh` only when intentionally refreshing configured providers. It does not initiate a Yahoo Chrome holdings pull. Omitting `--as-of` selects the final XNYS session of the previous calendar month. An explicit non-session date rolls to the preceding session and is recorded separately. The 16:00 New York information cutoff, actual market close (including early closes), generation time and next eligible execution close are distinct retained fields. The calendar implementation uses [exchange_calendars](https://github.com/gerrymanoim/exchange_calendars).

A repeated request key and identical request returns its existing job, including a failed result; it does not silently recompute with new data. For an intentional rerun after correcting inputs, use a new key such as `monthly-2026-08-reconciled-2`. Reusing a key with different assumptions is rejected. Optional scheduling should invoke this command with one key per intended month and capture its exit status. Install any OS scheduler yourself with the checkout as working directory, uv on PATH and the required environment; none is installed automatically. Manual execution always remains available.

The monthly job archives normalized inputs, hashes/versions, issues, assumptions and outputs, then creates pending comparator baselines at the first complete snapshot. It also checks earlier saved forecasts against the current run's retained observations and appends evaluation records. Missing exact endpoints or unmatured horizons remain unavailable. Comparator ledgers require separately confirmed actions/flows; monthly code does not invent them.

Long work runs outside HTTP requests. Status persists in SQLite; dead-worker jobs become failed on startup, while an independently running CLI worker is retained. Each browser tab has its own base run/draft in session storage. Drafts are tied to that run; browser storage is not a substitute for **Save run**.

**Recalculate** uses the selected saved inputs without provider calls. Editing makes old results visibly stale. A failed preview retains the last usable result. **Save run** archives the exact successful preview as an immutable child; **Reset** restores the selected run. Switching with a dirty draft offers explicit retain/discard/cancel choices. Changing horizon clears incompatible numerical overrides/priors/cash assumptions unless explicitly replaced. Disable **Use probabilities** for unweighted exploration without destroying the retained scenario values.

CLI replay uses the same calculation path. `RUN_ID` below means an existing ID printed by `run` or shown in the saved-run selector:

```sh
uv run python -m portfolio_research replay --config data/research-config.json --run-id RUN_ID
uv run python -m portfolio_research replay --config data/research-config.json --run-id RUN_ID --patch data/research-patch.json --save
```

A minimal hypothetical patch file is `{"allocation":{"transaction_cost_bps":25}}`. Editable groups are `mandate,signals,risk,allocation,tax`; source paths/SQL/secrets cannot be edited through the public API. Account maps are replaced as explicit complete maps. The resolved-settings/diff view shows what a calculation actually uses. No-change remains a valid recorded decision; candidate review never executes brokerage orders.

## Evaluate and export

Forecast evaluation needs observed endpoint prices with `security_id,date,adjusted_close,available_at,received_at`. Publication/receipt must be timezone-aware timestamps at or after the exact eligible close and no later than evaluation time. Endpoints are the next eligible execution close and the first eligible session at/after the full 6/12/18-month horizon from execution. Nearby prices cannot substitute. A zero terminal price additionally needs confirmed worthless treatment; missing/delisted instruments remain in scope.

```sh
uv run python -m portfolio_research evaluate --config data/research-config.json --run-id RUN_ID --prices data/outcome-prices.csv --evaluation-date 2027-09-02 --out data/forecast-evaluation.json
```

This evaluates the forecast values actually saved after overrides. Original forecasts and decisions remain immutable. Runs generated after their hypothetical execution date are labeled reconstructions and are not prospective evidence. An unmatured result is a useful status, not an error or a zero return.

Comparator evaluation uses separate exact-decimal unit/cash ledgers for no-change, approved benchmark, simple policy, selected model and actual decision:

```sh
uv run python -m portfolio_research evaluate-ledgers --config data/research-config.json --run-id RUN_ID --inputs data/comparator-inputs.json --out data/comparator-evaluation.json
```

The JSON contains `events_by_arm`, `end_prices`, `end_date`, `coverage_confirmed` and `execution_confirmed`. Every arm needs an explicit event list, even when empty. Confirm full corporate-action, distribution, fee, tax-reserve and external-flow coverage; external flows must match across arms. Events have unique `id`, `date`, `account_id`, `currency`, `kind`, and an explicit same-day sequence where needed. Supported kinds are `external_flow`, `split`, `distribution`, `fee`, `tax_reserve`, `trade`, and confirmed mandatory `cash_out`. Trade events need signed decimal quantity, price, explicit cost/reserve and an aware `executed_at`; policy trades must identify and match the frozen `policy_candidate` basket. End prices are `[{"security_id":"SYNTHETIC","currency":"USD","date":"2027-09-02","price":"100.00"}]`. See synthetic examples in `tests/test_prospective.py` for complete executable contracts.

A generated proposal alone is not an invested benchmark/policy. Comparator outputs use dollar investment gains with external flows removed. No cash-flow timing return convention or complete after-tax performance is invented. Subscription/fee events count once. This is prospective recordkeeping, not a complete historical strategy backtest or a claim of superiority.

Export any saved run:

```sh
uv run python -m portfolio_research export --config data/research-config.json --run-id RUN_ID --out data/shared-review
```

JSON and self-contained read-only HTML use the public response schema, excluding internal config paths, SQL, headers and keys. They retain the selected run's actual analysis. Exported financial information should still be treated as private. An export write failure does not discard a successfully archived run; export it later to a writable directory. UI run comparisons show assumption/modeled-outcome differences; realized outcomes appear only in evaluation records.

## Backup, restore and migration

Wait for collection/research work to finish before taking a coordinated project backup. The SQLite backup API captures committed WAL transactions consistently within each database; the files are backed up sequentially, so pausing new work keeps their joint frontier easy to audit.

```sh
uv run python -m portfolio_research backup --config data/research-config.json --out backups/review-2026-09-13
uv run python -m portfolio_research restore --from backups/review-2026-09-13 --into data/restored-review
uv run python -m portfolio_research migrate --config data/restored-review/research-config.json
uv run python -m portfolio_research serve --config data/restored-review/research-config.json --data-dir data/restored-review --port 8767
```

Backup and restore destinations must be new. Backups include source/research databases when present, the collector pairing key, resolved private configuration, normalized CSV inputs/cache and a checksum manifest. They are not application-encrypted. Store them privately. Restore verifies hashes, rejects traversal/symlinks/colliding output paths before creating its destination, relocates local CSV/config paths and does not overwrite the original data directory. Inspect the restored app on a separate port before switching. A restore changes the data directory, not your Chrome extension's configured port automatically.

Research migrations are additive and preserve immutable prior runs/records. An unknown schema version is rejected before writes; use compatible code or a verified backup rather than deleting tables. Collector migrations belong to normal collector startup. Do not point the research store at the source via a second path, symlink or hardlink.

## Troubleshooting and readiness

- **No coherent batch:** finish a full selected-account pull. Legacy snapshots remain inspectable but cannot prove a historical batch.
- **No portfolio NAV/cash/currency/date:** review snapshot-bound supplements; a successful web-table capture cannot invent these facts.
- **No QVM score:** inspect eligibility, contemporaneous universe coverage, currency/accounting, 60 trading-session liquidity and exact momentum endpoints. A missing score does not remove an owned asset from risk/coverage.
- **Draft/blocked/infeasible candidate:** inspect its account funding, mandate, locks, dealing rules, lot/tax coverage and scenario/risk diagnostics. No constraint is silently relaxed.
- **No weighted return:** complete a consistent joint probability distribution or deliberately use unweighted scenarios; explicit cash return is required when cash is held.
- **Provider failure:** useful retained inputs remain available, with errors. Fix that provider or import reviewed data and submit a new review.
- **Chrome `ERR_BLOCKED_BY_CLIENT`:** the browser is blocking the local page before application interaction. Review Chrome/extension rules for the exact loopback URL yourself; changing server calculation code cannot fix that browser decision.
- **Interrupted/failed job:** old runs remain available. Correct the cause and use a new request key. API errors redact private paths and credentials.

Operational readiness, software correctness, forecast calibration and investment outperformance are separate claims. The first live review still depends on the owner's reconciled data and preferences. Three successful monthly cycles would provide operational experience; they do not establish investment alpha.
