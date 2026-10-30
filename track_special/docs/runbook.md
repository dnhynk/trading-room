# ARX runbook

The default mode is `observe`. `python -m track_special doctor` checks the configuration and the no-write boundary. `collect` and `observe` only make unsigned public GET requests and append to external state. `paper` and `replay` make no exchange writes, and their synthetic results are labelled as not being evidence of profitability.

`pause-entries` blocks new entries only, `cancel-entry-orders` records a cancel request for entry orders, and `request-exit` switches to EXIT_ONLY. In a development build, `validate-live` always reports the track as not operational and changes no account, margin, position or leverage settings.

A protection gap, a database error or a request with an unknown result stops new entries. An ACK does not confirm a fill or protection; after a timeout, the same clientOid is reconciled first. A `RESULT_UNKNOWN` order is never resent until reconciling its clientOid with the exchange shows either a terminal state or a not-found result recorded with its time limit.

Every process starts with `startup_reconciled=false`. Entry reservations can open only after a REST snapshot confirms the ARX position, regular and strategy orders, the protected quantity and the absence of external orders and positions, and the later increments have been merged in. A cancel request made while a result is unknown does not overwrite the state with `cancel_pending`; the order stays waiting for reconciliation. The current no-live commands report locally-canceled, cancel-pending and reconciliation-deferred separately and do not claim that the exchange completed a cancel.

The state database lives only at an absolute path under `TRADING_ROOM_HOME` or the repository's sibling `trading-room-state`. The process lock is taken before the database is opened. A leftover lock is not presumed stale and deleted; the operator reconciles its owner. Protection counts as complete only when the order ID, an active lookup on the exchange, the trigger basis, the covered quantity and the valid time are all present.

Reductions always take priority over new entries and increases. `pause-entries` (stops new entries only), `request-exit` (EXIT_ONLY), `emergency-halt` (emergency stop) and `cancel-entry-orders` (existing entry orders wait for cancellation) are separate controls, and a cancel ACK alone does not complete position or protection reconciliation. `propose-risk-change` records only the hashes, the worst loss, the review delay and the pending approval in external state. It does not change limits or live trading.
