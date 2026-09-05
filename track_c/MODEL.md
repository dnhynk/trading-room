# C2 — execution-conditioned quantitative policy

This contract supersedes C1's volume-ranked eight-symbol basket, mandatory v/flow entry, fixed 4-second entry TTL / 32-second exit, and ATR-based economic sizing. C1 remains an immutable comparison implementation. A/B stay paused. The existing reconciled cash ledger, durable order intent, cumulative fill accounting, ownership rules, cancellation reconciliation, and exchange protective orders remain execution invariants.

The objective is full-account compounded growth after actual execution costs. Predicting a future price level, a high win rate, a large turnover ranking, or passing unit tests is not evidence of that objective. Parameters are separated into exchange constraints, user risk limits, numerical approximation grids, and coefficients estimated from past data. No parameter is represented as mathematically optimal without the associated assumptions and evaluation.

## Observations and candidates

Keep source receive time, exchange time, message identity, venue and original book depth. Replay and live run the same causal feature code. A future event, future normalization, unclosed interval, or post-decision contract cannot enter a decision. Reject crossed/invalid books, duplicate trades, backwards channel times and stale data. Preserve outages as censored observations instead of forward-filling successful exits.

Exclude stablecoins/pegged assets from this ordinary spot strategy using a versioned asset policy. Existing external positions/orders retain their owner. BTC/ETH are observation benchmarks, with no trading exemption. The public subscription budget is a resource bound, not an instruction to fill trading slots. Turnover may decide acquisition coverage, never execution eligibility. Broad candidates are assessed using current spread, tick burden, executable depth, aggressive flow, replenishment, order-flow imbalance, volatility and data reliability. Every admission/refusal and the complete considered action set is auditable.

## Estimated components

1. A regularized conditional fill model estimates the probability of execution by candidate TTL, using observable queue ahead, size, trade intensity, depletion and imbalance. Public market-by-price data cannot identify cancellations ahead of our order. Labels therefore preserve conservative/optimistic queue scenarios and censored outcomes; a synthetic fill is never called an exchange fill.
2. A regularized conditional outcome model estimates the distribution of fee- and depth-adjusted sell proceeds after a passive fill. It is fitted on filled outcomes, separately from fill probability, so adverse selection is retained. Its response is executable net return, not future price level. Residual distributions and uncertainty accompany the mean.
3. A continuation model estimates the incremental proceeds of waiting versus liquidating the current inventory now. Existing entry cost is sunk for this comparison. Re-evaluate on current market state; do not wait for breakeven. Data failure, account uncertainty, the exchange protection and daily risk limits override modeled continuation.

Action axes are quantity, entry TTL and liquidation horizon. A logarithmic grid is a numerical approximation, not an alpha threshold. The policy compares uncertainty-adjusted expected log wealth growth, including idle capital during unfilled waits and the downside tail. Quantity is rounded down to the exchange step and must satisfy cash, executable sale depth, conservative tail-loss budget, exchange limits and minimum notional at the protective limit. Hard risk ceilings retain the previously authorized 0.25% per attempt / 1.5% daily policy; these are preferences, not learned alpha and not a guarantee against gaps.

Orders already in flight keep their recorded model version and protection. Later campaigns may adopt a new model while existing campaigns retain their original version. No request timeout permits a second order identifier. No change removes the exchange stop. A small experimental fill, if the policy permits one, is identified as live learning; its size is a feasible exchange-minimum-scale grid point subject to risk constraints, not a fixed operating-capital allocation. Live learning does not imply evidence of profitability. No arbitrary long paper/OOS waiting period overrides the user's authorization to learn through real trading.

## Fitting, validation and governance

### User refinement: simultaneous scalping and one-tick realization

Multiple independent symbols may hold campaigns simultaneously. One serialized portfolio owns the cash ledger and sums pending buy reservations, open downside, and research losses before each new intent. The 0.25% loss allowance is also a shared concurrent-risk ceiling; it is not multiplied by the number of coins. No manual per-order KRW cap and no averaging down. Each campaign retains its model until close while subsequent campaigns may use a newer artifact.

Candidate take-profit distances include one and two valid exchange ticks. Labels model first passage of an executable bid, not an assumed fill merely because an ask was touched. Live profit-taking cancels/reconciles the existing protective order before a price-limited market sale; it never reserves the same inventory for two sells. If the opportunity disappears, reconcile partial execution and restore protection or exit for risk/time. Coinone's documented API does not supply native OCO here, so a resting profit ask plus simultaneous full-quantity stop is not assumed available. Fees must be verified zero on the actual account. Entry frequency is the result of supported positive expected execution outcomes, not an unconditional quota.

All KRW amounts in Slack are rounded to whole won for display only; exact Decimal accounting remains unchanged.

The persistent portfolio is state v3, with one shared cash account, per-symbol campaigns and orders. Risk/time/data exits override a pending bounded profit sale. The production owner serializes OMS/account mutations; public and private streams only update observations or wake it. API budgets are shared and below the documented venue ceilings.

The independent worker freezes the past two hours, samples four-second anchors without looking at outcomes (at most 2,400 states to bound memory), and runs again thirty minutes after completion. It appends only mature exchange outcomes to training; public counterfactuals retain their separate source identity. Empirical live promotion uses time-block returns, a zero-mean dispersion prior from training residuals, an approximate small-sample interval and correction for evaluated models. The admission mode and its evidence are in the artifact. Neither this approximation nor a software test proves future profitability. An expired/invalid model prevents new entries while protection continues.

Freeze input files and hashes. Split all symbols on the same time boundaries. Purge training labels whose information interval reaches the next fold and embargo dependent neighbors. Fit scalers, regularization selection and calibration on training/inner validation only; reserve chronological outer folds for evaluation. Score fill probabilities with proper losses/calibration, conditional returns with error/interval coverage, and complete non-overlapping portfolios with fees, queue ambiguity, latency stress, drawdown and log growth. Compare C1/B-entry conditions and a time-matched control without picking the best control after seeing outcomes. Statistical uncertainty uses time blocks, not the count of correlated action-grid labels.

Artifacts are JSON with feature schema, data hashes, training cutoff, model parameters, training support, uncertainty, validation and code identity. Reject malformed, nonfinite, stale, future-trained or incompatible artifacts. Record hypothetical queue labels separately from real fill observations. Real completed attempts supply execution calibration and cost attribution; only matured outcomes can influence later model versions. Drift and missing support produce explicit refusals or bounded research status, never silently confident forecasts.

## Build acceptance

- Shared causal market state and typed/versioned features; queue-aware, size-aware and latency-aware label construction.
- Regularized fitting, chronological purged validation, calibrated inference, continuation evaluation, growth-based feasible sizing and no-action alternative.
- Asset exclusions and dynamic execution eligibility; selection explanations and candidate coverage in status.
- Identical policy in replay and live runner, preserved OMS/accounting/protection, observable model/decision/fill linkage.
- Frozen real-data run, comparison and cost/latency stress artifacts; truthful distinction between deployment and alpha evidence.
- AWS deployment with flat checks, same-account reconciliation, independent existing Slack service, rollback path and restart verification.

References informing the implementation, not evidence of Coinone profitability: [Cont–Kukanov–Stoikov, order-flow imbalance](https://arxiv.org/abs/1011.6402), [Huang–Lehalle–Rosenbaum, queue-reactive modeling](https://arxiv.org/abs/1312.0563), [Bailey–López de Prado, selection bias in backtests](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf), [Coinone order-book protocol](https://docs.coinone.co.kr/reference/public-websocket-orderbook).
