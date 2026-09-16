# Portfolio Review

A local application for collecting Yahoo Finance holdings and conducting a repeatable monthly portfolio review. Your normal Yahoo/Google sign-in stays in Chrome. Holdings collection, company evidence, EPS/multiple and DCF valuations, scenarios, feasible alternatives, saved reviews, and prospective evaluation share one interface.

The application produces research records and conditional comparisons. It does not place orders. Missing data remains visible; synthetic demo preferences never become settings for real accounts.

## Start

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Chrome. From this checkout:

```sh
uv sync --locked
uv run app.py
```

Open [Portfolio Review](http://127.0.0.1:8765). Keep the terminal running. The existing `data/` database, portfolio selections and Chrome pairing key are retained. Only one server can own a data directory, and only one can listen on a port.

For an isolated, fully populated example:

```sh
uv run python -m portfolio_research demo --directory data/demo --serve
```

Open [the synthetic demo](http://127.0.0.1:8766). Use a new directory for each newly generated demo. The dataset and company assessments are explicitly synthetic.

## Connect Yahoo

1. In `chrome://extensions`, enable Developer mode and load this checkout's `chrome-extension` folder.
2. In the app's **Data → Connection settings**, copy the pairing key into the **Local Portfolio** extension and save it.
3. Open Yahoo and sign in normally. Use **Find portfolios**, select portfolios with checkboxes, then **Pull latest holdings**.
4. Review the saved holdings and capture details. Reload the extension after extension code changes; restart the server after Python changes.

Selections persist. Unchecked accounts retain history but are excluded from pulls and exports. A failed collection never publishes a new complete analytical batch. A successful pull without an independently verified row count is visibly unverified.

## Monthly review

One action runs the month. **Update & analyze** in the header collects the newest holdings through Chrome, resolves identity and currency, fetches the configured providers, analyzes and publishes one review. The stage strip under the header reports what each step did and what it could not do; **Cancel** stops the operation before it publishes. A duplicate click rejoins the operation already running, reloading the page rejoins it as well, and an operation that is cancelled or fails writes nothing — the last usable review stays exactly as it was.

1. **Update & analyze** — collect, resolve, fetch, analyze and publish as one operation.
2. **Answer the exceptions** — Overview lists only the questions the sources and provider metadata could not answer; each answer is saved as dated supplemental evidence.
3. **Read the results** — Overview, Holdings and Research show the saved review beside the newest collection, each with its own dates.
4. **Adjust the assumptions** — Research and Scenarios hold the company workspace and explicit assumptions; edits stay in a draft until they are calculated.
5. **Recalculate** — **Scenarios → Recalculate** re-runs the draft against that run's frozen inputs and **Save run** archives an immutable child run.
6. **Record the decision** — **Review** holds candidate baskets, funding, no-action rationale, run comparisons, exports and outcome evaluation.

**Data → Historical month-end review (advanced)** recalculates a past month end from saved and cached inputs; it is not part of the monthly flow. **Data → Provider health** shows what each source has already answered and the next step when it cannot, without displaying any credential.

**Overview** shows the newest collected holdings as soon as a collection finishes, with no saved review or mandate required. Each account lists its capture and server receipt times; a receipt time is not a quote time. Captured amounts are shown as observed, grouped by explicit value currency without FX conversion and never labeled NAV, and the source valuation time stays unknown unless dated account evidence attests it. The last completed analysis appears separately with its own market date, information cutoff and generation time, so current holdings and saved reviews never share a denominator.

Use **Data** to reconcile dated account NAV/cash, currency and security identity. Import missing facts and scenarios through validated forms/CSV/JSON. Use **Settings** to supply your actual mandate, account permissions and explicit sleeve allocation.

Reporting and combined totals default to USD. Listing identity and value currencies are established automatically from Yahoo quote symbols, provider listing metadata and dated FX observations (Bank of Canada Valet and Yahoo FX). Amounts are presented in USD beside the original amount and currency, each conversion names its FX pair and observation date, and an amount already reported in USD is never converted again. Overview lists only the genuine exceptions: an ambiguous or unknown listing, a value currency that the quantity × price arithmetic cannot establish, or a missing or stale FX observation. Listing and currency answers are saved as dated supplemental evidence; FX exceptions clear once refreshed market data supplies a dated observation. Holdings that cannot be presented in USD are counted as unconverted, never silently dropped. Reload the Local Portfolio Chrome extension (1.2.0 or newer) so collections capture Yahoo quote symbols.

The analytical layer reads the holdings database without changing it. A separate `data/research.sqlite3` contains jobs, versioned inputs, assumptions, runs, decisions and evaluations. Read-only JSON/HTML exports exclude internal paths, source SQL and credentials; their portfolio contents are still private financial information.

See [Operations](docs/OPERATIONS.md) for verified configuration, monthly commands, replay, evaluation, exports, backup/restore and migration. See the [feature map](docs/FEATURE_MAP.md), [source schema](docs/SOURCE_SCHEMA.md), [methodology register](docs/METHODOLOGY_GAPS.md) and [verification record](docs/VERIFICATION.md) for implementation scope and limitations.

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

The app binds to loopback. Same-origin mutations require a local token; the extension has a separate revocable pairing key. This is a personal local service, not remote-production authentication. `.gitignore` excludes personal data, keys, sessions, reports, caches and environments. Never force-add ignored artifacts or publish personal exports. Use the SQLite backup command, including for a running WAL database; copying one live database file is insufficient.
