# Track C: liquidity recovery and real-fill observation

Track C estimates what BTC that turned briefly cheap on Coinone is worth to buy and sell again. In `structural_sampling` mode it uses the minimum-size candidate at the base offset (`0:minimum`) that passes the safety checks. In `execution_sampling` mode it uses the nearest minimum-size candidate below the reference-price floor, at a discount floor registered separately. Both modes only record the learned values. The published configuration is `observe` and starts no live trading. There is no automatic retraining, size increase or mode switch, and better prediction or profitability has not been shown.

| Path | Role |
|---|---|
| `live.py`, `runtime.py` | Decision policy, market intake, account reconciliation and the execution loop |
| `market/` | Local flow and order book, external fair price, data freshness, risk measures |
| `learning/` | Empirical estimates conditioned on state, price and size, plus comparison estimators |
| `execution/` | Order IDs, cancels, partial fills, protective orders, inventory and cash ledger |
| `replay/` | Attempt labels and replay, chronological training, probability and KRW P&L checks |
| `ops/` | Status and fill audits, notifications, recovery, release packaging |
| [`../track_c_multivenue/`](../track_c_multivenue/) | Separate multi-venue study that reads raw Track A-2 sessions in receive order; outside the Track C source hash |

## Decision inputs

Inputs are the residual discount, the local sell shock, external price moves and their freshness, spread and exit depth, and each candidate's price and size. The policy builds a market state, applies the safety checks and then builds up to six price and size candidates per episode. An empirical distribution that uses one nearby real sample per episode estimates the KRW value per order, fill statistics, the probability of recovery or failure after a fill, and the value of holding instead.

Per-order P&L already includes unfilled orders, so the fill probability is not applied a second time. KRW P&L is kept apart from inventory and time penalties. Stops, timeouts, partial exits and leftovers stay in the ledger, and gaps in observation are recorded as censored. The few fills in a public-queue replay are not a P&L floor. `learning/benchmark.py` is the earlier estimator, kept to measure what the learned values add under the same candidates and execution.

## Pre-send recheck

The live policy (`VERSION = 'c4-live-v2.4'` in `live.py`) runs candidate scoring and probability work on a worker thread. After that work, and again right before transmission, it checks the decision's age, the original book time, the latest market, the connections, the account and order ownership. It rebuilds the safety stage and the candidates from the latest state and sends only if the chosen price, size and stop are still allowed. A deep-book update that does not affect the order is accepted; a change in the candidate or the safety checks drops the decision. Time spent in the work queue, the API rate limiter, the connection pool and the TLS handshake counts against the decision's lifetime, and a new book does not extend it. `C4_DECISION_TIMING` events record each step and wait. An intent that expires before sending is not treated as an uncertain exchange order: its cash reservation is released and it is left out of fill-probability checks. Notifications show a local drop (`transmitted=false`) apart from an exchange rejection.

## Exit replay

The default replay (`VERSION = 'c4-state-value-v2.1'` in `learning/__init__.py`) follows the live exit order: reserve the protective order, cancel and reconcile it, then sell the remainder at market. It keeps full and partial fills of the protective order during cancellation, unfilled limit orders and leftovers, and applies the same procedure to an immediate exit and to continued holding. The default 500 ms for cancel and reconcile and 250 ms for the following balance check and submit are fixed scenario assumptions. Protective triggers on public trade prices and queue fills are not calibrated against real fills either. `exit_protocol=direct` replays the earlier single-delay procedure for comparison. Protective orders and market-order price limits follow the [Coinone order API](https://docs.coinone.co.kr/reference/place-order).

`expected_net_krw` and the recovery and failure probabilities are predictions under public-replay assumptions and have not been checked against real expected returns. `ops.live_evidence` summarizes cancel-and-reconcile times, exit request to response and fill times, and unresolved cancels, and separates the number of observations from the number of distinct orders and campaigns. These times differ from exchange matching latency, and values measured under another exit procedure are not applied as calibration for the current one.

## Example configuration

The example trades BTC only, with at most 20,000 KRW per order, a 2-tick discount floor, an 8-second entry time-to-live and a 180-second holding limit. It checks cash, exit liquidity, the exchange minimum, the risk budget, a volatility stop fixed at entry and an exchange protective order. ETH, XRP, SOL and others are recorded only. Whether the daily trade count is enough, and whether more symbols would help, is unconfirmed.

`execution_sampling` needs an explicit `c4_sampling_entry_ticks` in the runtime configuration, at least 1 tick and below the model floor. External consensus, the reference-price floor, freshness, spread, liquidity, cash, risk and stop checks all stay on, and size is limited to the minimum order unit. This sampling policy is not evidence of profitability.

```bash
python -m track_c.replay.engine --help
python -m track_c.replay.evidence --help
python -m track_c.ops.live_evidence --help
python -m unittest discover -s tests -t .
```

## Models, credentials and operations

The trained model, market recordings, real ledgers and evaluation registrations are separate inputs. When the source changes, build a new model from that source; the model's source-hash check stays on. A model revised after looking at evaluation results gets a new evaluation window and is not mixed with existing models, ledgers or evaluations.

The `.env` path, model path and `127.0.0.1` egress IP in `config.json` are examples. Coinone credentials come from `COINONE_ACCESS_TOKEN` and `COINONE_SECRET_KEY`. Do not run with the egress IP, funding, model or risk checks switched off.

The remote operations tools work only when `TRADING_ROOM_AWS_INSTANCE`, `TRADING_ROOM_SSH_HOST` and `TRADING_ROOM_SSH_KEY` are set. `TRADING_ROOM_REMOTE_ROOT` defaults to `/opt/trading-room-c`, the SSH user is `ubuntu` and the AWS region is `ap-northeast-2`. The AWS CLI comes from `TRADING_ROOM_AWS_CLI` or `PATH`. `ops.deploy` builds a local bundle or stages a separate release; switching the service over is a separate operating step.

Notifications go through one relay. The display window, the engine's UTC risk day and the evaluation window are kept apart, and KRW P&L is shown to one decimal place.
