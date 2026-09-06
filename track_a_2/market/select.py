"""Stable Coinone basket eligibility: ranking fills vacancies, never replaces incumbents."""
import math


def _top(row, name):
    levels = row.get(name) or []
    try:
        return float(levels[0]["price"])
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def ranked(contracts, tickers, config):
    allow = set(config["universe"])
    markets = {row.get("target_currency"): row for row in contracts}
    rows, reasons = [], {}
    for ticker in tickers:
        coin = str(ticker.get("target_currency", "")).upper()
        if coin not in allow:
            continue
        contract = markets.get(coin)
        reason = None
        if not contract:
            reason = "market_missing"
        elif contract.get("trade_status") != 1 or contract.get("maintenance_status") != 0:
            reason = "market_unavailable"
        elif not {"limit", "market", "stop_limit"} <= set(contract.get("order_types") or []):
            reason = "order_types"
        try:
            volume = float(ticker.get("quote_volume") or 0)
            high = float(ticker.get("high") or 0)
            low = float(ticker.get("low") or 0)
            last = float(ticker.get("last") or 0)
            range_pct = (high - low) / last * 100
        except (TypeError, ValueError, ZeroDivisionError):
            volume = range_pct = 0
        bid, ask = _top(ticker, "best_bids"), _top(ticker, "best_asks")
        spread_bp = (ask - bid) / ((ask + bid) / 2) * 10000 if bid and ask and ask > bid else math.inf
        if not reason and (not math.isfinite(volume) or volume < config["min_quote_volume_24h"]):
            reason = "turnover"
        if not reason and not config["min_range_24h_pct"] <= range_pct <= config["max_range_24h_pct"]:
            reason = "range"
        if not reason and spread_bp > config["max_spread_bp"]:
            reason = "spread"
        if reason:
            reasons[coin] = reason
            continue
        rows.append(dict(coin=coin, score=volume * range_pct, volume=volume, range_pct=range_pct, spread_bp=spread_bp))
    rows.sort(key=lambda row: (-row["score"], row["coin"]))
    for coin in allow - {row["coin"] for row in rows} - set(reasons):
        reasons[coin] = "ticker_missing"
    return rows, reasons


def retain(previous, held, candidates, size):
    """Eligible incumbents stay; disqualified holdings wind down; rank only fills slots."""
    eligible = {row["coin"] for row in candidates}
    selected = [coin for coin in previous if coin in eligible]
    for row in candidates:
        if len(selected) >= size:
            break
        if row["coin"] not in selected:
            selected.append(row["coin"])
    wind_down = sorted(set(held) - eligible)
    watch = list(dict.fromkeys(selected + wind_down))
    return dict(selected=selected, wind_down=wind_down, watch=watch)
