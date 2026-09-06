# Bitget UTA v3 public-data capability record

Status date: 2026-09-07. This is a public-data inventory, not account or
execution validation. The collector uses only unsigned `GET` requests and has
no API-key, private endpoint, order, or account capability.

| Capability | UTA v3 public route | ARX futures | ARX spot | Status |
| --- | --- | --- | --- | --- |
| Instruments | `/api/v3/market/instruments` | `category=USDT-FUTURES`, `symbol=ARXUSDT` | `category=SPOT`, `symbol=ARXUSDT` | documented route; identity must be observed, not assumed |
| Tickers | `/api/v3/market/tickers` | yes | yes | documented route; exact requested category/symbol must be returned; BTC/ETH are separate benchmarks |
| Book snapshot | `/api/v3/market/orderbook` | yes | yes | documented snapshot; reconnect and sequence continuity are not inferred |
| Current funding | `/api/v3/market/current-fund-rate` | yes | no | documented for futures, including interval and next-update fields |
| Funding history | `/api/v3/market/history-fund-rate` | yes | no | documented `cursor`/`limit` route; records are in `data.resultList` |
| Open interest | `/api/v3/market/open-interest` | yes | no | documented public snapshot route |
| Candles / public trades | `/api/v3/market/candles`, `/api/v3/market/fills` | yes | yes | documented routes; only elapsed candles are labelled completed |
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

Identity is strict: `ARXUSDT` futures is separate from `ARXUSDT` spot and is
accepted only after its returned instrument states complete base, quote,
settlement, perpetual/linear and online fields. UTA v3 reports futures order
quantity in base coin; conversion therefore uses multiplier `1` only for that
documented inference while retaining `quantityMultiplier` and `priceMultiplier`.
