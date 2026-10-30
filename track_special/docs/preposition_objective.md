# ARX pre-positioning objective

The owner's goal is to hold, before a full rally starts, an ARXUSDT long of about 10 times the margin budget, with all available USDT on Bitget as that budget. The owner also confirmed accepting the loss of that entire seed. This document records that risk choice. The goal is not to put everything into the first order but to finish accumulating in tranches before the rally, according to price and fill conditions. The owner also explicitly allowed buying below the average price. The current implementation does not send real orders.

## Capital and exposure

- `E0` is the available USDT on Bitget, reconciled before the campaign starts. Amounts already reserved for orders and margin held by other positions are not counted as available again.
- The target notional is `10 × E0`, the configured leverage is 10x and the margin budget is `E0`. Fees, amounts the exchange freezes for open orders, entry losses and quantity units can make the notional that can actually be ordered smaller than the target. Reservations and ACKs are not counted as holdings; real fills are counted separately.
- E0 is not reset from the available balance, which shrinks with every entry order. Neither an extra deposit nor a process restart resets the campaign loss budget. Track C's KRW capital is not part of this USDT target.
- The 100% loss allowance is a budget. It does not guarantee a stop fill price, an amount returned after liquidation or a loss cap, and it is not an instruction to wait for liquidation on purpose.

## Differences from the earlier comparison profile

| Item | Preserved comparison profile | New owner target profile |
|---|---:|---:|
| Default / maximum leverage | 3 / 5 | 10 / 10 |
| Total notional cap / E0 | 2 | 10 |
| Margin cap / E0 | 40% | 100% |
| First-entry loss budget / E0 | 1% | 100% |
| Combined stop risk / E0 | 3% | 100% |
| Campaign loss budget / E0 | 10% | 100% |
| First target / total notional budget | 20% | 10% |
| Later accumulation | Add while in profit | Variable tranches on pullbacks, compression and pre-breakout pressure |

`full_seed_10x_preposition_research` stays as a comparison profile that enters the whole budget at once; the current adaptive accumulator does not use it. The new configuration is `accumulation.paper.json`. It allows buying below the average price as long as the original seed, notional and margin budgets are not exceeded. It does not buy just because the price fell, and it does not multiply order size as the loss grows. The existing UTA strategy keeps its ban on adding while at a loss. Neither the principal nor the paper stop is reset, and nothing re-enters automatically after the campaign ends.

## Entry conditions and judging the target

Leverage and a loss budget cannot tell in advance when a rally will start. The current paper accumulator uses the latest completed 1-minute candle, the range box and volatility of the 30 candles before it, up to the 100 most recent public trades and the current order book. It looks for a close that recovers or a lower wick on the latest candle, together with the share of buy trades. It does not buy on order book depth alone.

- The first 10% target is taken in small size. Fees, slippage, book depth and recent volume can shrink the actual paper order further.
- On a pullback at least 0.5 ATR below the previous buy price, if sell pressure is seen to ease, it accumulates in steps of at most 10%. The cumulative target for this stage is 70%.
- In a volatility contraction where the low holds, it accumulates up to 85%.
- In the top 20% of the box, when the buy-trade and bid-quote ratios strengthen together, it speeds up in steps of at most 25% to fill the remaining budget. It does not chase a price that is already above the box.
- A signal from one completed candle is used only once. The paper stop is not lowered when adding, and the buffer to the estimated liquidation price is checked as well. If these conditions keep the target from being reached, the shortfall is reported as it is.

The numbers are research defaults that have not been validated. The accumulator is not described as fixed-amount buying on a schedule or as an algorithm that estimates the statistical probability of a bottom.

Follow-up research should first fix the definition of a full rally used for evaluation and the time it is observed. It should then evaluate together the real held notional / target notional just before that time, liquidation or exit before the rally, maximum adverse excursion, costs and the case where no rally comes. Future candles may be used to evaluate a rally period, but that label is never passed back to past entry decisions. Size cut by risk, cash or liquidity limits is marked as a shortfall, and holding a small position is not grounds for declaring the target met.

## Implementation scope

`execution/bitget_classic_v2.py` checks the account, positions and open orders with authenticated GETs. The initial budget is fixed only once, on an account with no positions or reservations, and stored together with a hash identifying the key and the configuration hash. Restarts and deposits do not change E0. `strategy/accumulation.py` computes signals and sizes, and `accumulation.py` stores public market data, paper fills, errors and operating state in a separate SQLite database. Real positions are not imported into the paper ledger, and the real account's own observation time is shown separately.

Paper fills are computed by walking the observed asks. Funding is an estimate from the last observed rate and the current mark price, and the paper campaign stops if an observation gap spans a settlement time. A paper stop is recorded as an exit signal and does not guarantee a real exit fill. These results are not live performance tested against exchange latency, quote cancellations or market impact.

Slack notices use the existing `common.notify` format and delivery path. Each card stores the price, quantity, average entry price, cumulative margin as a share of the initial seed and remaining budget as of that paper fill, and shows `모의 체결` ("paper fill") in its title and body. An event key prevents ordinary duplicates. When a send result is unclear, the notice is not resent and is left for checking. The Track A/B and Track C dashboards are not attached to this USDT card. No separate Slack daemon or Track C engine restart is needed.

An authenticated order sender and live operation are not included. This work does not change the operating state of Tracks A, B or C.
