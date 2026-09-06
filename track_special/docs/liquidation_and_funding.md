# Liquidation and funding evidence boundaries

## Current observations

Funding records are append-only observations: the raw response hash, exchange
time, receive time, and unavailable values are retained. A current funding
rate is not a paid/received funding event; the [Bitget funding endpoint](https://www.bitget.com/api-doc/uta/public/Get-Current-Funding-Rate)
defines it as a current rate and separately returns its settlement interval and
next update. Historical funding, if collected from its documented public route,
remains a source series with its own coverage gaps.

## Historical announcements

Bitget's [UTA changelog](https://www.bitget.com/api-doc/uta/changelog) says a
platform liquidation websocket channel was added on 2025-11-26. That is a
historical product announcement, not a claim that this track has observed ARX
liquidations, has continuous websocket coverage, or can calculate liquidation
prices.

## Unverified claims

No public REST position-tier route is treated as verified here, and no account
or position liquidation price is available to this public collector. Mark,
index, last, bid, and ask are distinct fields: missing mark or index is stored
as unavailable rather than substituted from last price. Funding-based strategy
or profitability claims are unverified without an independent, time-bounded
evaluation that preserves gaps, duplicates, and collection latency.
