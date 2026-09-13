# Methodology integration register

The supplied reference is preserved unchanged under `portfolio_lab_project/` locally. Its 72 manifest entries matched their SHA-256 digests. The integrated numerical implementation lives in `portfolio_lab/`; the import namespace preserves regression compatibility. Product and application services use Portfolio Review.

This register distinguishes software behavior from inputs or evidence the application cannot create. Passing calculations does not establish forecast calibration or investment outperformance.

## Integrated numerical methodology

| Requirement | Result and verification |
| --- | --- |
| Real-use investment preferences | USD is the default reporting and aggregation currency. Benchmark, horizon cash return, sleeve budget and budget basis default to null; mandate remains unconfirmed; optimization defaults off. Synthetic investment preferences are declared only by demo generation. |
| Reconciliation | Explicit cash differs from missing cash; residuals remain unresolved. Separate account budgets, currencies and source completeness gates are retained. The analytical adapter owns decimal input normalization separately from numerical float arrays. Candidate exports also preserve decimal amount/quantity strings. |
| Universe | Scores require explicit US domicile, ordinary common equity, compatible currencies, source eligibility, dated issuer common-equity capitalization and appropriate accounting. Missing source identity is not inferred from ticker, name or USD. Rankings describe the configured universe, not an exhaustive market universe. |
| Liquidity and momentum | Liquidity uses exactly the last 60 XNYS session dates. Missing/duplicate/nonfinite observations cannot qualify through a median over fewer data. Momentum uses exact exchange-session endpoints at t−12 and t−1; a missing endpoint stays missing, including holiday month ends. |
| QVM | Sector-relative winsorized ranks, issuer deduplication, negative valid yields, required family coverage and average-asset denominators are retained. Company balance-sheet underwriting remains separate from the composite score. |
| Common-share earnings | `income_common` and `earnings_definition="common_shareholders"` are required for earnings yield. SEC consolidated `NetIncomeLoss` is preserved but cannot be divided by common-equity capitalization as a qualified earnings yield. The common-share SEC tag and component provenance establish the supported definition. |
| Scenarios | One date, common labels and common horizon across assets and benchmark are required. Probabilities may all be blank: per-scenario outcomes remain available, weighted means/optimization remain unavailable. `allocation.use_probabilities=false` explicitly ignores retained source probabilities and overrides for unweighted exploration without changing scenario outcomes. The default is true; partial or invalid active probabilities fail without normalization. |
| Cash/distributions | Horizon cash return is explicit (null live, zero only in the synthetic fixture). Missing cash return leaves portfolio scenario outcomes/means unavailable, preserves asset contributions, makes affected candidates draft and blocks optimization. Known cash return enters retained cash once; costs/reserves earn no return. Asset horizon returns already include distributions, without adding dividend or buyback yield again. |
| Calibration | A free-text calibration ID never grants a calibrated label or unlocks allocation. Imported calibrated claims are treated as subjective scenarios; calibrated allocation fails closed pending a supported evidence-backed calibration system. Editing scenarios remains subjective. |
| Risk | Same-asset common weekly covariance, at least 104 observations, Ledoit–Wolf shrinkage and annualization by 52 are retained. A five-year same-asset sensitivity is available with at least 208 common observations; its coverage/status is separate. Constant-current-weight historical illustration remains distinct from personal performance. |
| Explicit sleeve | Membership maps account/security IDs to the existing sleeve's account-NAV weights. Legacy ownership is not incumbency. An approved account-NAV budget is explicit. Retain eligible top-40% incumbents, cap at 20, fill from the top 20%; tied scores favor less turnover and then stable issuer identity. Legacy weights are preserved. A permitted residual or a visible blocked/infeasible outcome replaces guessed allocation. |
| Whole candidate records | No-change, approved-benchmark new flows, minimum-gross-turnover constraint repair, simple sleeve and optional advanced optimum use the same candidate/funding/constraint ledger. The simple candidate emits complete proposals even with the optimizer disabled. Each record includes its own scenarios, funding, decisions and status. |
| Flow policy | Supported flow allocations use evidence-backed cash already included in the frozen snapshot NAV/cash. A matching valuation date is mandatory. Flows are never added twice or reused automatically on later snapshots. External contributions/withdrawals that are not yet in the source snapshot must first be reconciled by ingestion. |
| Rounding and bands | Per-account/instrument fractional permissions, quantity increments and dealing permissions are explicit. Missing rules leave a draft. Round to permitted increments, apply bands to the whole basket, then recompute costs, lot reserve, cash and every funding/lock/exposure/risk constraint. If convenience bands break hard constraints, restore the complete necessary rounded basket; if rounding remains infeasible, no constraint is relaxed. |
| Candidate thresholds | Default minimum trade is max($500, 0.25% of account NAV); incumbent gap is max(0.5 percentage points, 25% of target weight). The 2% review hurdle uses modeled improvement after friction per dollar redeployed, over 12 months. It is unavailable for a different horizon; it is not a whole-portfolio return hurdle. |
| Sensitivity | For each candidate, 5/10/25 bps per side recompute cash and full constraints for the same rounded basket. The 1/2/4% substitution-hurdle comparison shares the same modeled improvement and redeployed principal. |
| Conditional taxes | Existing lot coverage, calendar holding period, no loss credit and positive-gain reserve checks remain. Final rounded sale quantities are independently re-estimated and reconciled to the candidate reserve. `after_tax_return` stays unavailable; this is not final tax liability or a tax-optimized claim. |
| Providers | CSV/SEC/FRED/Yahoo remain separate from holdings collection. HTTP adapters have bounded retries/backoff, response limits, cached content hashes and redacted errors. Cache sidecars cannot point reads outside the current provider cache. Provider failure never inserts demo data. |

## Configuration contracts

These are analytical settings, saved immutably with each run. They are not installed as the owner's preferences automatically.

```json
{
  "mandate": {
    "dealing_rules": {
      "synthetic_account": {
        "synthetic_security": {
          "fractional_shares": false,
          "quantity_increment": 1,
          "dealing_allowed": true
        }
      }
    }
  },
  "allocation": {
    "optimize": false,
    "cash_return": 0.0,
    "active_sleeve_weight": 0.20,
    "sleeve_budget_basis": "account_nav",
    "sleeve_membership": {
      "synthetic_account": {"synthetic_security": 0.10}
    },
    "flow_policy": "approved_benchmark",
    "new_flows": {
      "synthetic_account": {
        "amount": 1000,
        "source_id": "synthetic-deposit",
        "included_in_snapshot": true,
        "valuation_date": "2026-08-31"
      }
    }
  }
}
```

The approved budget is a fraction of each account's original NAV, not the household NAV or unlocked invested capital. Membership values describe the actual portion of each existing holding assigned to the sleeve. A new empty sleeve must explicitly provide an empty member object for each account. The candidate contains both desired and post-rounding membership; adoption is a separate decision, never a side effect of previewing.

Changing horizon clears incompatible numerical return overrides, priors and cash-return assumptions unless explicitly replaced. The default hurdle remains a 12-month research convention. A dealing permission confirms the supplied research dealing window; it does not connect to a broker, infer a plan fund's rules, or execute an order. Unknown/unavailable instruments remain draft, locked or blocked as appropriate.

## Data and evidence gates

| Capability | Current boundary |
| --- | --- |
| Market-wide or survivor-free historical ranking | Blocked without a licensed point-in-time universe, identity history, delistings and historical capitalization. Current holdings are not that universe. |
| Accurate live security identity, cash, NAV, tax lots and account permissions | Requires source data or validated supplements. Code and synthetic cases are implemented; the application cannot invent the owner's values or restrictions. |
| Currency conversion and aggregation | USD is the default target. Original quote, reported market-value and account currencies retain their separate meanings. Automatic FX conversion is not implemented; mixed or unknown value currencies remain blocked without verified conversion. Selecting USD does not relabel a foreign amount or convert it at parity. |
| Complete fund exposure | Requires dated complete disclosures and underlying issuer mappings. Partial fund holdings remain unresolved, not inferred cash. |
| Qualified company fundamentals | Requires compatible currency/accounting definitions, public availability and receipt evidence. SEC supports reviewed USD US-GAAP tags; unsupported IFRS/custom tags require a reviewed import. |
| Intraday historical knowledge | SEC date-only filings retain the conservative next-day policy; same-day FRED vintages do not establish an exact publication time. Stronger replay needs actual availability/receipt records. |
| Calibrated expected-return optimization | Intentionally gated: no calibration registry/model validated on matured outcomes is claimed. Subjective experimental optimization remains explicitly labeled. |
| General household tax accounting | Intentionally unsupported: loss netting/credits, wash-sale resolution, foreign taxes and final tax returns are outside the conditional reserve. Missing treatment cannot appear as zero known tax drag. |
| Brokerage execution | Intentionally absent. No orders, cross-account transfers, shorts, leverage or derivatives. Research dealing rules and candidate validation are not broker execution certification. |
| Full historical strategy performance | Separate gated extension requiring transactions/flows, actions, historical universes, facts and costs. Prospective evaluation and a historical risk illustration do not establish backtested alpha. |

Company assessments, EPS/multiple/DCF valuation, immutable application persistence, the actual Yahoo read adapter, unified UI, calendar metadata and prospective comparator ledgers are integrated in their application modules and documented in the overall feature map. This numerical register does not substitute for their acceptance checks.
