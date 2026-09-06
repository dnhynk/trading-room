# ARX risk constitution

Risk evaluates filled long lots and all active entry reservations at their worst fills against one conservative stop. Principal loss, current-profit giveback, gross stop loss without profitable-lot offsets, notional, isolated margin, stage cap, funding, liquidity, available collateral, precision, and liquidation buffer are separate gates. Candidate fees and funding scale with an approved reduction in size; previously booked lot fees/funding are not deducted again. Approval floors arbitrary quantity steps and refuses a size below the quantity or notional minimum.

Daily, weekly, drawdown, campaign, and loss-streak state are durable inputs. External
flows adjust equity rather than PnL. Every trigger enters `EXIT_ONLY`; daily/weekly
resume requires an explicit eligibility call after its boundary, streak resume also
requires the cooldown, and campaign/drawdown halts are permanent in this build.
Restart never clears them. Ledger allocations are solely 25/75 increments above the
positive realized-net high-water and never treat recovery, deposits, margin returns,
or unrealized PnL as profit.
