# Track C multivenue study

This package compares raw Coinone, Upbit and Bithumb sessions recorded by Track A-2 using Track C's market state and public-queue replay. It is an offline research boundary with no authenticated client, no order client and no live mode. It sits outside `track_c/` because the frozen Track C BTC model hashes every Python file under `track_c/`.

## Input handling

The loader leaves the original gzip files and manifests unchanged. It verifies each file's SHA-256 and then reads events in the `sequence` order of the single collector process. It does not re-sort by `received_ms` or by venue priority. Each hourly session starts after every venue reconnected, so counterfactual orders still open at a session boundary are censored and the market and reference state is reset. Session boundaries are not presented as a continuous market or as independent samples.

## Policies

The comparison policies are fixed in advance.

| Policy | Entry condition known before the decision |
|---|---|
| P0 | A new Track A-2 `DIP_SLOWING` signal, inside Track C's shared observation and order-management conditions |
| P1 | P0 plus Track C's external-discount eligibility |
| P2 | A new Track C local sell episode plus external-discount eligibility |
| P3 | P2, with a Track A-2 slowdown observed by the same decision time and still within its time-to-live |

P0 freezes the Track A-2 slowdown entry condition as a comparison group. It still uses Track C's external reference readiness and floor for shared order creation and open-order handling, so it differs from the earlier Track A-2 strategy, which used no external data. All four policies use the same `0:minimum` candidate and the same fee, latency, depth and exit assumptions, so they differ only in the entry information. The union of all triggers and each policy's reasons for not selecting are recorded too.

A separate `local_minimum_passive_fill_markout` diagnostic keeps candidates with an uncertain external reference from disappearing silently at the shared order gate. It does not relax the external safety gate. It is a non-trading measurement that compares simulated fills of a local minimum-size limit order, and the executable markouts at 1, 5, 30 and 120 seconds, across external reference states.

## Results and censoring

Session results are split into completed cash P&L, leftover inventory valued at a fresh book, leftover inventory that cannot be valued, and a cash-recovery stress that values leftovers at zero. The censoring rate and the share of principal left in censored inventory are shown next to them, and no single average picks a winning policy. Before the scheduled end of a one-hour file, new shared entries are blocked for the length of the worst fixed execution path. Censoring in a session that ended earlier than expected is kept as is.

A decision happens at the end of its `received_ms` bucket, at `decision_bucket_ms + 1` ms. The study checks that the last included receive time is earlier than the decision time in nanoseconds, and order latency is counted from that decision time.

Individual candidates are overlapping counterfactual orders, so their summed P&L is not account return or turnover. Results are saved even when they lose money and are always `RESEARCH_ONLY` with `orders_enabled=false`. `COMPLETE` means the computation finished. It does not approve an alpha or pre-register a future window.

## Commands

```bash
python -m track_c_multivenue --session <finalized-session>
python -m track_c_multivenue --session <session-1> --session <session-2> --output ../trading-room-state/track-c-multivenue/study-001
python -m track_c_multivenue --register-output ../trading-room-state/track-c-multivenue/registrations/future-001.json --start-ms <future-start-ms> --end-ms <future-end-ms>
python -m track_c_multivenue --session <future-session> --output ../trading-room-state/track-c-multivenue/future-001 --registration ../trading-room-state/track-c-multivenue/registrations/future-001.json
```

Both times are epoch milliseconds, and `--start-ms` must be later than the moment you register.

A receipt written just before an evaluation proves only that the run's inputs were frozen. `REGISTERED_BEFORE_WINDOW` requires the source, contract, period, primary metric, censoring and exclusion rules to be written to a separate file before the window starts, and accepts only sessions that fall entirely inside that window. This local file is not an external timestamp and does not prove that no session was left out, and the report states that limit.

Output goes only to `TRADING_ROOM_HOME` or the sibling `trading-room-state/` directory, outside the repository. External prices are not back-filled into older Coinone-only tapes, and results share no identifiers with the existing Track C BTC model, evaluations or ledgers.
