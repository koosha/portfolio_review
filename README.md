# Portfolio Review

A local application for collecting Yahoo Finance holdings and conducting a repeatable monthly portfolio review. Your normal Yahoo/Google sign-in stays in Chrome. Holdings collection, company evidence, EPS/multiple and DCF valuations, scenarios, feasible alternatives, saved reviews, and prospective evaluation share one interface.

The application produces research records and conditional comparisons. It does not place orders. Missing data remains visible; synthetic demo preferences never become settings for real accounts.

## Start

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Chrome. From this checkout, once:

```sh
uv sync --locked --extra yahoo
uv run app.py
```

The `yahoo` extra installs the market-data adapter. Without it the review still runs, and every price, statement and estimate it could not fetch is reported as missing rather than guessed.

Open [Portfolio Review](http://127.0.0.1:8765). Keep the terminal running. The existing `data/` database, portfolio selections and Chrome pairing key are retained. Only one server can own a data directory, and only one can listen on a port. The first start writes the settings it resolved to `data/research-config.json` and reads that file from then on; every recovery command below defaults to it, and editing it changes what the next review uses.

For an isolated, fully populated example:

```sh
uv run python -m portfolio_research demo --directory data/demo --serve
```

Open [the synthetic demo](http://127.0.0.1:8766). Use a new directory for each newly generated demo. The dataset and company assessments are explicitly synthetic.

Two public sources take one optional setting each, both applied once and both kept out of exports. **Data → Provider health → SEC contact user agent**, then **Save provider settings**, stores the SEC contact string (`data.sec_user_agent`): an application name and your real contact email, which SEC requires — until it holds a real address, SEC fundamentals are skipped and the review says `SEC_CONTACT_REQUIRED` instead of fetching. FRED reads its key from the `FRED_API_KEY` environment variable (rename that variable with `data.fred_api_key_env`); export it in the shell that starts the server. Neither value is ever displayed back once saved.

## Connect Yahoo

1. In `chrome://extensions`, enable Developer mode and **Load unpacked** this checkout's `chrome-extension` folder — **Local Portfolio · Yahoo connector**, version 1.2.0.
2. In the app's **Data → Connection settings**, press **Copy key** and paste that pairing key into the extension popup, then save it there.
3. Press **Open Yahoo** and sign in normally. Use **Find portfolios**, select portfolios with checkboxes, then **Holdings → Pull latest holdings**.
4. Review the saved holdings and capture details. Reload the extension after extension code changes; restart the server after Python changes.

Selections persist. Unchecked accounts retain history but are excluded from pulls and exports. A failed collection never publishes a new complete analytical batch. A successful pull without an independently verified row count is visibly unverified.

## Monthly review

One action runs the month. **Update & analyze** in the header collects the newest holdings through Chrome, resolves identity and currency, fetches the configured providers, analyzes and publishes one review. The stage strip under the header reports what each step did and what it could not do; **Cancel** stops the operation before it publishes. A duplicate click rejoins the operation already running, reloading the page rejoins it as well, and an operation that is cancelled or fails writes nothing — the last usable review stays exactly as it was.

1. **Update & analyze** — collect, resolve, fetch, analyze and publish as one operation.
2. **Answer the exceptions** — the **Exceptions** panel on Overview counts what the sources and provider metadata could not answer (`N open`, otherwise `None open`); **Save** on a card records that answer as dated supplemental evidence and the count falls.
3. **Read the results** — **What changed**, **Portfolio risks**, **Holdings to review** and **New candidates** on Overview, with Holdings and Research showing the saved review beside the newest collection, each with its own dates.
4. **Adjust the assumptions** — **Research → Company research** (**EPS & multiple**, **DCF**, **Evidence & thesis**) and **Scenarios** hold the workspace and the explicit assumptions; edits stay in a draft until they are calculated.
5. **Recalculate** — **Recalculate** in the shared context bar under the header re-runs the draft against that run's frozen inputs, **Save run** archives an immutable child run, and **Reset** restores the saved inputs.
6. **Record the decision** — **Review** holds **Feasible alternatives**, funding, no-action rationale and exports; **Save monthly decision** retains the chosen one, **Compare runs** sets two saved runs side by side and **Evaluate saved forecasts** scores an earlier one.

**Data → Historical month-end review (advanced)** recalculates a past month end from saved and cached inputs; it is not part of the monthly flow. **Data → Provider health** shows what each source has already answered and the next step when it cannot, without displaying any credential.

**Overview** shows the newest collected holdings as soon as a collection finishes, with no saved review or mandate required. Each account lists its capture and server receipt times; a receipt time is not a quote time. Captured amounts are shown as observed, grouped by explicit value currency without FX conversion and never labeled NAV, and the source valuation time stays unknown unless dated account evidence attests it. The last completed analysis appears separately with its own market date, information cutoff and generation time, so current holdings and saved reviews never share a denominator.

Use **Data** to reconcile dated account NAV/cash, currency and security identity. Import missing facts and scenarios through validated forms/CSV/JSON. Use **Settings** to supply your actual mandate, account permissions and explicit sleeve allocation.

Reporting and combined totals default to USD. Listing identity and value currencies are established automatically from Yahoo quote symbols, provider listing metadata and dated FX observations (Bank of Canada Valet and Yahoo FX). Amounts are presented in USD beside the original amount and currency, each conversion names its FX pair and observation date, and an amount already reported in USD is never converted again. Overview lists only the genuine exceptions: an ambiguous or unknown listing, a value currency that the quantity × price arithmetic cannot establish, or a missing or stale FX observation. Listing and currency answers are saved as dated supplemental evidence; FX exceptions clear once refreshed market data supplies a dated observation. Holdings that cannot be presented in USD are counted as unconverted, never silently dropped. Reload the Local Portfolio Chrome extension (1.2.0 or newer) so collections capture Yahoo quote symbols.

### Automatic research

Market data is fetched by a versioned adapter (`yfinance-adapter-1`) for every held listing: daily prices and corporate actions, annual, quarterly and trailing-twelve-month statements, consensus estimates, price targets and recommendation counts, ETF and mutual-fund top holdings and sector weights, and dated filings, dividends, splits and news. Each answer is archived with its receipt time and a content hash; a cached answer younger than `data.provider_refresh_hours` (20 by default) is reused instead of re-fetched, each call is bounded by `data.provider_timeout_seconds` with one retry, and calls are paced by `data.provider_min_interval_seconds`. A source that does not answer becomes an issue and a `missing` entry for that security and that capability only — every other holding and output carries on. Every review reports a coverage table (what each capability was asked for, what answered, what is stale or missing, per security) and a requirements table saying what each output has and still needs.

Consensus figures are other people's expectations: they are labelled external estimates and are never treated as established outcomes, and news text is carried as third-party opinion, never as instruction. The generated company briefs and the EPS and DCF assumption sets are **proposals**: every figure names its source, the consequential assumptions are listed, and nothing reaches the reviewed workspace until you adopt it (a copied proposal is saved with `origin: prefill`). Statement publication dates are assumed when no filing date is available, adjusted prices can be revised after receipt, and fund disclosures are undated provider snapshots limited to the top holdings. No model provider is configured by default (`data.model_provider` is null); the null adapter summarises nothing and all arithmetic stays in Python. `uv run python scripts/provider_smoke.py AAPL XIC.TO --fund XIC.TO` runs the adapter live against public symbols and prints the coverage report.

### New candidates

A review can compare what you hold against securities you do not. Discovery is your decision, not a default: it runs once an account admits the screened universe (`mandate.account_candidate_policy: {account: "eligible_universe"}`) or `signals.watchlist` names symbols, and until then the review researches only the securities it already knows and asks no screen. When it does run, the fetching stage pages one dated Yahoo equity screen (region `us`, largest intraday market capitalisations, `data.universe_acquire_size`, 1,500 by default), archives every page with its receipt, and keeps the largest `data.universe_size` (1,000) issuers that qualify. Qualification establishes identity only — company equity, a US listing quoted in USD, ordinary common stock rather than a preferred, warrant, unit or depositary line, a US-domiciled issuer, and one representative class per issuer — so a kept row is a row worth researching, never a row already found investable: price, liquidity and the composite still have to be earned from the enriched data. Paging is bounded by the review's own arithmetic rather than by the provider's goodwill: it stops at the pages the requested size needs, stops when a page names nothing new, and stops when you cancel or the time budget runs out, saying so instead of presenting a short screen as the whole one. Each review states the scope it actually used and how much of it was researched, for example *Largest 1,000 eligible US ordinary common issuers by Yahoo intraday market cap on 2026-09-11, plus 3 watchlist symbols; 812 of 1,000 enriched*, and the acquisition says in its own words that it is a candidate set and not proof of a complete universe.

Nothing is dropped silently. Every acquired row that does not become a candidate is listed with its reasons — `not_ordinary_common`, `depositary_receipt`, `non_us_listing`, `currency_mismatch`, `duplicate_share_class` (naming the representative kept instead), `issuer_cap_inconsistent`, `separate_sector_model_required` for the sectors the review models separately, `beyond_size_limit`, or — when a bound or a cancellation stopped the pass before the issuer's identity was ever asked about — `identity_limit_reached` and `identity_not_attempted`, which say that the check did not run rather than that the issuer failed it. A watchlisted symbol the screen never returned is recorded as `watchlist_not_acquired`, so a name you asked for by hand is never passed over in silence. Watchlisted symbols and securities you already hold are kept past the size limit and past a separately modelled sector, because you asked for them by name; they are not kept past identity.

Candidates are identified and enriched in one bounded pass with the holdings: watchlist first, then the largest issuers, up to `data.candidate_enrichment_limit` (1,000) and within one `data.provider_time_budget_seconds` (1,800) budget shared by both halves of the fetch, and the pass stops when you cancel the operation — a cancellation once seen is never forgotten. News and filings are fetched, and a brief and its proposals are built, for the holdings and the largest `signals.candidate_brief_limit` (20) candidates only; the remaining candidates report their coverage, which is all the prioritisation and the signals read. Whatever a bound leaves out is named: the coverage report's `universe` entry states how many issuers were acquired, qualified, in scope and enriched, lists the ones that were not enriched, and says whether the time budget ran out or the fetch was stopped — a narrower enriched set is reported as a narrower set, never presented as the whole scope. Scenario returns come from your editable shared state (`allocation.shared_state`), one joint set covering every security at one date and one horizon. An imported forecast for that horizon, dated as of this review, always wins and is never regenerated; an import of another vintage or another horizon is superseded and each superseded security is named. The states are US equity market states, so they are stated for equities and for the mandate's benchmark; a bond fund, a commodity trust or an unresolved instrument gets no scenario from them and is named instead, and a view on one of those is yours to state in `allocation.shared_state.asset_overrides`. `uv run python scripts/provider_smoke.py --universe --acquire-size 250` measures this live without enriching anything.

The analytical layer reads the holdings database without changing it. A separate `data/research.sqlite3` contains jobs, versioned inputs, assumptions, runs, decisions and evaluations. Read-only JSON/HTML exports exclude internal paths, source SQL and credentials; their portfolio contents are still private financial information.

Working notes — operations, feature map, source schema, methodology register and verification record — stay in the git-ignored `docs/` directory and are not part of a checkout; the essentials are in this file. `uv run python -m portfolio_research --help` lists the operations the UI does not expose (`run`, `replay`, `evaluate`, `evaluate-ledgers`, `export`, `migrate`, `inspect`, `init`, `forecast-template`, `supplemental-template`), each with its own `--help`.

## Recovery

**Stop and restart.** Ctrl-C the terminal and run `uv run app.py` again; the data directory and the pairing key are retained. On start the application recovers interrupted work: an operation whose owning process is gone is marked failed with *Interrupted by application restart; start a new review*, an operation still owned by a live process is left alone, and the last published review loads unchanged either way. Reload the extension after extension code changes; restart the server after Python changes.

**Cancel and retry.** **Cancel** beside **Update & analyze** stops the operation before it publishes, and nothing is written. The stage strip then names the stage that stopped and what it could not do; press **Update & analyze** again to start a fresh operation — pressing it while one is still running rejoins that one instead. A single portfolio whose last pull failed shows **Retry** in place of **Pull** on Holdings, which re-pulls that portfolio alone.

**Exceptions.** The **Exceptions** panel answers one question at a time. A listing or value-currency answer is saved as dated supplemental evidence and closes immediately. A stale or missing FX observation is not answered by hand: run **Update & analyze**, or **Data → Refresh inputs & run review**, and the exception clears once a dated observation arrives. Exceptions that name supplemental inputs are answered on **Data → Supplement account facts**.

**Provider cache.** Archived provider responses live beside the research database, one directory per provider — `data/cache/<provider>/` for the default `data/research.sqlite3` — each payload stored with its SHA-256 digest and a receipt sidecar. An answer younger than `data.provider_refresh_hours` (20 by default) is reused instead of re-fetched; deleting a provider's directory forces the next review to fetch it again, losing the archived receipts for it.

**Backup and restore.** Both directories must be new; neither command overwrites anything.

```sh
uv run python -m portfolio_research backup --out backups/2026-09
uv run python -m portfolio_research restore --from backups/2026-09 --into data/restored
```

A backup copies both SQLite databases through the SQLite backup API (safe on a live WAL database), the resolved configuration, the `inputs/` and `cache/` directories, the `.chrome-connector-key` pairing key, and a manifest of SHA-256 digests. Restore verifies every digest before writing anything, refuses an existing directory, and rewrites the configuration paths for the new directory — launch it with `uv run app.py --data-dir data/restored`, which picks up the restored `research-config.json`. A backup therefore holds your holdings and your pairing key: keep it under the ignored `backups/` and never publish one.

## Development

Python 3.12+ runs the standard-library HTTP service and numerical core. JavaScript has no production runtime dependencies. Node.js 20+ and npm are used for tests; jsdom is a development dependency for DOM/API integration checks.

```sh
uv sync --locked
npm ci
uv run ruff check .
uv run ruff format --check .
uv run python -m unittest discover -s tests -q
node --test tests/*.test.mjs
```

- `portfolio/`, `chrome-extension/`: Yahoo collection and persistence.
- `portfolio_research/`: collector adapter, evidence/valuation, calendar, application services, public schemas and operations.
- `portfolio_lab/`: audited numerical modules; the import namespace is retained for compatibility.
- `static/`: one application shell and per-tab draft state.
- `tests/`: synthetic collector, numerical, valuation, persistence and UI regressions.

The app binds to loopback. Same-origin mutations require a local token; the extension has a separate revocable pairing key. This is a personal local service, not remote-production authentication.

## Before publishing

Everything this application collects is private financial information, and so is anything derived from it. Run this check before every push:

1. `git status --ignored` — confirm that `data/`, `private/`, `cache/`, `reports/`, `backups/`, `exports/`, `.chrome-connector-key` and every `*.sqlite*` are listed as ignored, not as untracked candidates.
2. `git status` — nothing personal is staged: no holdings CSV or JSON, no saved review export, no screenshot of real accounts, no local absolute path.
3. Never force-add an ignored artifact. `git add -f` is what turns an ignored directory into published data.
4. Fixtures and demo datasets stay synthetic. The demo command generates its own dataset; no real account, symbol list or balance belongs in `tests/` or `examples/`.
5. Provider credentials live in the environment (`FRED_API_KEY`) or in the ignored configuration (`data.sec_user_agent`), never in tracked source, and neither is echoed back by the UI or included in an export.

Exports exclude internal paths, source SQL and credentials, but their portfolio contents are still yours: treat a generated JSON or HTML report as private regardless of where it was written.
