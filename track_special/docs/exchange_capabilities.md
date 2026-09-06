# Bitget UTA v3 public-data capability record

Status date: 2026-09-07. This is a public-data inventory, not account or
execution validation. The collector uses only unsigned `GET` requests and has
no API-key, private endpoint, order, or account capability.

| Capability | UTA v3 public route | ARX futures | ARX spot | Status |
| --- | --- | --- | --- | --- |
| Instruments | `/api/v3/market/instruments` | `category=USDT-FUTURES`, `symbol=ARXUSDT` | `category=SPOT`, `symbol=ARXUSDT` | documented route; identity must be observed, not assumed |
| Ticker | `/api/v3/market/ticker` | yes | yes | documented route; mark/index fields are retained only when returned |
| Book snapshot | `/api/v3/market/orderbook` | yes | yes | documented snapshot; reconnect and sequence continuity are not inferred |
| Current funding | `/api/v3/market/current-fund-rate` | yes | no | documented for futures, including interval and next-update fields |
| Funding history | `/api/v3/market/history-fund-rate` | yes | no | documented public history route; pagination/retention needs a collection-time observation |
| Open interest | `/api/v3/market/open-interest` | yes | no | documented public snapshot route |
| Candles / public trades | `/api/v3/market/candles`, `/api/v3/market/fills` | yes | yes | documented routes; only elapsed candles are labelled completed |
| Position tiers | — | unknown | n/a | **unverified**: no route is called or synthesized |
| Liquidations | public websocket announcement only | unknown | n/a | **unverified for this REST collector** |

The [Bitget current-funding API](https://www.bitget.com/api-doc/uta/public/Get-Current-Funding-Rate)
documents the futures categories, decimal funding rate, interval, and next
update timestamp. Bitget's [UTA changelog](https://www.bitget.com/api-doc/uta/changelog)
is the source for the documented v3 market-route names and records that
liquidation was added as a websocket channel; it is not evidence that ARX has
current liquidation events or that any account-level capability works.

Identity is strict: `ARXUSDT` futures is retained separately from `ARXUSDT`
spot and accepted as a campaign instrument only after an observed instrument
response states ARX base, USDT quote/settlement, linear perpetual, and online
status. A successful public response is merely a current observation, never
proof of live readiness, balances, margin mode, tiers, or execution behavior.
