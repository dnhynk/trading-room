"""Causal, bounded accumulation research; decisions contain no exchange I/O.

Below-average additions are an explicit owner-directed exception for this
strategy only. They consume the original budget and do not enlarge later clips.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any

from ..contracts import decimal
from ..risk.liquidation import independent_long_liquidation_price

D = Decimal
ZERO, ONE, BPS = D("0"), D("1"), D("10000")


def number(value: Any) -> Decimal:
    result = decimal(value)
    if not result.is_finite():
        raise ValueError("NONFINITE_FINANCIAL_VALUE")
    return result


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= ZERO:
        raise ValueError("INVALID_QUANTITY_STEP")
    return (max(value, ZERO) / step).to_integral_value(rounding=ROUND_DOWN) * step


def validate_config(config: dict[str, Any]) -> None:
    if (config.get("schema_version") != 1 or config.get("mode") != "paper"
            or config.get("live_enabled") is not False
            or config.get("api_family") != "classic_v2"
            or config.get("symbol") != "ARXUSDT"):
        raise ValueError("ACCUMULATION_REQUIRES_CLASSIC_V2_PAPER_CONFIG")
    if number(config["leverage"]) != 10 or number(config["target_notional_multiple"]) != 10:
        raise ValueError("OWNER_TEN_X_BUDGET_CONTRACT")
    for key in ("initial_fraction", "pullback_target_fraction", "compression_target_fraction",
                "normal_slice_fraction", "urgent_slice_fraction", "book_participation",
                "bar_volume_participation", "initial_buy_share", "urgent_buy_share",
                "urgent_book_share", "compression_ratio", "pre_breakout_box_position"):
        if not ZERO < number(config[key]) < ONE:
            raise ValueError("INVALID_ACCUMULATION_FRACTION")
    if not (number(config["initial_fraction"]) < number(config["pullback_target_fraction"])
            < number(config["compression_target_fraction"])):
        raise ValueError("INVALID_ACCUMULATION_TARGETS")
    for key in ("pullback_atr_spacing", "max_spread_bps", "max_slippage_bps",
                "structural_stop_atr", "liquidation_buffer_atr"):
        if number(config[key]) <= ZERO:
            raise ValueError("INVALID_ACCUMULATION_DISTANCE")
    for key in ("box_bars", "min_trade_samples", "max_trade_age_seconds",
                "max_snapshot_age_seconds", "min_slice_interval_seconds", "poll_seconds"):
        if type(config[key]) is not int or config[key] <= 0:
            raise ValueError("INVALID_ACCUMULATION_INTERVAL")
    if config["box_bars"] < 12 or type(config.get("allow_below_average_add")) is not bool:
        raise ValueError("INVALID_ACCUMULATION_CONTRACT")


@dataclass(frozen=True)
class Frame:
    time_ms: int
    bar_ms: int
    bid: Decimal
    ask: Decimal
    mark: Decimal
    close: Decimal
    previous_close: Decimal
    low: Decimal
    previous_low: Decimal
    range_low: Decimal
    range_high: Decimal
    atr: Decimal
    contraction: Decimal
    close_location: Decimal
    buy_share: Decimal
    book_share: Decimal
    bar_quote_volume: Decimal
    asks: tuple[tuple[Decimal, Decimal], ...]
    step: Decimal
    min_quantity: Decimal
    min_notional: Decimal
    max_quantity: Decimal
    taker_fee: Decimal
    mmr: Decimal
    funding_rate: Decimal
    next_funding_ms: int
    entry_blockers: tuple[str, ...] = ()


def market_frame(raw: dict[str, Any], config: dict[str, Any], e0: Decimal) -> Frame:
    """Closed 1-minute bars and current quotes; future bars never affect signals."""
    if raw.get("api_family") != "classic_v2":
        raise ValueError("CLASSIC_FRAME_REQUIRED")
    now = int(raw["finished_ms"])
    max_age = config["max_snapshot_age_seconds"] * 1000
    ticker, book, contract = raw["ticker"], raw["book"], raw["contract"]
    for timestamp in (raw["started_ms"], ticker["ts"], book["ts"]):
        if not -1000 <= now - int(timestamp) <= max_age:
            raise ValueError("STALE_MARKET_SNAPSHOT")
    if (ticker.get("symbol") != "ARXUSDT" or contract.get("symbol") != "ARXUSDT"
            or contract.get("baseCoin") != "ARX" or contract.get("quoteCoin") != "USDT"
            or contract.get("symbolType") != "perpetual"
            or "USDT" not in contract.get("supportMarginCoins", [])
            or contract.get("symbolStatus") != "normal"
            or number(contract["maxLever"]) < number(config["leverage"])):
        raise ValueError("INSTRUMENT_UNAVAILABLE_OR_MISMATCHED")
    bars = sorted((r for r in raw["candles"] if int(r[0]) + 60000 <= now),
                  key=lambda r: int(r[0]))
    count = config["box_bars"]
    if len(bars) < count + 1:
        raise ValueError("INSUFFICIENT_CLOSED_BARS")
    bars = bars[-count - 1:]
    if any(int(b[0]) - int(a[0]) != 60000 for a, b in zip(bars, bars[1:])):
        raise ValueError("BAR_GAP_OR_DUPLICATE")
    if now - int(bars[-1][0]) > 120000:
        raise ValueError("STALE_CLOSED_BAR")
    for bar in bars:
        o, h, low, close = map(number, bar[1:5])
        if min(o, h, low, close) <= ZERO or low > min(o, close) or h < max(o, close):
            raise ValueError("INVALID_OHLC")
    prior, signal = bars[:-1], bars[-1]
    ranges = [number(r[2]) - number(r[3]) for r in prior]
    atr = sum(ranges[-12:], ZERO) / 12
    older_range = sum(ranges[-12:-3], ZERO) / 9
    if atr <= ZERO or older_range <= ZERO:
        raise ValueError("NO_OBSERVED_PRICE_RANGE")
    bid, ask, mark = map(number, (ticker["bidPr"], ticker["askPr"], ticker["markPrice"]))
    if min(bid, ask, mark) <= ZERO or bid > ask:
        raise ValueError("INVALID_EXECUTABLE_QUOTE")
    asks = tuple(sorted((number(p), number(q)) for p, q in book["asks"]))
    bids = tuple(sorted(((number(p), number(q)) for p, q in book["bids"]), reverse=True))
    if (not asks or not bids or bids[0][0] > asks[0][0]
            or any(min(p, q) <= ZERO for p, q in (*asks, *bids))):
        raise ValueError("INVALID_ORDER_BOOK")
    # Ticker and depth are separate observations. Do not silently join distant quotes.
    if abs(asks[0][0] / ask - ONE) * BPS > number(config["max_spread_bps"]):
        raise ValueError("QUOTE_BOOK_DISAGREEMENT")
    trades: dict[str, dict[str, Any]] = {}
    for trade in raw["trades"]:
        if trade.get("symbol") != "ARXUSDT" or trade.get("side") not in {"buy", "sell"}:
            raise ValueError("INVALID_PUBLIC_TRADE")
        timestamp = int(trade["ts"])
        if now - 300000 <= timestamp <= now:
            trades[str(trade["tradeId"])] = trade
    entry_blockers: tuple[str, ...] = ()
    if (len(trades) < config["min_trade_samples"]
            or now - max((int(t["ts"]) for t in trades.values()), default=0)
            > config["max_trade_age_seconds"] * 1000):
        entry_blockers = ("INSUFFICIENT_RECENT_TRADE_FLOW",)
    buys, total = ZERO, ZERO
    for trade in trades.values():
        notional = number(trade["price"]) * number(trade["size"])
        if notional <= ZERO:
            raise ValueError("INVALID_PUBLIC_TRADE_SIZE")
        total += notional
        if trade["side"] == "buy":
            buys += notional
    ask_value = sum((p * q for p, q in asks[:5]), ZERO)
    bid_value = sum((p * q for p, q in bids[:5]), ZERO)
    compatible = [r for r in raw["tiers"] if r.get("symbol") == "ARXUSDT"
                  and number(r["startUnit"]) <= e0 * 10 <= number(r["endUnit"])
                  and number(r["leverage"]) >= 10]
    if not compatible:
        raise ValueError("TEN_X_POSITION_TIER_UNVERIFIED")
    mmr = max(number(r["keepMarginRate"]) for r in compatible)
    if not ZERO <= mmr < D("0.1"):
        raise ValueError("INVALID_MAINTENANCE_TIER")
    high, low, close = map(number, (signal[2], signal[3], signal[4]))
    return Frame(
        now, int(signal[0]), bid, ask, mark, close, number(prior[-1][4]),
        low, number(prior[-1][3]), min(number(r[3]) for r in prior),
        max(number(r[2]) for r in prior), atr,
        (sum(ranges[-3:], ZERO) / 3) / older_range,
        (close - low) / (high - low) if high > low else ZERO,
        buys / total if total > ZERO else ZERO, bid_value / (bid_value + ask_value), number(signal[6]),
        asks, number(contract["sizeMultiplier"]), number(contract["minTradeNum"]),
        number(contract["minTradeUSDT"]), number(contract["maxMarketOrderQty"]),
        number(contract["takerFeeRate"]), mmr, number(raw["funding"]["fundingRate"]),
        int(raw["funding"]["nextUpdate"]), entry_blockers,
    )


@dataclass(frozen=True)
class AccumulationDecision:
    phase: str
    quantity: Decimal = ZERO
    worst_price: Decimal = ZERO
    target_fraction: Decimal = ZERO
    reason: str = "WAIT"


def decide(frame: Frame, state: dict[str, Any], config: dict[str, Any]) -> AccumulationDecision:
    """Adaptive clips: absorption on pullbacks, then faster pre-breakout completion."""
    def n(key: str) -> Decimal:
        return number(config[key])

    e0, spent, quantity = map(number, (state["e0"], state["notional"], state["quantity"]))
    if state.get("terminal"):
        return AccumulationDecision("FINISHED", reason="CAMPAIGN_TERMINAL_NO_REENTRY")
    if state.get("pending"):
        return AccumulationDecision("WAIT", reason="UNRECONCILED_RESERVATION")
    if frame.entry_blockers:
        return AccumulationDecision("WAIT", reason=frame.entry_blockers[0])
    if frame.ask > frame.range_high:
        return AccumulationDecision("MISSED_PRE_BREAKOUT", reason="NO_BREAKOUT_CHASE")
    if (frame.ask / frame.bid - ONE) * BPS > n("max_spread_bps"):
        return AccumulationDecision("WAIT", reason="SPREAD_TOO_WIDE")
    if frame.time_ms - int(state.get("last_fill_ms", 0)) < config["min_slice_interval_seconds"] * 1000:
        return AccumulationDecision("WAIT", reason="CLIP_SPACING")
    if frame.bar_ms == state.get("last_signal_bar_ms"):
        return AccumulationDecision("WAIT", reason="SIGNAL_ALREADY_CONSUMED")
    if quantity > ZERO and not config["allow_below_average_add"] and frame.ask < spent / quantity:
        return AccumulationDecision("WAIT", reason="BELOW_AVERAGE_ADD_DISABLED")
    width = frame.range_high - frame.range_low
    if width <= ZERO or frame.close < frame.range_low:
        return AccumulationDecision("WAIT", reason="BASE_NOT_STABLE")
    location = (frame.close - frame.range_low) / width
    stable = (frame.close >= frame.previous_close or frame.close_location >= D("0.65"))
    stable = stable and frame.buy_share >= n("initial_buy_share")
    urgent = (stable and location >= n("pre_breakout_box_position")
              and frame.low >= frame.previous_low
              and frame.buy_share >= n("urgent_buy_share")
              and frame.book_share >= n("urgent_book_share"))
    target = ZERO
    clip = n("normal_slice_fraction")
    if spent < e0 * 10 * n("initial_fraction") and stable:
        phase, target, reason = "BASE", n("initial_fraction"), "SMALL_INITIAL_ALLOCATION"
    elif urgent:
        phase, target, clip, reason = "ACCELERATE", ONE, n("urgent_slice_fraction"), "PRESSURE_BEFORE_BREAKOUT"
    elif (stable and frame.ask <= number(state.get("last_fill_price", "0"))
          - frame.atr * n("pullback_atr_spacing") and location <= D("0.65")):
        phase, target, reason = "PULLBACK", n("pullback_target_fraction"), "LOWER_PRICE_WITH_ABSORPTION"
    elif (stable and frame.contraction <= n("compression_ratio")
          and frame.low >= frame.previous_low and location >= D("0.50")):
        phase, target, reason = "COMPRESS", n("compression_target_fraction"), "CONTRACTION_WITH_BUYING"
    else:
        return AccumulationDecision("WAIT", reason="NO_ACCUMULATION_SIGNAL")
    desired_notional = min(e0 * 10 * clip, max(ZERO, e0 * 10 * target - spent))
    worst = frame.ask * (ONE + n("max_slippage_bps") / BPS)
    # Existing margin and paid fees stay committed. Unfinished reservations block above.
    free = e0 - spent / 10 - number(state["fees"]) - max(ZERO, number(state["funding"]))
    free -= spent * frame.taker_fee  # reserve the existing position's exit fee
    per_unit = worst / 10 + worst * frame.taker_fee * 2 + max(ZERO, worst - frame.mark)
    depth_quantity = sum((q for p, q in frame.asks if p <= worst), ZERO) * n("book_participation")
    quantity_to_buy = floor_step(min(
        desired_notional / worst, max(ZERO, free) / per_unit,
        max(ZERO, e0 * 10 - spent) / worst, depth_quantity,
        max(ZERO, frame.bar_quote_volume) * n("bar_volume_participation") / worst,
        frame.max_quantity,
    ), frame.step)
    if quantity_to_buy < frame.min_quantity or quantity_to_buy * frame.ask < frame.min_notional:
        return AccumulationDecision(phase, target_fraction=target, reason="BUDGET_OR_LIQUIDITY_BELOW_MINIMUM")
    return AccumulationDecision(phase, quantity_to_buy, worst, target, reason)


def simulated_fill(frame: Frame, decision: AccumulationDecision) -> tuple[Decimal, Decimal]:
    """Walk the observed asks. This is a simulated taker fill, never a real fill."""
    remaining, notional = decision.quantity, ZERO
    for price, available in frame.asks:
        if price > decision.worst_price:
            break
        take = min(remaining, available)
        notional += take * price
        remaining -= take
        if remaining <= ZERO:
            break
    return decision.quantity - remaining, notional


def liquidation_estimate(frame: Frame, state: dict[str, Any]) -> Decimal:
    quantity, notional = number(state["quantity"]), number(state["notional"])
    if quantity <= ZERO:
        return ZERO
    return independent_long_liquidation_price(
        quantity_base=quantity, average_entry_price=notional / quantity,
        isolated_margin_usdt=notional / 10,
        maintenance_margin_rate=frame.mmr, liquidation_close_fee_rate=frame.taker_fee,
        unbooked_funding_cost_usdt=max(ZERO, number(state["funding"])),
    )
