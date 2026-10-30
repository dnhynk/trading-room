# Agent guardrails

- Read `README.md`, `config/tracks.json` and the README of the track you are changing first. The published configs are examples and do not show the real operating state.
- Track contracts live in the track READMEs, numbers in the configs and behavior in the code. Track A rotates a basket and Track B runs independent campaigns; neither inherits the other's strategy rules.
- Never start, redeploy or restart a supervisor or live trading because of a code change, a cleanup or a test run.
- Fix real defects with the smallest change, then run `python -m unittest discover -s tests -t .`. For a Track A or B strategy change, also compare `track_a.backtest` on the same input tape when one is available.
- Do not print, send or commit `.env` files, keys, server identifiers or real account data. Do not touch the owner's positions, orders or funds. Keep order ownership, protective orders, recovery and partial-fill reconciliation intact.
- Do not overwrite a frozen model, an evaluation registration, a ledger or a displayed baseline. A changed model or evaluation gets a new identifier and output path.
- Do not add automatic retraining, size increases or a switch to live trading. Do not loosen thresholds, symbols, cash or risk limits to get more trades.
- Do not call a public replay result live execution evidence or a P&L floor. Keep observation gaps, partial fills and leftovers from the same episode as dependent samples.
- Notification relays are configured by the operator. Keep the Track C KRW ledger and the Track A/B USDT ledger apart. Show KRW to one decimal place and keep full precision internally.
- Check that a book is flat in the same step that stops its process. On Windows, confirm the absolute path and scope inside the same command that deletes or moves files.
- Recordings, ledgers and experiment output go to the external state directory, and one-off scripts go to the OS temp directory. Do not add dated documents or one-off deployment scripts.
