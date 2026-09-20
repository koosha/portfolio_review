# Recorded provider responses

One file per provider capability, in the provider's own field names, read by
`tests/test_provider_contracts.py` through `tests/support/provider_fixtures.py`.

**Every value here is invented.** The ticker symbols are public (`AAPL`, `XIC.TO`,
`MSFT`, `BRK-B`, `F`) because a symbol is the one thing a shape test cannot fake, but
the prices, statements, estimates, holdings, news, exchange rates, CIKs and company
names are fictional and describe no real issuer. `Example Devices Inc.` is not a
company, and CIK `1234501` belongs to nobody. Nothing here came from an account, a
portfolio or a personal export.

| File | Read by |
| --- | --- |
| `yahoo_prices.json` | `market_data.price_history` — `history` frame and `history_metadata` |
| `yahoo_statements.json` | `market_data.statements` — annual and quarterly income, balance and cash-flow frames, filings |
| `yahoo_estimates.json` | `market_data.estimates` — consensus frames, price targets, recommendations, calendar |
| `yahoo_funds.json` | `market_data.fund_disclosure` — `funds_data` top holdings, sector weights, asset classes |
| `yahoo_news.json` | `market_data.news_and_filings` — story envelopes, filings, scheduled dates |
| `yahoo_profile.json` | `market_data.security_profile` — the `info` mapping and metadata |
| `bank_of_canada_valet.json` | `fx_providers.bank_of_canada_observations` — `{d, FX…CAD: {v}}` observations, with 2026-09-07 (Labour Day) absent |
| `sec_company_tickers.json` | `issuers.sec_issuer_lookup` — ticker to CIK records |
| `sec_companyfacts.json` | `providers.extract_sec_fundamentals` — the US-GAAP USD tags the extractor reads |

## Refreshing from live data

When a provider ships a new shape, re-record by hand and read the diff:

```
uv run python scripts/provider_smoke.py --record AAPL --fund XIC.TO --cik 320193
```

That makes live requests to public endpoints only and rewrites every file above,
trimmed to the same encoding. It is never run by a test, a hook or a review. A refresh
replaces the invented values with whatever those public endpoints answer, so decide
before committing whether this directory should keep real recorded numbers or go back
to synthetic ones with the new field names applied by hand.

## Naming a new fixture

The repository's privacy patterns are broad and unrooted on purpose — `*.csv`, `*.db`,
`*token*.json`, `*cookies*.json`, `*session*.json` — because they are the backstop that
keeps real holdings out of a push, and they apply here as well as anywhere. A fixture
named to collide with one is ignored: `git status` stays green for whoever added it and
the file is simply missing for everybody who clones. Keep the `yahoo_*` / `sec_*` /
`bank_of_canada_*` convention, which collides with none of them, and prefer renaming a
new fixture over narrowing a pattern. `PublishedFixtureTests` in
`tests/test_provider_recording.py` walks this tree the way git does and fails if a file
here would never reach a checkout.
