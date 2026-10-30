# Track B: coin-episode campaigns on Bitget futures

The published configuration is paused and in dry-run mode. Leave Track B's state in `config/tracks.json` and any operator `STOP` or `PAUSE` file as they are; only the operator lifts them. Settings are `hunt` and `hunt.strat` in `config/bitget.json`, and running Track B needs a separate profile with `hunt.on=1`. AI calls are off in the example configuration. Tracks A and B share the account and the execution engine, but Track B's strategy is its own contract.

## Strategy contract

The track looks for coins in a live pump episode, read from footprints in traded value, volatility, funding, the spot market and the coin's phase. It starts a campaign on a confirmed slowdown. Eligibility decides whether a coin is held; rank decides the order in which empty slots are filled. A held coin is kept even when its rank drops. The track may watch several opportunities, but capital comes from the existing pool, first come, first served.

The current baseline is one unit and a pool of one, with a loss budget sized to equity. The default money stop sits below the average price by the per-unit limit divided by the reference unit quantity. The unit count alone does not tighten that distance, and the nominal budget does not bound the worst real fill loss. When the ATR distance is too short, the size shrinks. Size stays under the risk cap, and the track skips an entry it cannot fund at the exchange minimum. Exchange protective orders and the daily stop and loss limits apply to the whole pool.

A process exit, an expired lease or a failed read is not treated as free capital. The track opens a new campaign only after confirming the other books' holdings and open orders. The independent unit of observation is one campaign from flat to first fill to flat. A partial fill is not a new sample, and incomplete, missing or manually closed campaigns stay in the evaluation.

## AI phase reading

The optional AI phase reader (`ai_read`, off in the example configuration) runs `codex exec` in a read-only sandbox. Its phase and side replace the rule reader's only when a strict majority of the configured runs agree. Failed calls stay in the denominator, and without a majority the rule reading stands. The eligibility gate stays in rule code. If the configuration changes during a scan, that scan's decision is dropped. Agreement rates and scan close-price paths are not hit rates or live stop rates.

## Code

| Path | Role |
|---|---|
| `hunt.py`, `whale.py` | Eligibility, phase reading and campaign candidates |
| `campaigns.py`, `phases.py` | Studies of past scans and phase paths |
| `research.py`, `replay.py` | Campaign ledger audit; comparison under equal cost, opportunity and capital |
| `common/cycle.py`, `common/risk.py` | Shared execution, capital pool, cumulative loss and protective orders |

## Checks

`python -m unittest discover -s tests/b -t .` runs the Track B tests. A change is adopted only after a check across dates, symbols, independent episodes, cost stress and chronological validation. Periods already looked at are development data, and a small sample is not a reason to raise risk or to apply a change automatically. The public copy contains no real campaign ledgers and no earlier operating history.
