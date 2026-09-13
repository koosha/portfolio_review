# Portfolio Review — Codex integration handoff

Integrate the supplied `portfolio_lab.zip` into [`koosha/portfolio_review`](https://github.com/koosha/portfolio_review), preserving the existing Yahoo Finance portfolio ingestion and incorporating its existing UI workflows into one coherent analytical application. The product name is **Portfolio Review**.

This document is an implementation brief for Codex. It authorizes the implementation work described here when supplied to Codex in the project; it does not mean that the integration has already been performed.

## 1. How to use this handoff

1. Open the local `portfolio_review` checkout containing the existing Yahoo integration and UI in Codex.
2. Place this file in the project root as `CODEX_HANDOFF_README.md`. Make `portfolio_lab.zip` available locally; an ignored `reference/` directory or a sibling directory is suitable.
3. Give Codex the following instruction:

> Read `CODEX_HANDOFF_README.md` completely and execute its implementation plan. Inspect the existing repository, its applicable instructions, the actual SQLite schema, and `portfolio_lab.zip` before making architectural decisions. Integrate the research methodology, missing valuation capabilities, analysis pipeline, and dashboard into one application named Portfolio Review. Preserve working Yahoo ingestion and existing user workflows. Complete the implementation, meaningful tests, browser verification, and operating documentation; do not stop at a plan. Treat the ZIP as a reference implementation to audit and adapt. Follow the methodology and acceptance requirements in the handoff where the reference code is incomplete. Make routine engineering choices autonomously, keep a short progress record, and identify concrete blockers without inventing data or investment preferences.

### What was actually inspected

| Item | Observed state on September 13, 2026 |
| --- | --- |
| GitHub repository | Public; the contents API returned “This repository is empty.” The branches API returned an empty list. No existing application code, UI framework, or database schema could be inspected remotely. |
| Existing Yahoo integration and UI | Described by the owner; their local implementation remains the discovery baseline. Do not infer that this code is absent locally because GitHub is empty. |
| Reference archive | `portfolio_lab.zip`; 3,347,918 bytes; 73 entries under `portfolio_lab_project/`. |
| Archive SHA-256 | `0bb21012ff3b5335bbbabd41b96ec402a07d5a17c184c3a0e0db62e49963b2e7` |
| Reference backend | Python 3.11+, pandas, NumPy, SciPy, scikit-learn; SQLite input and separate research store. |
| Reference UI | Plain HTML/CSS/JavaScript, served by a local Python HTTP server. |
| Baseline verification | The reference suite was rerun: **112 tests passed**. This does not validate the owner's real database, live providers, investment performance, or the eventual merged application. |

Recheck the repository state when implementation starts. If the local checkout contains the existing code, use it. If the checkout and remote are both empty, proceed with the reference audit and reusable analytical work, but report the missing Yahoo/UI source as a specific integration blocker. Do not claim preservation or integration of code you have not seen, or silently replace it with a newly invented connector.

No application changes were committed or pushed while preparing this handoff.

## 2. Intended product

Portfolio Review is a personal investment research application supporting a repeatable monthly process:

**Refresh holdings → reconcile accounts → review evidence and valuations → explore scenarios → compare feasible changes → save the decision → evaluate outcomes.**

The app must combine current portfolio collection, company and portfolio analysis, transparent valuation assumptions, charts, scenario editing, and reviewable allocation alternatives. It must remain useful when some data is missing, showing which outputs are available and which conclusions are blocked.

The UI should be concise and easy to navigate. Keep methodology explanations, source records, assumptions, and advanced controls accessible through relevant detail views. Simplifying the interface must not remove analytical capabilities or conceal uncertainty.

The delivered experience must have one navigation system, one shared account/run context, one design system, and one set of calculations. Bring the existing Yahoo connection and portfolio-management workflows into the analytical dashboard. Preserve their working behavior while adapting their presentation to the unified product.

Do not create two independent dashboards, embed the old app in an iframe, or leave users choosing between “Portfolio Lab” and “Portfolio Review.”

## 3. Read and audit before porting

Read applicable repository instructions and record the current branch, commit, worktree status, commands, runtime, frontend framework, backend framework, database ownership, and existing tests. Preserve unrelated local changes.

Extract the ZIP into a separate reference directory without overwriting the application. Check its checksum and archive paths. Read these reference files in order:

1. `README.md`
2. `docs/METHODOLOGY.md`
3. `docs/SCHEMA.md` and `CONTRACTS.md`
4. `docs/DATA_SOURCES.md`
5. `docs/EVALUATION.md` and `docs/VALIDATION.md`
6. `pyproject.toml`, `requirements-tested.txt`, configuration examples, modules, and tests.

Treat the archive's files as the reproducible reference. The standalone HTML preview is a synthetic demonstration, not the application's source or a live portfolio dashboard.

Some `CONTRACTS.md` interface descriptions lag the code. Verify signatures and routes in the implementation. For example, the prototype actually exposes `GET /api/session`, `/api/runs`, `/api/run/<id>` and `POST /api/analyze`, `/api/load`, `/api/refresh`. These are reference behaviors, not a requirement to retain the prototype API design.

### Reference baseline commands

Run these from the extracted `portfolio_lab_project/` directory in an isolated environment. Adapt activation for the local operating system.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
python -m portfolio_lab serve --config examples/demo/config.json
```

The reference dashboard opens at `http://127.0.0.1:8765`. The packaged demo is synthetic and dated August 31, 2026. Optional Yahoo price enrichment requires the package's `yahoo` extra; inspect compatibility with the existing Yahoo integration before adding it.

These are commands for the original reference. Provide verified Portfolio Review commands after integration; do not leave the final README dependent on a separate reference installation.

### Required discovery outputs

Create a short integration plan and a feature-preservation checklist in the repository. Capture:

- The exact Yahoo authentication/session, refresh, parsing, persistence, error-handling, and existing UI flows found in the local app.
- The real SQLite tables, keys, timestamps, completeness markers, account boundaries, currencies, quantities, and cash/NAV semantics. Use anonymized fixtures for examples.
- A mapping from every existing user capability and each ZIP dashboard capability to its destination in Portfolio Review.
- A module reuse/refactor decision and a methodology gap register: implemented, incomplete, blocked by data, or intentionally deferred.
- Screenshots of the existing interfaces where available and the proposed navigation/component structure.

Resolve normal engineering choices and proceed. If required data is missing, implement the relevant import/configuration path and a useful missing-data state. Ask only when an unavailable source or user decision prevents further meaningful progress.

## 4. Architecture and integration boundaries

Prefer a modular monolith with a Python analytical core. Adapt to the existing application's stack after inspecting it. Reuse a maintainable existing frontend and its components; add a Python service boundary only if needed. If no frontend architecture exists, select a modest component-based approach appropriate to a personal app and document the tradeoff. A framework migration must solve a concrete problem.

Use the existing ingestion system as the owner of holdings collection. Research code consumes its completed snapshots through an adapter. Live providers enrich analysis independently. Monthly batch jobs and interactive what-ifs call the same application services.

| Boundary | Responsibility |
| --- | --- |
| Existing Yahoo ingestion | Authentication/session handling, collection, validated import, complete snapshot publication. Preserve existing behavior. |
| Portfolio adapter and identity | Read-only snapshot access, schema mapping, stable security/issuer IDs, explicit resolution issues. |
| Provider adapters | Prices, SEC statements, macro vintages, fund holdings; retries, limits, caching, timestamps, provenance. |
| Domain analytics | Deterministic reconciliation, QVM, valuation, scenarios, exposure, risk, constraints, costs, and conditional taxes. |
| Application services | Refresh orchestration, analysis jobs, preview, save, replay, run comparison, exports, outcome evaluation. |
| Research persistence | Versioned normalized inputs, assumptions, source manifests, immutable runs, forecasts, decisions, evaluations. |
| Presentation | Typed API/view models, common controls and charts, navigation, accessible interaction, concise explanations. |

Do not place SQL, provider calls, financial formulas, or portfolio optimization in UI components. Do not calculate the same metric separately in the browser and Python. Centralize units, missing states, rounding, currency display, and error contracts.

### Reference module migration map

Paths below are relative to `portfolio_lab_project/portfolio_lab/`.

| Reference source | Reuse/adapt into | Required review |
| --- | --- | --- |
| `ingestion.py` | Portfolio read adapter and research repository | `inspect_database`, `load_portfolio`, `ResearchStore`; actual schema, coherent snapshots, migrations, source immutability. |
| `providers.py` | Provider interfaces and data cache | SEC tags/periods, Yahoo adjustment conventions, FRED vintages, publication/receipt evidence, failure isolation. |
| `metrics.py` | Reconciliation, screens, exposures, risk and joint scenario services | Separate responsibilities while preserving tested calculations; retain coverage and missing-data flags. |
| `allocation.py` | Feasible candidate comparison | Default to simple alternatives; advanced optimizer is experimental and initially disabled. Audit sleeve membership, thresholds, constraints, rounding. |
| `taxes.py` | Optional account-specific tax estimate service | Conditional US gain-tax reserve only; not a complete tax engine. |
| `analytics.py`, `pipeline.py` | Application orchestration and result/export contracts | `run_analysis`, `replay_analysis`; separate data refresh from frozen-input recalculation. |
| `evaluation.py` | Prospective forecast evaluation | Preserve maturity/leakage gates; extend comparator ledgers separately. |
| `config.py`, `cli.py` | Validated settings and operational entry points | Version/migrate configuration; preserve a usable CLI alongside the UI. |
| `web.py`, `static/` | UI behavior reference and selective component reuse | Replace shared mutable session state as necessary; integrate into the chosen app shell. |
| `tests/` | Regression baseline | Keep meaningful properties; add integration and valuation tests that cover actual risks. |

Keep the `portfolio_lab` Python import namespace temporarily if that reduces migration risk. Change all user-facing branding immediately; rename package/CLI identifiers with compatibility aliases or a documented migration. Do not invalidate saved runs solely to rename the product.

### Storage, execution, and security

- The holdings source stays read-only to the analytical layer. Preserve SQLite read protections, query parameterization, and consistent read transactions. Keep the research database separate from the source, including symlink/path alias checks.
- Publish completed import batches atomically. A failed/partial refresh must not become the latest complete snapshot. Handle SQLite WAL/backups correctly; do not copy a live database file without its transactional state.
- Persist monetary amounts, quantities, and lot basis with explicit decimal precision. The ZIP uses floats for much of its computation; it is not a complete decimal ledger. Convert deliberately to floating-point arrays for numerical analytics, then validate monetary reconciliation and rounding at the boundary.
- Persist opaque stable IDs; tickers are dated aliases. Maintain source IDs, hashes, locators, units, currencies, publication and receipt timestamps, code/method/config versions, dependency versions, and parent-run relationships.
- Saved runs and forecasts are immutable through application operations. Migrations preserve prior records; changed assumptions create new versions. Backups and restore instructions are required.
- Long analyses run as tracked jobs outside UI request handling, with status, clear failures, and duplicate-submission protection. Multiple tabs or previews must not overwrite each other's base run or draft. A local process/job store is sufficient if reliable; do not add distributed infrastructure without need.
- Preserve the reference's local-only boundary unless a different deployment is explicitly in scope. Its stdlib server/session token is not production authentication. Any remote deployment requires an appropriate authenticated application boundary before exposure.
- The target repository was public when inspected. Commit only code, documentation, safe configuration templates, and synthetic/anonymized fixtures. Ignore private databases, exports, API keys, Yahoo cookies/session state, raw portfolio caches, logs, and generated personal reports. Redact secrets from saved configs and diagnostics.
- Define explicit public response/export schemas. The prototype returns complete archived result objects containing `saved_config`; filtering editable controls does not remove private paths or source SQL nested there. Keep full reproducibility records server-side and deliberately select safe fields for UI responses and shareable exports.

## 5. Data adapter and monthly workflow

Implement an explicit mapping to the owner's actual schema; do not require destructive table renaming. A `column_map` or adapter implementation is acceptable. Document mappings and which fields remain unknown.

| Canonical record | Required semantics |
| --- | --- |
| Position snapshot | Account/security IDs, valuation timestamp/date, quantity and price where known, market value, currency, source, raw identifier; reported weight retained separately. |
| Account snapshot | Account ID/type, dated NAV including cash, explicit cash, currency, completeness, account restrictions and tax jurisdiction where known. |
| Security/issuer | Stable IDs, ticker aliases and effective dates, instrument/class/venue/currency, resolution status; dated eligibility, sector, and issuer-wide common-equity market cap. |
| Tax lot | Account/security/lot IDs, quantity still open at the snapshot, acquisition date, adjusted basis and currency. Aggregate basis is insufficient. |
| Observation/source | Field/value, units, fiscal period, public availability, system receipt, source/locator, version and missing reason. |
| Forecast/decision | Run, horizon, assumptions, scenario values, probability basis, sources, assessment/review status, decision or no-action rationale, later override record. |

The reference does not automatically acquire a complete research universe or accurate cash, account types, lots, and permissions from Yahoo. If the existing importer lacks a field, add a validated supplemental import or manual configuration mechanism. Unknown cash is not zero and the unexplained NAV residual is not cash.

Choose a common completed snapshot, not the newest row independently for each security. Account cash and NAV must correspond to that snapshot. Keep sold positions from reappearing through stale rows. Represent stale/partial data explicitly and use a prior coherent snapshot only with a visible date/status.

The monthly command/job must:

1. Coordinate with the existing importer and select a complete snapshot.
2. Set the intended month-end decision cutoff using the relevant trading calendar and America/New_York timezone; record valuation, decision, and generation times separately.
3. Refresh configured providers, preserving raw records where permitted and normalizing data with provenance.
4. Reconcile coverage and accounts before calculating affected portfolio conclusions.
5. Calculate available screens, valuation scenarios, exposures, risk, and candidate comparisons.
6. Save inputs, assumptions, issues, outputs, source/code/config versions, and exports as one traceable run.
7. Evaluate eligible matured forecasts and comparator records without rewriting their originals.

One-off what-if calculations reuse frozen inputs and do not call providers. “Refresh holdings,” “Run monthly review,” and “Recalculate scenario” must be distinct operations. Scheduling should be configurable and idempotent; document installation rather than silently installing an operating-system task. Preserve a manual run option.

## 6. Methodology that must survive the integration

The ZIP implements a subset of the earlier research design. Its current behavior is the baseline to test, not a reason to preserve analytical shortcuts. Apply the requirements below, record material deviations, and version changes. Do not claim predictive skill from passing software tests.

### Reconciliation, identity, and timing

- Preserve coverage and account boundaries. Never normalize an incomplete portfolio to 100% or finance one account's purchases from another account's sale proceeds without a verified permitted transfer.
- Keep separate share classes as separate securities and aggregate them for issuer exposure. Unknown plan funds remain unresolved; do not map one to a retail fund using name similarity.
- Separate coverage of everything owned from eligibility for the systematic company screen. Funds, banks, insurers, REITs, foreign reporters, and other instruments need appropriate treatment; unavailable company scores do not remove them from portfolio coverage.
- Historical facts must have been public by the cutoff. Strict live-system replay additionally requires actual receipt by the decision generation time. Never substitute period-end or file-generation dates for missing availability or valuation dates.
- Retain original filing/restatement versions. Construct comparable TTM flows from nonoverlapping quarters or validated equivalent arithmetic. Date-only SEC/FRED observations do not establish exact intraday availability.
- Implement a trading-calendar policy. The research convention uses the final regular US session of the month and a 16:00 America/New_York information cutoff. Explicitly distinguish that cutoff from actual market close on early-close sessions; do not permit a price or fact to enter a simulated trade before availability. Baseline simulated execution is no earlier than the next eligible session's close.

### Quality, value, and momentum

Initial screen: the largest 1,000 eligible US-domiciled ordinary-common-equity issuers by contemporaneous market cap, price at least $5, and prior 60-session median daily dollar volume at least $10 million. These are provisional research settings. The ZIP screens its supplied universe; it does not prove market-wide or historical completeness. Never label rankings over only current holdings as a market-wide screen.

Choose one representative eligible share class per issuer using liquidity. Use issuer-wide common-equity market value for valuation yields. Require appropriate history and explicit eligibility/accounting checks.

| Family | Initial definition |
| --- | --- |
| Quality | Average favorable percentile of gross profit / beginning assets, operating income / average assets, and (operating cash flow − net income) / average assets. Flows are TTM. |
| Value | Average favorable percentile of earnings attributable to common shareholders / issuer common-equity value and (operating cash flow − positive capex) / issuer common-equity value. |
| Momentum | Total-return index at month `t−1` divided by month `t−12`, minus one: 11 monthly intervals, excluding the latest month. |
| Composite | Equal-weight average of the three families initially; a relative research score, never a return forecast. |

Winsorize at contemporaneous 2.5th/97.5th percentiles and use average ranks for ties. Quality/value ranks are sector-relative with at least 20 usable issuers per relevant metric; momentum ranks span the eligible universe. Require two quality inputs, both value inputs, and momentum. Missing families mean no composite, not automatic weight redistribution. Negative earnings/cash flow with valid positive denominators remain adverse yields; invalid denominators yield missing metrics.

The reference uses `net_income` for earnings yield. Audit whether a provider supplies income attributable to common shareholders; do not assume consolidated net income is interchangeable. Record accounting definitions and reject incompatible comparisons.

Changing screen weights is an explicit research variation and must not silently retune a saved production methodology. Related ratios remain within one family rather than accumulating independent votes. Balance-sheet underwriting checks remain separate from additive alpha scoring.

### Valuation and company research: required new work

The ZIP has valuation **yields** and editable asset-return scenarios. It does **not** implement an EPS/multiple price bridge, DCF/reverse DCF, or a complete company-thesis workflow. Build the following layer as part of this integration; do not deliver only a renamed version of the ZIP.

1. **Versioned company assessment:** dated source-backed facts, market expectations, operating assumptions, thesis/counter-thesis, catalysts within the horizon, material balance-sheet risks, invalidation conditions, and review status. Manual evidence entry/import is acceptable; automated extraction is optional.
2. **EPS/multiple model:** for adverse, central, and favorable outcomes, compute `horizon price = horizon EPS × matching P/E`; then `horizon total return = (horizon price + distributions per starting share) / starting price − 1`. Explicitly label trailing versus forward EPS and matching multiple convention. Model dilution/buybacks through shares/EPS; do not add buyback yield again.
3. **DCF for appropriate nonfinancial companies:** compute `FCFF = EBIT × (1 − tax rate) + D&A − capex − change in operating working capital`. Connect growth to reinvestment/returns on capital, model discounting and terminal value explicitly, bridge EV to common equity using debt, preferred claims, NCI, excess cash, and separately valued assets, then divide by an explicit diluted share count. Address stock compensation consistently. Reject invalid terminal-growth/discount-rate combinations. Preserve null outputs when essential inputs are unavailable.
4. **Sensitivity and expectations:** provide useful driver sensitivities, such as EPS versus P/E and discount rate versus terminal growth. A reverse-valuation view may be added once the forward model is correct; identify non-unique/impossible solutions. Do not delay the essential forward valuation for this extension.
5. **Scenario linkage:** show where company assumptions produce scenario returns and allow a reviewed scenario set to feed portfolio comparison. DCF intrinsic value is conditional on assumptions; it is not automatically a 12-month price target. Any convergence/horizon-return assumption must be explicit.

Manual return overrides remain available and clearly marked. Store assumptions with units, horizon, source or subjective basis, author/origin, and version. Editing a previously calibrated forecast creates a subjective draft unless it passes an actual supported calibration process.

### Scenarios, risk, and forecast integrity

- Keep relative score, valuation estimate, scenario return, and calibrated expected return as separate fields and UI concepts.
- Support 6-, 12-, and 18-month horizons, with 12 months the provisional primary horizon. Probabilities are optional for scenario exploration. Probability-dependent means or optimization remain unavailable until a consistent distribution is supplied.
- Joint portfolio states must be coherent across assets and benchmark. Validate common labels and probabilities; do not combine unrelated company scenarios as a single macro state. Do not silently normalize invalid probabilities.
- Changing horizon invalidates incompatible return overrides/priors. Cash return and distribution/reinvestment conventions must be explicit and consistently applied.
- A free-text calibration ID alone is not validation. Require a real calibration record, compatible horizon and sample, matured outcomes, diagnostics, and method version before allowing a calibrated label to unlock allocation logic. No calibrated return is better than invented precision.
- Macro data initially informs context and stress assumptions. Do not infer reliable equity returns or market timing mechanically from macro levels.
- Aggregate direct holdings and dated fund look-through by issuer, without double-counting wrappers. Partial fund holdings leave an unknown residual. Sector totals are additive; overlapping theme tags are not.
- Risk uses three years of aligned weekly total returns, at least 104 common observations, documented covariance shrinkage, and annualization by 52. Include a five-year sensitivity where feasible. Missing-history assets need an explicit proxy or incomplete-risk status; avoid incompatible pairwise covariance panels.
- Display volatility, beta, tracking error, concentration, risk contributions where supported, and historical/mechanical/joint scenario losses with their estimation scope. Separate a constant-current-weight historical illustration from actual personal portfolio performance.

### Candidate allocation, taxes, and decisions

Default to transparent alternatives: no discretionary change, approved-benchmark treatment of new flows, minimal constraint repair, and a simple systematic sleeve. A raw benchmark return is a reference; a benchmark allocation alternative must actually obey account eligibility, funding, costs, and mandate constraints.

The supplied optimizer defaults on. Change the real-use default to off and expose it only as an advanced experiment. Do not turn scores into forecasts. Scenario-based optimization can be explored under explicit assumptions and remains a candidate for review. Calibrated expected-return optimization requires validated forecasts/priors and a separately demonstrated benefit.

- Persist explicit systematic sleeve membership and budget, separate from legacy holdings. Retain eligible incumbents in the top 40%; cap at 20; fill vacancies from the top 20%, ordered by score. Break ties by lower turnover then stable issuer ID. Apply combined portfolio limits, not just sleeve limits. Use a configured eligible residual instrument or return infeasibility.
- Specify whether the approved sleeve budget is a fraction of account or household NAV. The ZIP instead applies its percentage to unlocked invested assets and treats currently owned names as incumbents. Replace these shortcuts with the declared budget and explicit membership records.
- Leave unconfirmed mandate fields null and real-use `confirmed=false`. Synthetic demo preferences must never become the owner's settings. Benchmark scope, caps, liquidity, loss tolerance, account permissions, tax treatment, and trading restrictions require actual configuration before affected proposals can be ready.
- Reconcile each account: ending cash equals starting cash plus permitted flows and sales, less purchases, costs, and tax reserve. Never silently relax a constraint to force a solution.
- Preserve locks and an explicit transition policy for legacy breaches. An untradeable breach may make a proposed repair infeasible; explain it.
- Test trading costs at 5/10/25 basis points per side, with 10 provisional. Model instrument-specific dealing and fractional-share permissions. After rounding or applying trade bands, recheck the complete basket's funding, taxes, and exposures.
- Initial experimental bands: minimum trade is the greater of $500 or 0.25% of account NAV; incumbent weight gap is the greater of 0.5 percentage points of account NAV or 25% of target weight. Hard constraints override convenience bands. The ZIP flags small legs; it does not fully implement this policy. Do not drop individual legs and leave an unfunded basket.
- For return-seeking substitution, the provisional 2% improvement hurdle is after incremental costs/taxes over 12 months **per dollar redeployed**. It is not two percentage points of whole-portfolio return. Test 1/2/4% sensitivities. In subjective mode it is a review flag, not an investment-performance claim.
- The reference tax module reserves for positive US realized gains under supplied rates/lots. It does not resolve wash sales, cross-household activity, foreign tax rules, loss credits, or final liability. Missing lots/treatment must block tax-sensitive readiness; a separately labeled hypothetical comparison may still be useful. Do not claim “tax optimized.”
- Always retain no-action reasons. Distinguish draft, blocked, infeasible, and review-ready candidates. No brokerage execution, leverage, shorts, or derivatives is included.

### Prospective evaluation

Maintain separate benchmark, simple feasible policy, no-discretionary-change, and model-versus-actual decision ledgers. The no-change baseline begins with the first complete snapshot and preserves units, corporate actions, distributions, and the declared flow policy. It is not reconstructed personal history.

All comparator arms use consistent external flows, timing, cash treatment, costs, and valid tax conventions. Contributions are not investment gains. After-tax outcomes remain unavailable without the necessary data; recurring subscription costs count once.

Evaluate forecasts only when their actual horizon/execution endpoints and required latency have matured. Preserve frozen sources, assumptions, prompts/model versions where used, outputs, and overrides. For allocation-method tests, hold the selected securities fixed to distinguish sizing from selection.

Full historical validation is a separate gated extension: point-in-time universe/facts, delistings, corporate actions, chronological folds, purged overlapping labels, dependence-aware inference, preregistered comparisons, and a locked holdout. The reference's forecast evaluator and purged-split helper are not a complete investment backtest. Retrospective LLM judgments cannot establish unbiased historical alpha.

Operational readiness, software correctness, forecast calibration, and investment outperformance are different claims. Three successful monthly cycles support operational readiness; even a matured 12-month cohort provides only initial evidence. Keep the ability to conclude that the simpler allocation is preferable.

## 7. Unified UI specification

Use **Portfolio Review** in the app title, navigation, browser title, exported reports, empty states, help, and normal CLI output. Preserve historical provenance naming where necessary.

### Navigation and feature placement

Use five primary destinations with **Data** and **Settings** as utilities. Adapt labels only when a clearer mapping emerges from the existing app audit.

| Destination | Default view | Details available on demand |
| --- | --- | --- |
| Overview | NAV, explicit cash, coverage/freshness, principal exposures, changes since the prior comparable run, review priorities | Reconciliation, issuer/sector/fund overlap, risk components, stress assumptions and methodology. |
| Holdings | Existing portfolio/account selection and holdings workflows, searchable sortable table, direct versus issuer exposure | Security/account details, quantities, lots if available, source dates, unresolved identities. |
| Research | Company screen and selected-security valuation workspace | Raw/ranked QVM inputs, peer coverage, statements, EPS/multiple/DCF assumptions and sensitivities, thesis, citations and macro context. |
| Scenarios | Current versus edited joint scenarios, horizon selector, assumptions editor and result comparison | Company-return linkage, probabilities, stress controls, priors, advanced risk settings and rationale. |
| Review | Feasible alternatives and complete proposed basket with cash/cost/tax impact | Constraint/funding diagnostics, lots, no-action reasons, saved runs, overrides, run comparison, matured forecast evaluation and exports. |
| Data utility | Existing Yahoo connection/refresh flow, snapshot status, provider status and data issues | Account mapping, validated imports, source provenance and identity resolution. |
| Settings utility | Mandate, accounts/permissions, benchmark, data configuration and monthly scheduling | Versioned method settings, advanced validated configuration, diagnostics. Secrets stay server-side. |

Every existing workflow must appear in the feature-preservation checklist. This table is not permission to delete a workflow that the existing app contains but this inspection could not see.

### Design rules

- One shared toolbar/context shows account scope, selected saved run, valuation date, decision cutoff, and freshness. Use a compact summary with details rather than many persistent badges.
- Changing a display filter must not silently change the portfolio's optimization scope, household constraints, or denominators. Make any deliberate change to analytical scope explicit and save it in the run configuration.
- Default screens lead with conclusions and a few useful metrics. Use compact labels, restrained colors, aligned numbers, clear units, consistent spacing/type, and high-contrast focus states. Avoid introductory paragraphs and decorative dashboard copy.
- Move the ZIP's always-visible, lengthy assumptions sidebar into contextual panels/drawers. Keep the controls relevant to the current task nearby. Provide labeled forms for common and consequential settings; retain validated advanced JSON for expert use without making it the only route to essential functions.
- Show a resolved settings/diff view when advanced configuration overrides visible controls. Users must be able to see which values a calculation actually used.
- Open holding/security details consistently from any relevant table or chart. Preserve selected scope and filters when navigating. Support useful sorting, filtering, column selection and exports without crowding the default table.
- Use line charts for trends, bars for exposure/comparison, heatmaps for valuation sensitivities, and tables for exact candidate/funding numbers. Every chart must identify units, timeframe, scope, coverage, and whether its data is historical, hypothetical, or synthetic.
- Missing numeric values display as unavailable with an inspectable reason, never as zero or an empty successful chart. Hide or disable unsupported actions with a concise reason and a way to resolve it.
- Make demo/live/offline modes unmistakable. Never fall back to synthetic data after a live-provider failure.
- Support a desktop research workspace and usable narrow layouts. Tables may scroll within their container; primary controls must remain reachable. Verify keyboard navigation, focus management, input labels, dialogs, status announcements, and color-independent chart/table meaning.

### State and interaction requirements

Implement explicit states for loading, empty, ready, stale, partial, error, draft/dirty, calculating, saved, blocked, and infeasible where relevant.

Editing assumptions immediately marks existing results as out of date until recalculation succeeds. Retain dirty drafts safely when switching runs or refreshing inputs; use explicit save/discard/retain choices inside the application when needed. Read-only exported dashboards must disable mutation controls with a clear reason.

“Recalculate” previews changed assumptions on frozen inputs. “Save run” creates a new immutable child. “Reset” restores the selected run's assumptions. “Refresh data” starts a new input refresh/review and must not silently consume or erase scenario drafts. Changing horizon prompts or safely handles incompatible drafts and clears incompatible numerical overrides.

Expose only a small set of primary actions per view. Disabling a button without explaining why is insufficient. Preserve the last usable result after a failed calculation while clearly identifying its age and the failed request. Associate results with their exact request/run so slow responses cannot overwrite newer edits.

The same candidate/run record must drive summary cards, charts, trade tables, exports, and saved history. Run comparisons show assumption and modeled-outcome differences; realized performance appears only in an appropriate separately labeled evaluation view.

## 8. Implementation sequence and completion gates

Work in reviewable increments, updating the feature map and gap register. Maintain an end-to-end usable path throughout. Do not stop after a cosmetic shell or leave core pages as placeholders.

| Phase | Deliverable | Completion gate |
| --- | --- | --- |
| 0. Discover | Repository/UI/schema audit, reference baseline, gap register, chosen architecture | Existing workflows and actual integration boundaries documented; missing source/data identified precisely. |
| 1. Connect | Preserved Yahoo flow, read adapter, completed snapshot selection, IDs, research store, migrations | Synthetic/anonymized real-schema fixture reconciles; source remains unchanged; incomplete data blocks affected conclusions. |
| 2. Analyze | Provider adapters, deterministic QVM/exposure/risk, new company evidence and valuation services | Traceable calculations and missing-data behavior pass meaningful tests; provider outages do not break all useful analysis. |
| 3. Unify UI | One Portfolio Review shell containing old workflows and all analytical views | Feature map fulfilled; valuations, assumptions and advanced detail accessible; real browser interaction and responsive review pass. |
| 4. Review | Joint scenarios, simple feasible alternatives, explicit sleeve state, conditional taxes, saved runs/replay | Complete candidate reconciles per account; draft/save/reset behavior works; original runs remain immutable. |
| 5. Operate | Monthly CLI/job, prospective records/evaluation, exports, setup/migration/backup docs | One-command synthetic demo and repeatable monthly flow work; live readiness accurately reported. |

Deliver the required analysis and valuation functionality even if advanced optimization or historical backtesting remains gated. When a live credential or owner's mandate is absent, finish the implementation and synthetic end-to-end path, then clearly identify the remaining live input. Do not hardcode private preferences to obtain a green status.

## 9. Acceptance checklist

Run tests proportionate to these concrete risks; do not rely on test counts or superficial snapshots. Record failures and unsupported live checks honestly.

### Existing behavior and integration

- [ ] Yahoo connect/session, collection, error recovery, persistence, and existing portfolio UI workflows continue to work or have documented equivalent behavior.
- [ ] Source schema is mapped rather than destructively rewritten; analytical runs leave the holdings database unchanged.
- [ ] New partial imports never masquerade as completed snapshots; sold positions do not return through per-symbol “latest” selection.
- [ ] Monthly jobs and UI invoke the same analytical services. Replay uses frozen inputs without network access.
- [ ] Fresh install, configuration/migrations, synthetic demo, and documented commands work outside the reference directory.

### Financial and data integrity

- [ ] A fixture with 87.55% coverage produces 12.45% unresolved residual, not cash. Missing cash differs from a valid zero balance.
- [ ] Unknown valuation dates stay unknown; generation dates do not fill them. Mixed currencies are blocked until a verified FX path exists.
- [ ] GOOG/GOOGL remain separate securities with combined Alphabet issuer exposure. Unresolved plan funds are not guessed from similar names.
- [ ] Partial fund holdings leave unknown exposure; wrappers are not counted again in issuer totals.
- [ ] Later restatements/after-cutoff observations cannot enter an earlier frozen assessment. TTM periods, common-share earnings, units and denominators are checked.
- [ ] QVM thresholds/ranks/missing-family rules and momentum endpoints match the versioned methodology; small-universe coverage is labeled accurately.
- [ ] An EPS scenario with starting price 100, horizon EPS 5.5, P/E 20 and distributions 1 yields price 110 and total return 11%. Buybacks/dividends are not counted twice.
- [ ] DCF present values, terminal-value validity, EV-to-equity bridge, diluted shares, currencies, sensitivities and missing inputs are tested with independently hand-calculated cases.
- [ ] Joint scenario labels/probabilities/horizons validate correctly. Unweighted scenario exploration works; missing probabilities cannot produce a weighted mean.
- [ ] Missing-history positions do not vanish from risk scope. Incomplete risk and proxy usage remain visible.
- [ ] Account funding, restrictions, costs/taxes, lot coverage, sleeve membership, no-trade bands and post-rounding feasibility are tested on realistic edge cases. Infeasibility returns reasons without relaxed constraints.
- [ ] The 2% substitution hurdle uses dollars redeployed and charges friction once. Missing taxes are not represented as zero tax drag.
- [ ] Unsupported extracted/LLM numbers and arbitrary calibration IDs cannot unlock financial conclusions. A saved scenario assumption remains distinguishable from a validated forecast.
- [ ] Unmatured/overlapping training labels are rejected; external flows are not gains; counterfactual and actual performance are distinct. Historical evaluation does not silently exclude failed/delisted holdings.
- [ ] Identical retained inputs, configuration, code and numerical environment reproduce deterministic results within declared tolerances; changed assumptions create a new forecast/run.

### UI, operations, and regression

- [ ] Portfolio Review branding is consistent; there is one navigation/context and no duplicate analytical implementation.
- [ ] All old and ZIP capabilities have a working destination; valuation and evidence detail are implemented, not just mocked.
- [ ] Changing assumptions, recalculating, saving, resetting, loading an older run, comparing runs and exporting work end to end.
- [ ] Summary cards, charts, candidate rows, funding totals and exports reconcile to the same run and candidate ledger.
- [ ] Empty/stale/partial/blocked/error/synthetic states are clear and usable. A failed provider never inserts demo data.
- [ ] Browser verification covers desktop and narrow layouts, keyboard use, forms/dialog focus, loading, long tables and request races. Capture representative screenshots and report any environment-blocked checks explicitly.
- [ ] Baseline reference regressions remain covered; existing app tests, backend tests, applicable frontend checks/build and meaningful end-to-end flows pass.
- [ ] Personal data and secrets are excluded from version control and exposed API/config payloads. Backup/restore and provider configuration instructions are usable.

## 10. Required implementation handback

At completion, provide:

1. A short explanation of the resulting architecture and how the existing Yahoo app and ZIP were integrated.
2. The finished feature-preservation map and methodology gap register, distinguishing implemented behavior from data-blocked or deferred capability.
3. Verified install, configuration, launch, monthly-run, replay, evaluation, export, migration and backup/restore commands.
4. Test/build/browser results with screenshots and clear live-provider/real-data limitations.
5. A concise list of owner inputs still needed, such as exact account cash/NAV, unresolved identifiers, credentials, lots, mandate fields, or restrictions.

Keep documentation practical. Product screens should explain the current decision and how to inspect it; implementation details belong in developer/operating documentation. The result should support a disciplined, flexible monthly review and remain honest when no change—or insufficient evidence—is the appropriate conclusion.
