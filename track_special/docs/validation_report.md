# ARX validation report

Validation reference time: 2026-09-07 KST. Validating the code paths is separate from validating the strategy's profitability, and a pass in this report does not mean a current buy signal or an expected return.

## Evidence from real public data

With no API keys and only unsigned public Bitget UTA v3 `GET` calls, one collect, store and read-back cycle was completed. The final result is kept outside the repository in the state directory `trading-room-state/track-special-arx/marketdata-final-audit`.

- First and last receive: `2026-09-07T02:51:25.571540+09:00` / `2026-09-07T02:51:27.988017+09:00`
- 27 streams, 1,517 records stored, 1,517 records read back independently
- Coverage of 1,505 records: the last, still-open candle is left out of each of the 12 candle streams
- The default research axes are spot market `4H/1H/5m` and futures market/mark/index `4H/1H/5m`
- 0 endpoint errors; liquidation history returned an empty response and was recorded as 1 gap
- Both the ARX spot and the `ARXUSDT` USDT perpetual identities were verified
- Private requests, orders, account or leverage settings, transfers: 0
- Receipt: `trading-room-state/track-special-arx/marketdata-final-audit/snapshot_receipts.jsonl`

The public responses at the time showed the future as online, with price tick `0.00001`, quantity step and minimum `1`, minimum notional `5 USDT`, maximum leverage `20`, funding interval `4`, maker/taker `.0002`/`.0006`, a tier 1 range of `0–5000` and MMR `.025`. These are observations from that moment, not a guarantee of live settings. The instrument response had no `settleCoin`, so the USDT-linear contract and the base-coin quantity were determined explicitly from the documented `USDT-FUTURES` category together with the order contract.

## Code and execution checks

- `python -m unittest discover -s tests/special -t .`: 65 tests, OK
- `python -m unittest discover -s tests -t .`: 643 tests, OK
- strict mypy: 29 source files, no issues
- `python -m compileall -q track_special`: passed
- `git diff --check`: passed
- `doctor`: observe, credentials read false, exchange writes 0, live false
- `paper`: exchange writes 0; requires an explicit synthetic intent and risk approval
- `replay --scenario gap_collapse`: reports a synthetic gap, liquidation and protection failure as losses
- `status`: reports the unfinished startup reconciliation as `startup_reconciled=false`
- `validate-live`: exits with an error after 0 private requests and 0 writes, and lists every blocker

The synthetic tests check the separation of intent, reservation and ACK/fill, a deterministic 32-character `clientOid`, blocking a resend after a timeout until reconciliation, partial-fill and cancel races, monotonic state transitions, protection gaps and control states. Protection becomes active only when the server query, order ID, active status, trigger basis, covered quantity, stop price and freshness/deadline are all confirmed. The serializers for regular orders and TPSL protection orders follow the official UTA shapes; they are pure functions and cannot sign or send.

An independent review agent found two paths: one where an ambiguous or rejected UTA response could be mistaken for an ACK, and one where `assetMode=multi_assets` slipped past the single-collateral check. The final implementation requires a success code, the exact `clientOid` and a non-null `orderId`. Codes the documentation leaves ambiguous become `RESULT_UNKNOWN`, which blocks a resend until reconciliation. The account snapshot explicitly requires `assetMode=single_asset`, and automated tests reproduce both regressions.

Replay accounts for timestamps, mark and last prices, explicit funding settlements, fees, spreads, latency, depth-based partial fills and non-fills, unprotected exposure and adverse ordering within the same candle. It also includes separate synthetic scenarios for a long decline, no pump, a spike after only the probe filled, a fake breakout, a gap collapse, mark/last divergence, a funding spike or interval change, a trading halt, a rejected server stop, orders left after liquidation, ADL and a duplicate order on restart.

## Not verified, and why live is blocked

The public and synthetic replays lack enough ARX history of mark prices, order books, funding, tier changes and real fills. So although there are interfaces for comparing cash, spot, no-pyramid and pyramid variants, they do not show profitability, avoidance of forced liquidation or executability.

For the authenticated account, none of the following was checked: whether it is UTA or Classic, the isolated/basic combination, one-way mode, blocking of automatic margin top-ups, separation into a dedicated account, external orders and positions, the real position tier and liquidation price, and how TPSL acceptance, lookup, partial-fill coverage and reduce-only races behave. The live transport and the private POST signer are not implemented. The capital, leverage, risk, funding and holding-period, liquidation-buffer, emergency-exit, end-date and approval metadata in `live.example.yaml` are all unapproved or null. Live is therefore blocked on purpose, and this development work alone cannot promote it.
