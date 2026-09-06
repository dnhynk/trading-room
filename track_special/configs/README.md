# Configuration boundary

The `.yaml` files in this directory use JSON syntax, which is a valid YAML subset and can be parsed with Python's standard library. All financial values are quoted decimal strings.

- `observe.yaml` is the repository default. Signal and holding parameters are deliberately null, so observation cannot silently become a strategy run.
- `paper.yaml` contains a synthetic 1,000 USDT scenario and explicitly labelled research parameters. It is not a user-approved capital budget.
- `research_profiles.yaml` preserves the proposed aggressive-but-bounded comparison values. `live_approved=false` prevents promotion by copying the profile.
- `live.example.yaml` leaves capital, leverage, loss, funding, holding, liquidation-buffer, emergency-exit, end date, timezone, API family, and approval fields null. Validation must list every missing value and make no exchange mutation.

Increasing budget/leverage/risk, loosening protection, ending cooldown early, or increasing profit reuse is a risk-increasing change. A proposal must record the before/after worst loss, configuration hashes, rationale, a review delay (48 hours is only a comparison default), and explicit approval. Risk reductions may be applied immediately in a later authorized live implementation.
