# Trading Room

Trading Room records crypto market data, replays it through the trading code, and places orders for several strategies on one operator's Bitget and Coinone accounts.

On the Coinone tracks, an order's intent is committed to SQLite under a client order ID before it is sent, and the decision behind it is checked again against fresh market and account data.

```console
$ python -m track_a_2.live --check
{"track": "A-2", "configuration": "valid", "mode": "live", "live_ready": false,
 "blocks": ["track_registry_not_active", "track_registry_execution_disabled",
  "config_not_active", "config_execution_disabled", "portfolio_isolation_unconfirmed",
  "evaluation_approval_missing", "evaluation_manifest_missing", "universe_empty",
  "egress_ip_unconfigured"]}
```

That is the published configuration, checked with no credentials and no network access (output wrapped to fit). Each entry in `blocks` is a separate lock that has to be released before Track A-2 can trade.

I built this alone between August and September 2026. I wrote the specifications and reviewed the changes; Claude Code and Codex implemented them. This public copy is no longer updated. Every track is paused or observe-only, and profitability has not been verified.

## What it does

Each strategy is a track: a package with its own configuration, rules and tests. Tracks A and B share one Bitget account and engine. The other tracks keep their own ledgers and state directories. Track A-2 uses its own Coinone keys unless the owner approves a shared portfolio, and the special track reads Bitget keys (the same variable names as Tracks A and B) only to check the account for paper runs.

- Records public order books, trades and connection events from Coinone, Upbit, Bithumb and Bitget. A Coinone session that also records Upbit and Bithumb stamps every message with one shared receive sequence, so research code reads events in arrival order.
- Replays recorded Track A-2 sessions through the same market, strategy, runtime and order-manager code that trades live, under stated latency, queue and depth assumptions.
- Places and tracks orders on Coinone KRW spot and Bitget USDT futures. The Coinone tracks reconcile the account against their own journal: cumulative fills are applied once, a cancel counts only after the order detail confirms it, and an order with the track's ID prefix that the journal does not know halts the track.
- Labels replay and study output as research-only. An approved replay has no effect until the operator enters its path and SHA-256 in the config by hand.
- Lets Track B ask an LLM (`codex exec` in a read-only sandbox, off in the example config) for a coin's market phase. The label counts only when a strict majority of the configured runs agree, and the eligibility gate stays in rule code.

| Track | Market | Strategy | Published state |
|---|---|---|---|
| [A](track_a/README.md) | Bitget USDT futures | Basket rotation: add as an adverse move slows, trim as a favorable move stalls | paused, dry run |
| [B](track_b/README.md) | Bitget USDT futures | One campaign per confirmed coin episode, funded from a shared pool | paused, dry run |
| [A-2](track_a_2/README.md) | Coinone KRW spot | Long-only version of the Track A rotation | paused, execution disabled, no symbols |
| [C](track_c/README.md) | Coinone BTC/KRW spot | Values brief BTC dips against Upbit and Bithumb prices; minimum-size orders sample real fills | observe |
| [C multivenue](track_c_multivenue/README.md) | Recorded Coinone, Upbit and Bithumb sessions | Offline comparison of Track A-2 and Track C entry rules | research only, no order client |
| [Special](track_special/README.md) | Bitget ARXUSDT perpetual | Long-only accumulation research | observe, no live adapter |

## How it works

- Intent before send. `track_a_2/execution/oms.py` and `track_c/execution/oms.py` commit an `ORDER_INTENT` event to SQLite (WAL, `synchronous=FULL`) before the request goes out. The exchange clients accept only the track's own client order IDs (`ta2-…`, `tc-…`). After a lost response the order stays `UNKNOWN`, keeps its capital slot and is resolved by a lookup with the same ID; the track halts if the lookup fails past a deadline. `test_lost_response_is_recovered_by_identifier_without_duplicate_submission` in `tests/c/test_execution.py` restarts the order manager and checks that one submission was made.
- Recheck before send. `entry_invalid_reason` in `track_c/live.py` checks decision age, book age, account freshness, ownership and the chosen price, size and stop after the model runs. The transport repeats the check after rate-limit, connection-pool and TLS waits (`before_send` in `track_c/execution/coinone.py`, `rate_limit.py`, `http_pool.py`). An expired decision is logged with `transmitted=false` (`track_c/execution/oms.py`) and releases its cash reservation (`test_local_expiry_after_durable_intent_is_terminal_and_releases_reservation` in `tests/c/test_live.py`). Track A-2 passes a similar guard, `submission_guard` in `track_a_2/runtime.py`.
- Locks before credentials. `block_reasons` in `track_a_2/execution/preflight.py` checks the track registry, the track config, portfolio isolation, an approved evaluation or an explicit owner override, a non-empty symbol list, a fixed egress IPv4 address and the STOP/PAUSE files. `track_a_2/live.py` runs it before reading credentials, then looks up the public egress IP and runs it again. `tests/a2/test_settings.py` covers the checks. Track C's runtime stops before its first account request when the egress IP differs from `expected_egress_ip` (`track_c/runtime.py`).
- Results tied to source hashes. `sources()` in `track_c/learning/config.py` hashes every Python file under `track_c/` and `common/` plus `pyproject.toml`. Model loading and replay evaluation (`track_c/replay/evidence.py`, `track_c/replay/engine.py`) reject a model or evaluation whose recorded source, config or input hashes differ from the current ones. Track A-2 evaluation manifests carry a source digest of the same kind.
- Blocked by default. `config/tracks.json` marks every track paused or observe-only. In the special track, `LiveTransport` in `track_special/arx_campaign/execution/engine.py` has no signer and no HTTP write path, and `validate-live` lists the remaining blockers and exits with code 2.

Start reading here: `track_a_2/execution/oms.py` (877 lines), `track_a_2/execution/preflight.py` and `track_c/live.py`. The shared Track A/B engine is very large: `common/cycle.py` is about 100 KB and `common/signal.py` about 90 KB.

## Results

There are no trading results in this repository, and there is no CI. The checks below ran locally on 2026-10-31 with Python 3.12.14 on Windows 11 and on Ubuntu under WSL2.

| Check | Result |
|---|---|
| `python -m unittest discover -s tests -t .` | All 729 tests pass on Windows 11. On Ubuntu (WSL2), 728 pass and one Windows-only test is skipped |
| Same suite with non-loopback network blocked | Same result; no outbound connection attempts |
| `python -m mypy` (strict, `track_special/arx_campaign`, mypy 2.3.1) | No issues in 33 source files |
| `python -m track_a_2.live --check` | `live_ready: false`, 9 blocks |
| `python -m track_special validate-live` | 4 blockers, exit code 2, 0 private requests |

## Status and limitations

- Not updated here. Development continues in a private repository.
- Profitability is not verified. The repository holds no live P&L, fill rates or latency calibration. Replays use fixed latency and queue assumptions, and public data cannot show the queue ahead of a private order, hidden liquidity or market impact.
- Every track is paused or observe-only in the published configuration. Before a Coinone track can trade, it needs credentials, a fixed egress IP and explicit config changes. Track C also needs a model built from the current source, and Track A-2 needs an approved evaluation or an explicit owner override. The special track has no live adapter.
- The tests run on synthetic inputs and fake exchanges. They check the safety logic and say nothing about returns.
- The Track A/B process checks (`common/preflight.py`, `track_b/hunt.py`) call PowerShell, so that tooling assumes Windows.
- 39 of the 229 Python files contain Korean comments or strings, mostly in the older Track A/B code.

## Run it

Python 3.12 or newer. Nothing below needs API keys, and the unit tests make no outbound network calls.

```bash
git clone https://github.com/dnhynk/trading-room.git && cd trading-room
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
python -m pip install -e ".[dev]"                 # dev adds mypy 2.3.1
python -m track_a_2.live --check                  # prints the nine blocks shown above
python -m unittest discover -s tests -t .
python -m mypy                                    # strict check of track_special/arx_campaign
```

`python -m track_special validate-live` prints four blockers and exits with code 2. `python -m track_special replay --scenario restart_duplicate_order` runs one of twelve synthetic failure scenarios; its output is labelled `synthetic_failure_scenario_not_profitability_evidence`.

Runtime state (logs, recordings, ledgers, models) goes to a sibling directory, `../trading-room-state/`, or to `TRADING_ROOM_HOME`. Tracks A-2, C multivenue and Special reject state paths inside the repository.

## Docs

- [Track A-2](track_a_2/README.md): order, accounting, recovery and activation contract for Coinone spot
- [Track C](track_c/README.md): pre-send recheck, replay assumptions and evidence rules
- [Special track architecture contracts](track_special/docs/architecture_contracts.md)
- [Special track validation report](track_special/docs/validation_report.md)
- [AGENTS.md](AGENTS.md): guardrails for coding agents working in this repository

## Project context

The aim was to run several strategies on my own exchange accounts and to close five execution and validation gaps: resending an order after a lost response, acting on a stale decision, starting live before keys and network are ready, reusing a model after its code changed, and reading a replay as real fills.

Most development commits lack a Co-Authored-By trailer, so the commit log understates how much of the code the agents wrote.

Public snapshot of a private development repository, first published on 2026-09-06. Keys, server addresses, ledgers, market recordings, trained models and the private history are not included. The configs are examples and do not describe a real account.

## License

MIT. See [LICENSE](LICENSE).
