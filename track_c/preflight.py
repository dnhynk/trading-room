"""Read-only Coinone readiness; --order-krw is a hypothetical depth probe, never an order."""
import argparse
from decimal import Decimal, ROUND_DOWN
import json
from pathlib import Path
import time

from .coinone import CoinoneError, CoinoneReadOnly, Credentials, decimal, symbol


def round_trip(book, market, units, amount, fee):
    """Same-book buy/sell estimate, qty floored, full visible depth required.

    Excludes queue waiting, latency moves and adverse selection. Fees charged in
    KRW are an estimate until actual fills establish the exchange's accounting.
    """
    asks = sorted((decimal(r["price"], positive=True), decimal(r["qty"])) for r in book["asks"])
    bids = sorted(((decimal(r["price"], positive=True), decimal(r["qty"])) for r in book["bids"]), reverse=True)
    if not asks or not bids or bids[0][0] >= asks[0][0]:
        raise CoinoneError("empty or crossed book")
    mid = (asks[0][0] + bids[0][0]) / 2
    tiers = sorted((decimal(u["range_min"]), decimal(u["price_unit"], positive=True)) for u in units)
    applicable = [step for floor, step in tiers if floor <= mid]
    if not applicable:
        raise CoinoneError("price unit unavailable")
    out = dict(spread_bps=str((asks[0][0] - bids[0][0]) / mid * 10000), tick_bps=str(applicable[-1] / mid * 10000))
    if amount is None:
        return out
    step = decimal(market["qty_unit"], positive=True)
    quantity = (decimal(amount, positive=True) / asks[0][0] / step).to_integral_value(rounding=ROUND_DOWN) * step
    out.update(probe_order_krw=str(amount), probe_qty=str(quantity))
    if quantity < decimal(market["min_qty"]) or quantity * asks[0][0] < decimal(market["min_order_amount"]):
        out["depth_status"] = "below_minimum_order"
        return out

    def walk(levels):
        remaining, cash = quantity, Decimal(0)
        for price, qty in levels:
            used = min(remaining, qty)
            cash += price * used
            remaining -= used
            if not remaining:
                return cash
        return None

    buy, sell = walk(asks), walk(bids)
    if buy is None or sell is None:
        out["depth_status"] = "insufficient_visible_depth"
        return out
    out.update(depth_status="covered", buy_notional_krw=str(buy), sell_notional_krw=str(sell),
               spread_and_depth_bps=str((buy - sell) / buy * 10000), account_fee_inclusive_bps=None)
    if fee is not None:
        taker = decimal(fee["taker"])
        out["account_fee_inclusive_bps"] = str(((buy * (1 + taker)) - sell * (1 - taker)) / buy * 10000)
    return out


def inspect(client, coins, *, private, amount=None):
    report = dict(started_ms=time.time_ns() // 1_000_000, venue="coinone", mode="read_only",
                  live_trading_ready=False, orders_submitted=0, markets={}, checks=[],
                  note="Read-only connectivity/depth inspection; this command does not validate runtime funding, sizing, or execution readiness.")
    if private:
        try:
            balances = client.balances()
            orders = client.active_orders()
            krw = [r for r in balances if r.get("currency") == "KRW"]
            if len(krw) != 1:
                raise CoinoneError("KRW balance missing or ambiguous")
            report["account"] = dict(krw_available=str(decimal(krw[0]["available"])), krw_reserved=str(decimal(krw[0]["limit"])),
                                     active_order_count=len(orders),
                                     existing_assets=[r["currency"] for r in balances if r.get("currency") != "KRW" and decimal(r["available"]) + decimal(r["limit"]) > 0],
                                     ownership="Existing assets and orders are not owned or modified by Track C.")
        except (CoinoneError, KeyError, TypeError) as exc:
            report["account_error"] = str(exc) if isinstance(exc, CoinoneError) else "invalid account response"
            report["checks"].append("account_read_failed")
    for coin in coins:
        row = {}
        try:
            meta = client.market(coin)
            units = client.price_units(coin)
            fee = None
            if private:
                try:
                    fee = client.fees(coin)
                except CoinoneError as exc:
                    row["fee_error"] = str(exc)
                    report["checks"].append(f"{coin}:account_fee_unverified")
            sent = time.time_ns() // 1_000_000
            book = client.orderbook(coin)
            received = time.time_ns() // 1_000_000
            age = received - int(book["timestamp"])
            row.update(trade_status=meta["trade_status"], maintenance_status=meta["maintenance_status"],
                       order_types=meta.get("order_types", []), min_order_krw=str(decimal(meta["min_order_amount"])),
                       quantity_step=str(decimal(meta["qty_unit"], positive=True)), observed_ms=received,
                       rest_round_trip_ms=received-sent, exchange_book_age_ms=age,
                       account_fees=fee, cost=round_trip(book, meta, units, amount, fee))
            if meta["trade_status"] != 1 or meta["maintenance_status"] != 0:
                report["checks"].append(f"{coin}:market_not_tradable")
            if not 0 <= age <= 5000:
                report["checks"].append(f"{coin}:stale_book_or_clock_skew")
        except (CoinoneError, KeyError, TypeError, ValueError) as exc:
            row["error"] = str(exc) if isinstance(exc, CoinoneError) else "invalid public response"
            report["checks"].append(f"{coin}:public_check_failed")
        report["markets"][coin] = row
    report["completed_ms"] = time.time_ns() // 1_000_000
    report["status"] = "BLOCKED" if report["checks"] else "ACCOUNT_READABLE" if private else "PUBLIC_ONLY"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default=str(Path(__file__).resolve().parents[1] / ".env"))
    parser.add_argument("--public-only", action="store_true")
    parser.add_argument("--credential-profile", choices=("default", "aws"), default="default")
    parser.add_argument("--symbols", nargs="+", default=["BTC", "ETH", "XRP"], type=symbol)
    parser.add_argument("--order-krw", type=lambda s: decimal(s, positive=True))
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    path = Path(args.output)
    if path.exists():
        parser.error("output already exists; use a new audit file")
    try:
        creds = None if args.public_only else Credentials.read(args.env, profile=args.credential_profile)
    except CoinoneError as exc:
        report = dict(status="BLOCKED", live_trading_ready=False, orders_submitted=0, reason=str(exc))
    else:
        report = inspect(CoinoneReadOnly(creds), args.symbols, private=not args.public_only, amount=args.order_krw)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
    print(json.dumps(dict(status=report["status"], live_trading_ready=False, orders_submitted=0, output=str(path.resolve())), ensure_ascii=False))
    return 1 if report["status"] == "BLOCKED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
