# Bitget UTA v3 public-data capability record

Status date: 2026-09-07. This is a public-data inventory, not account or
execution validation. The collector uses only unsigned `GET` requests and has
no API-key, private endpoint, order, or account capability.

Bitget's [2026-06-22 listing notice](https://www.bitget.com/support/articles/12560603886485)
recorded 20x maximum leverage, four-hour funding, USDT settlement, and tick
`0.00001`, while explicitly warning that parameters may change. The table and
collector therefore use the runtime instrument/funding/tier responses; the notice
is historical evidence only.

| Capability | UTA v3 public route | ARX futures | ARX spot | Status |
| --- | --- | --- | --- | --- |
| Instruments | `/api/v3/market/instruments` | `category=USDT-FUTURES`, `symbol=ARXUSDT` | `category=SPOT`, `symbol=ARXUSDT` | documented route; identity must be observed, not assumed |
| Tickers | `/api/v3/market/tickers` | yes | yes | documented route; exact requested category/symbol must be returned; BTC/ETH are separate benchmarks |
| Book snapshot | `/api/v3/market/orderbook` | yes | yes | documented snapshot; reconnect and sequence continuity are not inferred |
| Current funding | `/api/v3/market/current-fund-rate` | yes | no | documented for futures, including interval and next-update fields |
| Funding history | `/api/v3/market/history-fund-rate` | yes | no | documented `cursor`/`limit` route; records are in `data.resultList` |
| Open interest | `/api/v3/market/open-interest` | yes | no | documented public snapshot route |
| Candles / public trades | `/api/v3/market/candles`, `/api/v3/market/fills` | market/mark/index at `4H`,`1H`,`5m` | market at `4H`,`1H`,`5m` | documented routes; only elapsed candles are labelled completed |
| Position tiers | `/api/v3/market/position-tier` | yes | n/a | public tier list; tier/min/max/leverage/MMR are retained |
| Liquidations | `/api/v3/market/liquidations` | yes | n/a | public three-day history; `data.list` and cursor are retained |

The [Bitget current-funding API](https://www.bitget.com/api-doc/uta/public/Get-Current-Funding-Rate)
documents the futures categories, decimal funding rate, interval, and next
update timestamp. Bitget's [UTA changelog](https://www.bitget.com/api-doc/uta/changelog)
is the source for the documented v3 market-route names and records that
liquidation was added as a websocket channel; it is not evidence that ARX has
current liquidation events or that any account-level capability works.

The 2026-09-07 audit observations are ARX futures online, tick `0.00001`,
quantity step/minimum `1`, minimum notional `5`, maximum leverage `20`, funding
interval `4`, maker/taker `.0002`/`.0006`, and tier 1 `0–5000`, leverage `20`,
MMR `.025`. These, including spot identity and contract-address material, are
observations rather than guarantees.

Identity is strict: `ARXUSDT` futures is separate from `ARXUSDT` spot. The current
instrument response states ARX base, USDT quote, `USDT-FUTURES`, perpetual and online;
it does not return a `settleCoin` field. USDT settlement/linearity and base-coin order
quantity are therefore explicit inferences from the documented UTA category and
order contract, not invented response fields. Conversion uses multiplier `1` only
under that documented contract while retaining `quantityMultiplier` and
`priceMultiplier`. The UTA order mapper uses current `qty`, `timeInForce`, and
lower-case `reduceOnly=yes|no`; Classic fields never enter that mapper. The separate
documented TPSL mapper emits a one-way partial-position stop as `side=sell`,
`reduceOnly=yes`, base-coin `qty`, `slTriggerBy=mark`, and
`slOrderType=market`, without `posSide` or `marginMode`. It does not sign or send
the payload and is not evidence that the target account accepts or retains it.

Private capability code exposes only injected signed GETs for
`/api/v3/account/settings` and `/api/v3/position/current-position`. Account settings
must explicitly show UTA `unified|hybrid`, level `isolated|basic`, `one_way_mode`, an
exact `assetMode=single_asset` value, and an ARXUSDT `isolated` symbol config. Missing
or `multi_assets` asset mode, Advanced, switching/upgrading, crossed,
hedge, missing symbol config, external exposure, or unknown auto-top-up blocks entry.
No signer or private POST implementation exists in this development build.
Before live review, a non-production capability test must verify strategy-order
acceptance, query-visible coverage after partial fills, trigger semantics,
reduce-only reservation/cancellation behavior, and coexistence with other exits.
