# Integration verification — September 13, 2026

The implementation and automated operating path are complete. Two environment-dependent acceptance items remain incomplete: replacing the original live server and real Chrome visual/accessibility verification. They are not reported as passing.

## Verified software and operations

| Check | Result |
| --- | --- |
| Supplied reference reproducibility | All 72 MANIFEST_SHA256 entries matched; 112 original reference tests passed in an isolated environment. The extracted reference was supplied without a ZIP, so the ZIP-level checksum could not be rechecked. |
| Locked environment | `uv sync --locked` passed; Python 3.12+, committed numerical dependency pins and exchange-calendar lock. |
| Python regressions | `uv run python -m unittest discover -s tests -q`: **304 passed**, including inherited collector behavior, adapted reference tests, calendar, coherent batches, real-schema adapter, exact decimals, evidence/EPS/DCF, funding, immutable storage, job recovery, prospective timing, privacy and restore boundaries. |
| JavaScript regressions | `node --test tests/*.test.mjs`: **51 passed**, including inherited extension/collector tests, draft contracts and real-HTTP/in-memory-DOM integration. |
| JavaScript dependency install | `npm ci --ignore-scripts` passed with pinned jsdom 26.1.0; jsdom is a development-only dependency and supports Node 20+. No browser-control package was added. |
| Static checks | `uv run ruff check .`, `uv run ruff format --check .`, and `git diff --cached --check` passed. Reference regression tests are included in Ruff coverage. |
| Fresh synthetic workflow | `demo`, `run`, `replay`, `export`, `evaluate`, `forecast-template`, `backup`, `restore`, and `migrate` CLI paths passed outside the supplied reference directory. The restored archive retained its immutable run and normalized input files. |
| Integrated interface events | Real event listeners rendered the app against a disposable Python HTTP server. Tests cover navigation, valuation output, dirty → preview → save, unchanged parent run, older-run load/reset, delayed response/newer edit, server validation failure retention, and missing cash/probability rendering. |
| Public-source review | All staged source/docs/configuration and synthetic fixtures were reviewed. Private local paths, real holdings/account names, credentials, pairing material, API keys, generated reports and attribution trailers were absent. Dependency caches, databases, exports and private inputs remain ignored. |
| Source preservation | A consistent private SQLite backup was taken before the intended live migration. Read-only checks confirmed the existing source selections, snapshot identities/hashes and pairing key remained unchanged. A conservative unconfirmed offline configuration was prepared privately. |

Tests validate software properties, not calibration, outperformance, full market coverage or the accuracy of an owner's input statements. Exact forecast maturity and execution gates intentionally leave unavailable outcomes blank. Synthetic runs generated after a hypothetical historical execution date are labeled reconstructions and cannot become prospective evidence.

## Blocked acceptance items

| Item | Observed restriction | What remains |
| --- | --- | --- |
| Actual Chrome browser pass | Chrome displayed “127.0.0.1 is blocked” and `ERR_BLOCKED_BY_CLIENT` for the isolated synthetic app at `http://127.0.0.1:8766`. The server itself was reachable through HTTP. Browser protections were not changed. | Desktop/narrow visual review, representative application screenshots, native keyboard/dialog focus and browser download/security behavior. DOM tests are explicitly not a substitute for these checks. The owner must make the exact local page accessible in Chrome before browser verification can resume. |
| Switch the original app on port 8765 | Sending SIGINT to the positively identified old server returned `Operation not permitted`, including through the approved escalation path. No competing server was started on that port. | Stop the old server with Ctrl-C in its own Terminal, then run `uv run app.py` from the checkout. Startup will perform additive collector/research migrations and retain the existing selections and pairing key. Verify the new app before starting a fresh full selected-account pull. |
| Personal live analytical readiness | Existing collector rows do not establish all required valuation dates, currencies, independent NAV/cash, identities, lots and mandate permissions. No owner's preferences were inferred from the demo. | Complete snapshot-bound supplements, reviewed provider/evidence inputs and actual Settings. A fresh successful full pull is required to establish a new collection batch. |
| Live external providers | Fixture/cached/offline paths and failure isolation were exercised; no complete personal SEC/FRED/Yahoo enrichment run is claimed. | Supply chosen provider settings/credentials and verify their actual data coverage. Missing providers never fall back to synthetic data. |

The original server and source remain intact. The new Python service has been exercised on disposable synthetic data; its launch on the personal data directory is deliberately left to the coordinated switch above. No screenshot of a working browser-rendered application is claimed or fabricated.

## Remaining owner inputs

1. Independent account NAV and cash, currency attestations and actual valuation dates tied to the exact source snapshots.
2. Verified security/issuer mappings and dated aliases; qualified eligibility/capitalization/fund disclosures where those analyses are desired.
3. Benchmark, limits, locks, account/dealing permissions, explicit sleeve membership/budget and cash-return assumption.
4. Actual lots and tax treatment for tax-sensitive proposals; complete actions/flows/executions and endpoint observations for prospective comparisons.
5. Reviewed company evidence and scenario assumptions, plus optional provider credentials retained server-side.

[Operations](OPERATIONS.md) contains the commands and import contracts. [Feature map](FEATURE_MAP.md) and [methodology register](METHODOLOGY_GAPS.md) distinguish implemented, data-gated and intentionally deferred capabilities. Full historical backtesting, calibrated expected-return allocation, reverse DCF and brokerage execution are not implied by this integration.
