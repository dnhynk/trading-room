# Configuration boundary

The `.yaml` files in this directory use JSON syntax, which is a valid YAML subset and can be parsed with Python's standard library. All financial values are quoted decimal strings.

- `observe.yaml` is the repository default. Signal and holding parameters are deliberately null, so observation cannot silently become a strategy run.
- `paper.yaml` contains a synthetic 1,000 USDT scenario and explicitly labelled research parameters. It is not a user-approved capital budget.
- `research_profiles.yaml` preserves `aggressive_bounded_research` and adds the user-directed `full_seed_10x_preposition_research`: 10x leverage/notional, the whole Bitget available-USDT budget, and 100% campaign principal-loss tolerance. The entire entry budget belongs to stage 1. All named research profiles remain excluded from live configuration.
- `accumulation.paper.json` is the active adaptive-accumulation research configuration for Classic v2. The first target is 10%, normal clips are capped at 10%, and pre-breakout clips at 25% of the original notional budget. Pullback and compression targets are 70% and 85%; strong pre-breakout buying targets the remaining budget. These are transparent research defaults, not fitted or proven probabilities. Below-average additions are explicitly owner-authorized for this strategy only. The old one-shot profile is not used by this runner.
- `live.example.yaml` leaves capital, leverage, loss, funding, holding, liquidation-buffer, emergency-exit, end date, timezone, API family, and approval fields null. Validation must list every missing value and make no exchange mutation.

Increasing budget/leverage/risk, loosening protection, ending cooldown early, or increasing profit reuse is a risk-increasing change. A proposal must record the before/after worst loss, configuration hashes, rationale, a review delay (48 hours is only a comparison default), and explicit approval. Risk reductions may be applied immediately in a later authorized live implementation.

The [preposition objective](../docs/preposition_objective.md) records the user's explicit
capital, leverage, and total-loss choices. Those choices supersede the earlier
comparison risk budget; another confirmation of those same choices is unnecessary.
The profile is a research input, not an instruction to submit an exchange order.

`accumulate` validates its own Classic paper configuration; it does not reinterpret
the UTA live template. Its network client has only fixed-host, allowlisted GET
requests. An explicit `--notify-slack` enables labelled paper-fill notices through
the existing `common.notify` renderer/sender and existing webhook configuration.
