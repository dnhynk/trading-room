# Track A: basket rotation on Bitget futures

The published configuration is paused and in dry-run mode. Leave Track A's state in `config/tracks.json` and any operator `STOP` or `PAUSE` file as they are; only the operator lifts them. Settings live in `config/bitget.json`, and Track A is the `hunt.on=0` path. Tracks A and B share one account and the common execution engine, so do not resume both at once.

## Strategy contract

The track adds as an adverse move slows and trims as a favorable move stalls, cycling the average price and realized P&L. Long and short sides are symmetric. An add happens only on a slowdown at a ladder step better than the previous buy. A trim respects the new lot's cost and the round-trip trading cost. The last lot can also be closed on a stall. Every add needs a slowdown.

The traded symbols form a basket chosen to cover trading costs and produce enough cycles. Rank only decides which empty slot to fill; it does not replace a held book. When a symbol loses eligibility, the track stops adding, closes the book with the normal exit rules and then drops it. Weights are equal by default, and a weight change needs a same-period live comparison.

The track keeps the exchange-side money stop sized to equity and a daily loss cap. It opens no new risk while open orders, holdings or another book's state cannot be confirmed. Realized P&L, fees, partial fills and capital transfers are kept apart. An experimental feature that cuts exposure on a weak rebound during a loss is off in the example configuration.

## Code

| Path | Role |
|---|---|
| `select.py`, `scan.py` | Basket eligibility, selection and wind-down |
| `cycles.py` | Cycle statistics rebuilt from real fills |
| `backtest.py`, `replay.py`, `tune.py` | Replay, chronological comparison and explicit parameter experiments |
| `capture.py`, `recon.py`, `legs.py`, `pair.py`, `slip.py`, `sweeps.py` | Fill and price-path analysis |
| `common/signal.py`, `common/cycle.py` | Signals, orders and position handling shared with Track B |

## Checks

`python -m unittest tests.common.test_signal tests.common.test_cycle tests.a.test_select` runs the core regressions. A tape you hold separately replays with `python -m track_a.backtest <tape path>`. A result on a tape that was already looked at counts as a development check only. The public copy contains no real recordings.
