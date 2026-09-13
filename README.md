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

Use **Data** to reconcile dated account NAV/cash, currency and security identity. Import missing facts and scenarios through validated forms/CSV/JSON. Use **Settings** to supply your actual mandate, account permissions and explicit sleeve allocation.

Reporting and combined totals default to USD. Original amount currencies are preserved; automatic FX conversion is not yet implemented, so mixed or unknown value currencies still require verified conversion before aggregation.

Then **Run monthly review** creates an immutable record. **Research** contains the company workspace; **Scenarios → Recalculate** uses frozen inputs; **Save run** archives a new child. **Review** contains full candidate baskets, funding, no-action rationale, run comparisons, exports and outcome evaluation.

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
