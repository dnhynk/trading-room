# Special track guardrails

- This directory is a separate research track for ARX USDT-linear perpetual futures. Do not read spot positions, orders, ledgers, capital pools or stop files from Tracks A, B or C and turn them into futures state.
- The default mode is `observe` with `live_enabled=false`. During development, tests and public data collection, make no authenticated writes, no orders, no changes to account, margin or position settings and no transfers, deposits or withdrawals.
- Do not mix UTA v3 and Classic v2 request or response fields in one object or request. If private capabilities cannot be checked against the real account, block promotion to live.
- Compute amounts, prices, quantities and ratios with `Decimal`. Do not turn float inputs into money values implicitly.
- Record intent, risk approval, order reservation, fill, protective order, fee, funding and reconciliation as separate persistent events. An order acknowledgement is not a fill or confirmed protection.
- Reducing risk comes before opening or adding. Do not implement adding while in loss, lowering protection, martingale sizing, automatic margin top-ups, cross-margin workarounds or flipping to short. The paper accumulator's buys below the average price are an explicit owner exception for that strategy only (see `configs/README.md`).
- Store recordings, ledgers and experiment output in `TRADING_ROOM_HOME` or the sibling `trading-room-state/track-special-arx/`. Do not commit real collection results to the source tree.
- Keep synthetic and fixture tests apart from real public-data evidence. A successful public query is not evidence of profitability, account capability or live readiness.
