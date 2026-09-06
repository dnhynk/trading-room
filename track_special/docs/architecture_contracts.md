# Architecture contracts

## Isolation and authority

The ARX special track owns a new state namespace, database, configuration hash, intention ID prefix, and client-order-ID prefix. It imports no A/B positions, orders, capital-pool claims, realized PnL, stop files, or ledger rows. It imports no C KRW state, model, evaluation registry, or notification cursor. Existing spot assets remain spot assets and are never interpreted or sold as futures inventory.

The only mode enabled by repository defaults is `observe`. `paper` and `replay` write solely to local research state. `live` is fail-closed until a human supplies every nullable live risk setting and an authenticated, read-only preflight proves the account, mode, margin, instrument, positions, pending orders, protection semantics, and strategy ownership. Development never calls a private write endpoint.

## Data contract

Every normalized market record has `venue`, `api_family`, `category`, `symbol`, base/quote/settlement identity, exchange time, receive time, source sequence when available, and a hash of the raw payload. Spot and futures records have distinct stream keys. Mark, index, last, and executable bid/ask retain their names and are never silently substituted.

Collectors are append-only and idempotent on a source event key. They retain duplicates as observations while marking duplicate identity, record reconnects and gaps, and report first/last receive time, count, coverage, longest gap, and latency. Unavailable liquidation or wallet data is stored as unavailable rather than synthesized.

Research facts store `source_url`, `event_at`, `first_observed_at`, `fetched_at`, `expires_at`, raw hash, and a classification of `official_fact`, `inference`, or `unverified`. Research text can explain but cannot approve an order.

## Risk contract

All prices, quantities, rates, and money values cross boundaries as `Decimal`; aware timestamps normalize to UTC. Base quantity is the normalized underlying quantity after any contract multiplier.

For a verified USDT-linear contract:

```text
N = abs(q) * mark_price
effective_leverage = N / strategy_equity, only when equity is reliable and > 0
initial_margin_estimate = N / configured_leverage  # diagnostic only
gross_position_pnl = q * (exit_price - average_entry_price)
```

Every entry candidate is evaluated with existing lots plus `RESERVED`, `SUBMITTING`, `ACKNOWLEDGED`, `RESULT_UNKNOWN`, `OPEN`, and `PARTIALLY_FILLED` entry reservations at their worst allowed fill. At a conservative common `P_exit`:

```text
pnl_at_stop = realized_net_pnl
              + sum(filled_qty * (P_exit - entry_price))
              + sum(reserved_qty * (P_exit - worst_fill_price))
              - unbooked_future_exit_costs
              - unbooked_stressed_future_funding
principal_loss_at_stop = max(0, -pnl_at_stop)
giveback_at_stop = max(0, current_campaign_net_pnl - pnl_at_stop)
gross_stop_risk = sum(filled_qty * max(entry_price - P_exit, 0))
                  + sum(reserved_qty * max(worst_fill_price - P_exit, 0))
                  + costs not already posted to realized PnL
```

Realized fees and funding already included in `realized_net_pnl` are never subtracted again. Principal loss, giveback, gross stop risk, gross notional, isolated margin, tier/MMR, liquidation buffer, stage notional, funding, liquidity, and order precision each have independent gates. A tighter stop cannot be invented merely to make size pass.

Reservations and approvals are created in the same database transaction. An approval binds campaign revision, config hash, market/account snapshot times, exact candidate, risk totals, and a short expiry. Execution must re-read all of them immediately before submission; stale approval is unusable.

Daily and weekly loss are deposit/withdrawal-adjusted equity changes from fixed timezone boundaries. Campaign loss is net PnL against fixed `E0`. Drawdown is measured from the contribution-adjusted strategy-equity high-water mark. Restart and date rollover do not reset durable history. Global triggers enter `EXIT_ONLY`; campaign triggers do not auto-clear at a time boundary.

## Strategy contract

The deterministic state machine is `WATCH → PROBE → BUILD → RIDE → HARVEST → FLAT → COOLDOWN`. Strategy emits reason-coded intents only. A stage is not owed: elapsed time never fills it, missed stages are not caught up, and each stage fraction is only a cap on approved maximum notional.

`PROBE` requires predefined stabilization, confirmed box, volatility contraction, and relative-strength conditions. `BUILD` requires a newly observed confirmation and a conservatively profitable existing position after exit costs. No future-right-hand candle may confirm a low before its confirmation time. No add is allowed during loss, after lowering a protective stop, or as a larger recovery trade after a stop.

## Order and protection contract

An `OrderIntent` is not an order. The durable lifecycle is intention → transactional reservation/approval → submitting → ACK/result-unknown → exchange reconciliation → open/partial/fill/cancel/reject. Timeouts and named ambiguous error codes are reconciled by client order ID before any retry. Duplicate events are idempotent; cancel-pending fills remain fills.

One-way long reduction is a `sell` with `reduce_only` in the selected adapter only after that adapter's account/API family is verified. UTA v3 names and Classic v2 names live in separate wire mappers. Server ACK is not matching confirmation. Reduce-only auto-cancel/replacement behavior is treated as an exchange capability requiring account-level testing.

Protection has its own lifecycle. New exposure is not considered protected until a server-side order covering the actual position quantity is queried as active with the approved trigger reference. Protection-before-entry is not assumed atomic. Any uncovered fill starts a bounded repair deadline; further entries stop, and the approved emergency reduction policy—not an invented market order—determines the next action.

## Ledger contract

The ledger is append-only, transactional, and balanced by typed accounts for cash/equity, isolated margin commitment, reserved entry exposure, position notional memo, realized trading PnL, unrealized PnL memo, fees, funding, and profit allocations. Margin return is not profit; full close notional is not sale revenue.

Only a new positive high-water increment in cumulative net realized PnL is eligible for the research allocation of 25% reusable and 75% reserve. Recovery of an earlier loss, unrealized PnL, deposits, or returned margin creates no reusable profit. Neither allocation raises `E0`, principal-loss caps, or gross-notional caps.

## Account and live gate contract

Live entry requires a verified online `ARXUSDT`, `USDT-FUTURES`, linear perpetual instrument for Arcium ARX; a single USDT margin coin; isolated margin; one-way mode; disabled automatic margin top-up; dedicated or verifiably separated strategy equity; no foreign/manual exposure; reconciled positions/orders/protection/ledger; server-side protection capability; fixed leverage while flat; and complete approved live settings.

UTA Advanced/shared multi-asset collateral, crossed margin, hedge mode, unknown/null liquidation semantics, missing tiers, stale account data, or any API-family ambiguity blocks entry. Code never changes account mode, margin mode, or leverage as part of validation.

