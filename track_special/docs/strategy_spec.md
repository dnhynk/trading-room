# ARX strategy specification

The pure strategy consumes completed, timestamped bars only.  A probe requires a confirmed box breakout, a pivot whose right-side bars are already closed, volatility contraction, and relative strength; it emits an intent rather than an order. Adds require a different completed-bar confirmation and an existing position profitable after costs, so no loss recovery or time-based catch-up is possible.

State is deterministic: `WATCH → PROBE → BUILD → RIDE → HARVEST → FLAT → COOLDOWN`. Harvest fractions are exactly 15%, 15%, then 70% of the reference quantity. Protective stops are monotone: a lower proposed protection is rejected.
