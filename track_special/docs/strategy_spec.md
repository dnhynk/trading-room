# ARX strategy specification

The pure strategy consumes completed, timestamped bars only. A probe is deliberately
pre-breakout: the signal must remain inside the box, above a fixed box-position
threshold, with a pivot whose right-side bars are already closed, volatility
contraction, and relative strength. A first spike above the box is not chased as a
probe. Adds require a newly completed breakout-hold or pullback-resumption plus an
executable, cost-adjusted profit on the whole existing position; a caller boolean is
not enough. No loss recovery or time-based catch-up is possible.

State is deterministic: `WATCH → PROBE → BUILD → RIDE → HARVEST → FLAT → COOLDOWN`.
The research comparison realizes 15% at 1R and 15% at 2R. The reference quantity
includes reconciled additions until the first harvest and is then frozen; partial
fills reduce the outstanding target, not the reference. The remaining 70% exits only
on trend invalidation, monotone trailing protection, funding/giveback rules, or a
holding/no-progress limit. R is fixed from entry risk and is never rewritten in the
campaign's favor. These thresholds and fractions are research inputs, not optima.
