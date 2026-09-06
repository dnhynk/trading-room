# Migration plan

## Existing state assessment

Trading Room already contains paused Bitget Tracks A/B, a paused Coinone spot Track A-2, and an independently operating Coinone Track C. No existing `ARX Spot Guardian` source, configuration, position, order, or ledger was found in the repository. The local source checkout also contains unrelated uncommitted Track C public-export work; the ARX implementation is developed in a clean Orca worktree so those files are not copied, staged, or overwritten.

The deployed C process remains owned by its existing operational session. This project does not restart, redeploy, pause, or edit it. A/B stop controls remain untouched.

## Target layout decision

The requested standalone `arx-futures-campaign` layout is represented inside this monorepo as `track_special/`: `track_special/arx_campaign/` replaces standalone `src/arx_campaign/`, while `configs/`, `docs/`, tests, and scripts remain track-scoped. This avoids a nested Python project and lets the repository-wide regression command validate the feature. State output remains outside the repository under `TRADING_ROOM_HOME/track-special-arx/` or sibling `trading-room-state/track-special-arx/`.

## Migration phases

1. Freeze the data, risk, order, protection, and ledger contracts. Register a disabled special track without changing the active focus.
2. Implement public UTA v3 instrument/market collection and normalized append-only storage. Distinguish ARX spot from ARXUSDT USDT perpetual and store current capabilities as observations, not constants.
3. Implement deterministic strategy, transactional risk reservations, ledger allocation, paper execution, replay, Korean reporting, and failure scenarios. Private writes remain impossible outside a separately reviewed live adapter.
4. Run repository tests and a public-data collect/store/read proof. Record real-public versus fixture evidence outside source state and summarize limitations in the validation document.
5. Independently review spot-code residue, API-family mixing, aggregate pyramid risk, liquidation/funding accounting, protection races, and look-ahead. Correct findings before the integration PR.
6. Leave live blocked. A later explicit promotion needs approved capital/risk/null settings, authenticated read-only account verification, account separation, actual server-side protection capability testing, fixed leverage while flat, and a separate live change review.

## Non-migrations

- Do not convert, close, sell, transfer, or relabel any spot balance.
- Do not import A/B campaign PnL, pool claims, orders, or protection records.
- Do not import C KRW equity, model, data, ledger, baseline, or Slack cursor.
- Do not copy the research profile into live configuration.
- Do not enable shorts, hedge mode, cross margin, multi-asset collateral, borrowing, automatic top-up, transfers, or automatic withdrawals.

