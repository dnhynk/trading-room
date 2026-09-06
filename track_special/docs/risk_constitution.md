# ARX risk constitution

Risk evaluates filled long lots and all active entry reservations at their worst fills against one conservative stop. Principal loss, gross stop loss, notional, isolated margin, stage cap, and liquidation buffer are separate gates; approval rounds down to precision and refuses a size below the minimum.

Daily, weekly, drawdown, campaign, and loss-streak state are durable inputs. A triggered loss streak is `EXIT_ONLY`; it never self-clears merely because a process restarts or a calendar rolls over. Ledger allocations are solely 25/75 increments in positive realized-net high-water and never treat deposits, margin returns, or unrealized PnL as profit.
