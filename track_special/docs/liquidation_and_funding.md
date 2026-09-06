# Liquidation and funding evidence boundaries

## Current observations

Funding records are append-only observations: the raw response hash, exchange
time, receive time, and unavailable values are retained. A current funding
rate is not a paid/received funding event; the [Bitget funding endpoint](https://www.bitget.com/api-doc/uta/public/Get-Current-Funding-Rate)
defines it as a current rate and separately returns its settlement interval and
next update. Historical funding uses `cursor` and `limit`; its data are in
`data.resultList` and remain a source series with their own coverage gaps.

## Historical announcements

The public [UTA liquidation-history API](https://www.bitget.com/api-doc/uta/public/Get-Liquidations)
is `GET /api/v3/market/liquidations`; it returns delayed, three-day history in
`data.list` with a cursor. It is not a claim of continuous coverage, an ARX
event, or an account liquidation price.

## Unverified claims

The public `GET /api/v3/market/position-tier` is retained as a dated market
observation, while no account
or position liquidation price is available to this public collector. Mark,
index, last, bid, and ask are distinct fields: missing mark or index is stored
as unavailable rather than substituted from last price. Funding-based strategy
or profitability claims are unverified without an independent, time-bounded
evaluation that preserves gaps, duplicates, and collection latency.
