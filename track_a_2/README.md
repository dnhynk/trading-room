# Track A-2: long-only rotation on Coinone spot

The live executor for Track A-2 is implemented, and so are the boundaries for public recording and execution replay. The repository's operating values are paused, observe, execution disabled and `universe=[]`, and no approved evaluation is set. Code that can run is a separate matter from approval to trade live. With the defaults, `python -m track_a_2.live` stops before it reads credentials or creates its state directory.

## Relation to the multivenue study

The track's own slowdown alpha is frozen. The execution and recovery code stays for auditing past orders and for comparison, and it does not restart by itself. [`track_c_multivenue`](../track_c_multivenue/) reads the unauthenticated Coinone, Upbit and Bithumb recordings and the Track A-2 slowdown condition as comparison groups P0 to P3, under the same minimum size, cost, latency and exit conditions as Track C's external-discount condition. This does not change the frozen Track C BTC model or ledger, and multivenue results are not used to promote anything to live trading.

## Strategy contract and alpha status

Track A-2 takes only the pure features and the long rotation state machine from Track A.

- Long only, on Coinone KRW spot. There are no shorts, borrowing, margin or funding rules.
- It tries limit buys as a decline slows. Each further buy keeps a ladder step below the previous buy.
- It takes quantity back as a rebound stalls. There is no permanent core position.
- A falling eligibility rank alone does not replace a held symbol. Held symbols and symbols with active orders are always watched, selected or not.

The speed signal can optionally scale with dip depth. With `v_fast_floor` and `v_depth_elasticity` both at 0 it matches the earlier rule exactly. With both on, the required speed is:

```text
v_fast_eff(D) = max(v_fast_floor,
                    v_fast × (dip_min_atr / D) ^ v_depth_elasticity)
```

At `D=dip_min_atr` the original `v_fast` is required, and only deeper dips count gradually as extra evidence. Every `v` signal records the `v_fast_eff` actually applied. Config validation rejects a floor above the original threshold and a setup with only one of the two parameters on. More frequent fills count as added economic exposure, so this relaxation does not raise unit size, the number of concurrent symbols, symbol or portfolio notional, or the loss budget by itself.

The default `max_units=4` and the disabled entry-flow and decay confirmations are still research candidates. Preserved Coinone tapes give no post-fill net P&L evidence, so optional rules such as `entry_flow`, `entry_decay` and `add_confirm` were left unchanged. Live trading that the owner starts without an evaluation is marked `owner_unvalidated_live_approved=true` and is not presented as an alpha approval. The owner override keeps the long-only spot contract and the 4-unit maximum, and it is not labelled as a profitability check. These execution gates were tightened to block economically wrong orders:

- Current maker plus taker cost is the floor for every profit threshold. When the base threshold is below cost, the relaxation formula does not invert.
- Profit-taking is judged on the conservative fill size and net sale price across the current top five bid levels. The mid price is not used.
- The `limit_price` of a profit-taking market sell is the lowest price that clears cost, target profit and round-trip cost. The stop price is not used there.
- Stops, recovery and cuts after structural damage are separate from the profit conditions, and they reduce risk without a price floor when needed.

None of this validates the alpha. Whether the slowdown signal raises real net value per order above a plain dip buy or a one-unit baseline has to be shown separately on recorded tapes.

## Symbols and sizing

Only symbols in the explicit `universe` in `config.json` are candidates. Symbols that pass trading and maintenance status, limit, market and stop order support, 24-hour KRW traded value, range and spread are ranked by traded value times range. Eligible symbols already selected stay; only empty slots are filled. A held symbol that drops out or sits outside the basket moves to `wind_down`, which blocks new buys and adds only.

The owner-set high-risk defaults are: unit 15% of equity, symbol notional 60%, total notional 90%, per-symbol campaign loss budget 2% and daily loss 6%. Spot has no forced liquidation, but principal loss, gaps, vanishing liquidity and long inventory risk all remain. Right before an order, the size is capped again by the smallest of:

```text
strategy base size
→ available KRW and remaining symbol and portfolio notional
→ conservative entry size across the current top five ask levels
→ conservative exit size across the current top five bid levels
→ exchange minimum and maximum quantity and amount, and quantity step
→ a size that does not breach the campaign loss budget at once
```

If the first real fill is half a unit, the campaign cash loss budget also starts at half. Only later partial fills of that first order raise the budget, by the size actually filled. Adds do not enlarge the campaign budget.

## Orders, accounting and recovery

- Order IDs use the `ta2-...` prefix only. The intent is synced to SQLite before sending. An order whose response was lost is queried by the same ID and is not submitted again.
- The runtime sends three order shapes: `BUY LIMIT` (post-only), `SELL MARKET` and `SELL STOP_LIMIT`. The client has no withdrawal or transfer methods.
- Cumulative filled quantity, average price and fees are applied once and only move forward. Across several partial fills of several orders, the overall average cost and the per-order lots are kept apart.
- When a reported fee exceeds the expected cap, a trustworthy real fill is still booked to cash, inventory and P&L first. Then `FEE_MISMATCH` stops new entries and the real inventory stays protected. Finished orders are queried again during the settlement window to pick up fee corrections.
- Each order, its fills and its settlement corrections carry the immutable `campaign_id` set at creation. A late fee correction from an earlier campaign counts toward total account P&L and stays out of the new campaign's P&L and loss budget.
- Cancel intent and cancel confirmation are separate. A cancel whose response was lost is retried at a limited interval and count. A protective order with a cancel request already sent does not count as valid protection.
- A short gap while a per-second feature waits for the next exchange second, or a stretch with no new ORDERBOOK message because the book did not change, is not read as the strategy's intent to cancel. An approved maker buy keeps its queue place until the original signal's `buy_ttl_s`, as long as it passes the websocket connection, current book shape and depth, cash, notional and campaign risk checks again. No new order is created without a feature decision. An explicit strategy release, a disconnect, the signal's time-to-live or an operating stop cancels the order. New submissions keep the strict book-age limit.
- Protection of held quantity counts as complete only when the exchange detail shows `NOT_TRIGGERED` with the exact quantity, price and trigger. Unconfirmed protection blocks adds and normal exits; after a time limit it is cancelled, reconciled and turned into a recovery exit.
- Inventory moves through `protected → protection_canceling → selling → reprotecting`. If a small trim is below the minimum sell amount, the existing protective order is not cancelled first. Every path in which a sale stops or fills partly after a protection cancel ends in re-protecting the rest or in a recovery exit.
- After a gap below the protective limit, or with quantity left after a trigger, the protection is cancelled and reconciled and the rest is sold at market without a price limit. `STOP_LIMIT` does not bound the loss in a gap.

The campaign ledger combines realized net P&L, the remaining average cost and future exit costs:

```text
campaign realized net P&L
+ net P&L of stopping out the remaining quantity
>= -fixed campaign loss budget
```

The protective price is built by working back from known tick rounding and the limit buffer in ticks and basis points, so that the real `STOP_LIMIT` limit price stays at or above this accounting floor. The preset buffer is part of the budget. Market loss through gaps, vanished bids or partial fills is still possible.

The daily loss block compares mark-to-market P&L with equity at the start of the UTC day, after removing same-day external deposits and withdrawals. Protective fills, stop-gap exits and recovery market exits each count as one stop event per campaign. This rule blocks new entries; gaps and lost liquidity can still push the real loss further.

When the settlement window ends, active order state leaves the state JSON and only the append-only event history remains. After boot, symbol search and metadata REST calls run outside the order, protection and reconciliation path, and only finished snapshots are applied by the single owning loop.

## Public recording and execution replay

The recorder writes public REST seeds, raw websocket messages and connection events to a dedicated state path without authentication. It does not read `.env` and creates no tapes inside the repository. Long connections send Coinone JSON `PING` messages and expect a `PONG` within a time limit. Subscription errors, the first real data on each per-symbol state channel, subscription confirmations and real events on event channels, receive gaps, reconnects and heartbeats are recorded as separate quality items. A quiet period with no `TRADE` events because nothing traded is not counted as data loss.

```bash
python -m track_a_2.observe --coin BTC --coin ETH --seconds 3600 --session a2-research-01

# Record Coinone, Upbit and Bithumb public messages together, in one process's receive order
python -m track_a_2.observe --coin ETH --coin SOL --coin XRP --external upbit --external bithumb --session a2-crossvenue-01
```

External reference recording uses no order or account authentication. Coinone messages go to `public.jsonl.gz` and external ones to `external.jsonl.gz` in the same session. Every row in both files shares one global `sequence` and the local `received_ns` and `monotonic_ns`, and the exchange time is indexed separately as `exchange_ms`. Research code may only use receive sequence numbers below the current row. Re-sorting by exchange time to slip in an external price that had not arrived yet is not allowed. External quality covers first data, longest gap, reconnects, server errors and WebSocket control PING and PONG for each venue, symbol and book or trade stream. Having this extra data does not approve the Track A-2 alpha or live trading.

The replayer checks the seed and manifest hashes and the receive order, then runs the real `Market → Strategy → Runtime → OMS` chain. It simulates a 250 ms decision tick, event wake-ups, the post-only queue, partial fills, send and cancel latency and races, protective triggers, market sells across the top five bids and cancellation of `limit_price` leftovers. The headline value assumes leftover inventory is sold at once into conservative bid depth.

```bash
# Records research results only. This is not a live approval.
python -m track_a_2.replay ../trading-room-state/track-a-2/observations/a2-research-01 --config track_a_2/config.json --approval-id a2-eval-research-01 --holdout

# Pass --approve only after the owner has reviewed and accepted the result.
python -m track_a_2.replay <observation> --config <evaluated-config> --approval-id a2-eval-approved-01 --holdout --approve
```

Every run keeps losses as well as profits as finite signed `Decimal` research results. Without `--approve` the result is `RESEARCH_ONLY`. An approval request that misses a threshold writes its report first and is then marked `REJECTED`. `APPROVED` requires a continuous feed, an unused period, the base, one-unit and cost, latency and depth stress runs, the same universe, real campaigns, positive exit P&L and no halt. The owner then has to enter the artifact's path and SHA-256 in the config. The approved artifact is bound to the Track A-2 code, the shared strategy and Coinone transport code, the dependency declarations, the non-operational config, the raw data and the cost, latency and depth models. If any of these changes, the evaluation-based live preflight fails.

Public tapes cannot fully show the queue ahead of a real private order, hidden liquidity or market impact. A tape with reconnect gaps can be used for research and cannot be used for approval. Replay fixes the symbol selection at seed time, so dynamic symbol changes over a long run need a separate observational comparison.

## Capital and activation locks

The default contract is a Coinone portfolio separate from Track C's and keys dedicated to Track A-2 (`credential_profile=a2`). A second contract that shares the portfolio is available only with the account owner's explicit approval. Shared mode cannot be marked as isolated and needs all of the following:

- `shared_portfolio_approved=true` and an explicit shared key profile (`coinone_default` or `coinone_aws`)
- a fixed `capital_allocation_krw` owned by Track A-2 and a `cash_reserve_krw` left for the other tracks
- `shared_reserved_symbols` covering the other tracks' traded symbols; the config is rejected if it overlaps the Track A-2 universe

KRW in a shared account is fungible at the exchange, but the Track A-2 ledger owns only its fixed allocation, its `ta2-...` orders and its own filled inventory and P&L. Cash changes from other engines do not change Track A-2 P&L, and the track does not enter symbols where other assets or orders are found. There are no withdrawal or portfolio transfer functions.

A normal new entry needs all of these:

1. A separate portfolio with dedicated Track A-2 keys, or the shared capital and symbol contract above set explicitly, plus a fixed IPv4 address.
2. The ID of a reviewed and approved evaluation, the external manifest path and SHA-256, and exactly the same universe. The owner's unvalidated live exception uses an `a2-owner-...` ID and an explicit override, allows at most 4 units and does not count as an evaluation approval.
3. `track_a_2/config.json` changed to `status=active`, `mode=live` and `execution_enabled=true` together, with every required value of the chosen account contract filled in.
4. Track A-2 activated separately in `config/tracks.json`.
5. No `STOP` or `PAUSE` file in the repository or in the Track A-2 state path.

Each buy checks these conditions again, together with the latest account, book and risk, right before the HTTP request is sent. The repository defaults do not meet these conditions.

```bash
# Check the locks only, with no credentials, no network and no state created
python -m track_a_2.live --check

# Read-only status
python -m track_a_2.status

# Recovery check that manages only risk the Track A-2 ledger already owns
python -m track_a_2.live --recovery-only --check

# Possible only after the owner has met every normal activation condition
python -m track_a_2.live --config track_a_2/config.json
```

A real `--recovery-only` run needs the chosen account contract, a fixed IP and existing exposure in the Track A-2 ledger instead of an approval artifact or the new-entry switches. In this mode the code refuses buys and only protects, reduces or exits existing inventory. A recovery owner that was started explicitly keeps managing existing Track A-2 risk even while the repository-root `STOP` for Tracks A and B is present, and it still treats a `STOP` in the Track A-2 state directory as a signal to exit.

## Slack notifications

The Track A-2 notifier is a read-only process separate from the trading engine. It reads only the Track A-2 SQLite ledger and `status.json` and imports no Coinone API or order code. It reuses the shared Slack Block Kit card rendering and the existing webhook, and the dashboard at the bottom of each card uses only the Track A-2 KRW ledger.

```bash
python -m track_a_2.ops.notify --data-dir ../trading-room-state/track-a-2 --env <.env-with-the-existing-Slack-webhook>
```

It sends cards for the first connection, real buy and sell fills, confirmed exchange protective orders, symbol changes, halts and order anomalies, recovery, and status every 60 minutes. Partial fills within a short window merge into one card. Internal order and campaign IDs, raw errors, paths and secrets are not rendered. The outgoing queue and cursor are stored atomically in a separate SQLite file, and failures retry with bounded exponential backoff. Because of how webhooks work, a crash after Slack accepted a card and before the local commit can still produce a duplicate, and that state is kept as `delivery uncertain`.

Notifier status is in `<data-dir>/notifications/status.json`. Only one notifier writer can run per state path, and deployments keep the existing single Slack relay. Deploying or restarting the notifier is no reason to restart the trading engine or the Track C engine.

## Directory layout

| Path | Role |
|---|---|
| `market/` | Coinone KRW symbols, books, trades and candles; basket eligibility and unit rounding |
| `strategy/` | Spot sizing with capital, two-sided book depth and exchange limits |
| `execution/` | Dedicated credentials and order allowlist, approval preflight, ledger, partial-fill and restart reconciliation |
| `runtime.py` | Public and private streams, strategy and order-manager coordination, risk-first execution and safe shutdown |
| `observe.py`, `external.py` | Unauthenticated recording of Coinone and external KRW seeds and raw messages |
| `replay/` | Verified tape loader, simulated exchange boundary, real runtime replay and evaluation artifacts |
| `ops/` | Read model for the Track A-2 ledger only, durable Slack queue and card rendering |

Only code and regression tests live in the repository. Public recordings, contract snapshots, SQLite ledgers, state and evaluation output go to `../trading-room-state/track-a-2/`. Config validation rejects state paths that belong to another track or sit inside the repository.
