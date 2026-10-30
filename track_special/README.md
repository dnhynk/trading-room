# Special track: ARX futures campaign

This is a separate research track for long-only positions in the ARX USDT-linear perpetual future (`ARXUSDT`) on Bitget. The design goal is to build a long position in tranches, up to 10 times a margin budget (E0) that is set from the account's available USDT. The maximum loss is bounded by that margin budget. Buying below the average price is allowed, but it raises neither the margin budget nor the 10x cap. The current implementation reads the account through the Classic v2 API, paper-runs adaptive accumulation on public market data and reports paper fills to Slack. The capital definition and the implementation scope are in the [pre-positioning objective](docs/preposition_objective.md).

The earlier `aggressive_bounded_research` profile and the UTA strategy that builds a position only while in profit after a small probe stay as comparison baselines. `full_seed_10x_preposition_research`, a single order for the whole budget, is also kept for comparison; the current accumulator does not use it. The default run configuration stays in observe mode.

This track is not a renamed Track B. It imports no Track B positions, orders, ledgers or capital pool, and it has no link to Track C's Coinone KRW ledger or processes. The current operating contract is `observe` with `live_enabled=false`. Development sessions place no real orders, change no account or margin settings and move no funds.

## Boundaries

- Data: ARX spot and `ARXUSDT` futures are stored as separate series, keeping exchange and receive times, order, duplicates and gaps.
- Strategy: the states `WATCH → PROBE → BUILD → RIDE → HARVEST → FLAT → COOLDOWN` emit intents and do not place orders themselves.
- Risk: the existing position and every in-flight, open or unresolved reservation are summed in one transaction. Principal loss, profit giveback, gross stop risk, notional, isolated margin and the liquidation buffer each have their own limit.
- Execution: the risk gate's short-lived approval is checked again right before sending. Acknowledgement, fill and confirmed protection are separate states.
- Ledger: returned margin, liquidation notional and unrealized profit are not booked as realized profit, and realized fees and funding are deducted once.

## Commands

`collect` and `observe` call unsigned public GET endpoints only. The separate `accumulate` command checks the account with Classic read-only keys from the given env file and runs paper accumulation on public data. `<state-root>` below is an absolute path outside the repository, such as the sibling `trading-room-state` directory.

```bash
python -m track_special doctor
python -m track_special --state-directory <state-root>/track-special-arx/marketdata collect
python -m track_special --config track_special/configs/paper.yaml paper
python -m track_special replay --scenario gap_collapse
python -m track_special status
python -m track_special report
python -m track_special pause-entries
python -m track_special cancel-entry-orders
python -m track_special request-exit
python -m track_special emergency-halt
python -m track_special propose-risk-change --change leverage --before 3 --after 4 --worst-loss UNKNOWN
python -m track_special --config track_special/configs/live.example.yaml validate-live
python -m track_special --state-directory <state-root>/track-special-arx/adaptive-accumulation accumulate --env-file <path-to-.env> --notify-slack
python -m track_special --state-directory <state-root>/track-special-arx/adaptive-accumulation accumulation-status
python -m track_special --state-directory <state-root>/track-special-arx/adaptive-accumulation stop-accumulation
```

`validate-live` lists the null settings, the missing user approval, the unverified authenticated account and positions, the unverified server-side protection and the missing live adapter, then exits with code 2. It is a diagnostic: it makes no private API calls and changes no settings. The current contracts and migration baseline are [architecture_contracts.md](docs/architecture_contracts.md) and [migration_plan.md](docs/migration_plan.md). Operating steps are in the [runbook](docs/runbook.md), and the last validation is in the [validation report](docs/validation_report.md).

## Adaptive accumulation

The settings are in `configs/accumulation.paper.json`. Buy size depends on four conditions: taking a small first position, easing sell pressure on a pullback, shrinking volatility, and buy pressure near the top of the recent range. The accumulator does not buy on a timer and does not put the whole budget into the first order. Failed lookups, a thin book or few trades, unfinished candles and signals already used are not grounds for another buy. Reaching the target before a rally is not assured, and neither is profitability. The current paper fills are not evidence of real fills.
