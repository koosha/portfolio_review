# Portfolio Review integration

## Discovery baseline

Started from `main`, commit `19ab1f3af23ec0dab84b6a6984a7d95b6fed2f53`. The collector is present locally and published; the research handoff's earlier remote-empty observation is superseded. Existing untracked handoff/reference files are user supplied. Preserve the running collector on port 8765 during development; verify the replacement on a separate loopback port and isolated synthetic data first.

The supplied reference is already extracted under `portfolio_lab_project/`. No ZIP was found in the project. All 72 manifest entries match their SHA-256 checksums. The archive checksum itself cannot be reverified without the ZIP. Reference assets/caches/reports are ignored; only audited modules, synthetic generators and tests are adapted.

## Architecture

Keep the Python stdlib local HTTP server, Chrome connector and existing JavaScript components. Introduce modular research services behind the same authenticated local boundary and one navigation/context. No additional dashboard server or frontend framework migration is necessary. Retain `portfolio_lab` as an internal numerical compatibility namespace; new `portfolio_research` modules own the real-schema adapter, company evidence/valuation, jobs, API schemas and operations.

The collector owns source migrations and batch publication. Analytics read exact completed batch members through a read-only transaction, never the per-account `latest_positions` view. Research inputs, versions, jobs and immutable runs use a separate SQLite database. Browser previews identify their own base run and request; no server-wide mutable selected run.

## Reviewable increments

1. Verify reference regressions and collector baseline; document source schema and capability map.
2. Add published collection batches and a conservative read-only adapter with validated supplemental inputs.
3. Audit/adapt numerical modules; add company evidence, EPS/P/E, DCF and sensitivities; implement the trading-calendar policy.
4. Provide unified Overview, Holdings, Research, Scenarios and Review views, with Data/Settings utilities; connect tracked jobs and immutable preview/save/replay operations.
5. Complete monthly/demo/evaluation/export/backup commands, prospective comparator records, meaningful tests and browser verification.
6. Review public source/privacy, complete checks, commit with the owner's identity and push without force after rechecking the remote.

## Progress and live data gates

- Discovery complete: existing account snapshots are receipt-dated and individually atomic. They cannot prove a coherent historical batch or actual valuation dates.
- Existing values lack explicit currencies; independent account NAV, account treatment, lots, permissions and stable identity mappings are missing. These remain data gates with validated input paths, not invented defaults.
- Reference audit confirms additional work for valuation, calendar timing, calibration evidence, optional scenario probabilities, explicit sleeve state/budgets, simple-candidate baskets, rounding/bands and safe export fields.
- Core integration, forward valuation/evidence, unified UI, candidate review, prospective evaluation and operating commands are implemented. Backend and DOM/API regressions pass. Chrome visual checks and the final live-server switch are environment-blocked; see VERIFICATION.md. Passing software tests are not evidence of investment outperformance.
