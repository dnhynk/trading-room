# ARX campaign research thesis (non-executable)

Arcium's [official site](https://www.arcium.com/) describes `$ARX` as the token
for the Arcium network and contributors. This is an **official fact about the
project's stated token role**, not evidence of exchange listing quality,
liquidity, performance, or a trading recommendation.

The current [official tokenomics page](https://www.arcium.com/tokenomics) states
a fixed one-billion supply, 20.88% unlocked at launch, and allocation-specific
cliffs/linear vesting for most of the remainder. It also says compute fees are paid
in a chain-native asset such as SOL—not directly in ARX—while ARX is used for
staking and governance. Those are dated official statements. The inference that
product use creates exchange demand for ARX is indirect and must not be promoted to
an order signal. Exact unlock events must be materialized with `event_at`,
`first_observed_at`, `fetched_at`, expiry, source URL and raw hash; a chart image or
relative “month N” statement is not silently converted into a calendar event.

Bitget's [spot listing notice](https://www.bitget.com/support/articles/12560603886296)
identifies the Solana contract as
`ARXwZkNAtzPfdcoqQiduJn8EPv9fKiDfGn2KyggyDrFs`, matching Arcium's current
tokenomics page. This establishes the listing identity evidence used for research;
it does not prove that any external venue's similarly named market is the same asset.

The research hypothesis is deliberately narrow: compare separately collected
ARX spot and Bitget `ARXUSDT` USDT-linear perpetual observations during clearly
labelled, complete-data intervals. It may examine basis, mark/index/last
dispersion, order-book snapshots, public trades, open interest and funding,
but cannot fill missing values or declare a price proxy executable.

Any proposed participation rule is an **inference** until tested on fixed,
out-of-sample episodes that retain reconnects, gaps, duplicate observations,
and receive latency. Claims that ARX will rise, that funding predicts returns,
or that public replay represents fills, liquidation safety, profitability, or
live readiness are **unverified**. This document approves no order and does
not change the track's observe-only, live-disabled contract.
