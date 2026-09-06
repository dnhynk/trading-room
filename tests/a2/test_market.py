from decimal import Decimal as D
import unittest

from track_a_2.market.feed import Market
from track_a_2.market.select import ranked, retain
from track_a_2.market.units import (
    price_ceil, price_down, price_floor, stop_prices,
    stop_prices_for_limit_floor,
)
from track_a_2.settings import load
from track_a_2.strategy.sizing import size


UNITS = [
    {"range_min": "0", "price_unit": "0.01"},
    {"range_min": "100", "price_unit": "0.1"},
    {"range_min": "1000", "price_unit": "1"},
]
CONTRACT = dict(
    quote_currency="KRW", target_currency="AAA", trade_status=1,
    maintenance_status=0, order_types=["limit", "market", "stop_limit"],
    qty_unit="0.1", min_qty="0.1", max_qty="100000",
    min_order_amount="5000", max_order_amount="1000000000",
)


def ticker(coin, *, volume=10_000_000_000, high=110, low=100, last=105, bid=104.9, ask=105):
    return dict(
        quote_currency="KRW", target_currency=coin, quote_volume=str(volume),
        high=str(high), low=str(low), last=str(last),
        best_bids=[{"price": str(bid), "qty": "100"}],
        best_asks=[{"price": str(ask), "qty": "100"}],
    )


class UnitsTests(unittest.TestCase):
    def test_rounding_respects_variable_price_boundaries(self):
        self.assertEqual(price_floor(UNITS, "99.999"), D("99.99"))
        self.assertEqual(price_ceil(UNITS, "99.999"), D("100.0"))
        self.assertEqual(price_floor(UNITS, "100.09"), D("100.0"))
        self.assertEqual(price_down(UNITS, "100"), D("99.99"))
        self.assertEqual(price_down(UNITS, "1000", 2), D("999.8"))

    def test_stop_limit_is_strictly_below_trigger(self):
        trigger, limit = stop_prices(UNITS, "100.08", 2, 10)
        self.assertEqual(trigger, D("100.0"))
        self.assertLess(limit, trigger)
        self.assertEqual(limit, D("99.90"))

    def test_known_stop_limit_buffer_is_inside_campaign_budget(self):
        trigger, limit = stop_prices_for_limit_floor(UNITS, "90", 2, 10)
        self.assertGreater(trigger, limit)
        self.assertGreaterEqual(limit, D("90"))
        self.assertGreaterEqual(D(100) * (limit - D(100)), D("-1000"))


class SelectionTests(unittest.TestCase):
    def config(self):
        return {**load(), "universe": ["AAA", "BBB", "CCC"], "basket_size": 2, "max_open_books": 2}

    def test_rank_filters_native_contract_liquidity_range_and_spread(self):
        contracts = [dict(CONTRACT, target_currency=coin) for coin in ("AAA", "BBB", "CCC")]
        rows = [ticker("AAA"), ticker("BBB", volume=1), ticker("CCC", bid=100, ask=101)]
        result, reasons = ranked(contracts, rows, self.config())
        self.assertEqual([row["coin"] for row in result], ["AAA"])
        self.assertEqual(reasons["BBB"], "turnover")
        self.assertEqual(reasons["CCC"], "spread")

    def test_rank_only_fills_vacancies_and_disqualified_holdings_wind_down(self):
        candidates = [dict(coin="BBB", score=3), dict(coin="AAA", score=2), dict(coin="CCC", score=1)]
        plan = retain(["AAA", "CCC"], {"AAA", "OLD"}, candidates, 2)
        self.assertEqual(plan["selected"], ["AAA", "CCC"])
        self.assertEqual(plan["wind_down"], ["OLD"])
        self.assertEqual(plan["watch"], ["AAA", "CCC", "OLD"])

    def test_eligible_holding_cannot_disappear_when_the_basket_is_full(self):
        candidates = [dict(coin="AAA", score=3), dict(coin="BBB", score=2)]
        plan = retain(["AAA"], {"BBB"}, candidates, 1)
        self.assertEqual(plan["selected"], ["AAA"])
        self.assertEqual(plan["wind_down"], ["BBB"])
        self.assertEqual(plan["watch"], ["AAA", "BBB"])


class SizingTests(unittest.TestCase):
    def config(self):
        return load()

    def test_unit_is_bounded_and_aligned_to_quantity_step(self):
        book = dict(
            bids=[{"price": "100", "qty": "1000"}],
            asks=[{"price": "100.1", "qty": "1000"}],
        )
        result = size(
            self.config(), CONTRACT, {"maker": "0.0002", "taker": "0.0002"}, book,
            equity="1000000", cash="900000", portfolio_notional="0",
        )
        self.assertIsNone(result["reason"])
        self.assertEqual(D(result["qty"]) % D(CONTRACT["qty_unit"]), 0)
        self.assertLessEqual(D(result["qty"]) * D("100"), D("50000"))
        self.assertGreater(D(result["cap_krw"]), 0)
        self.assertLessEqual(D(result["cap_krw"]) + D(result["fee_budget_krw"]), D("5000"))

    def test_unprotectable_minimum_size_is_rejected(self):
        contract = dict(CONTRACT, min_order_amount="500000")
        book = dict(
            bids=[{"price": "100", "qty": "10"}],
            asks=[{"price": "100.1", "qty": "10"}],
        )
        result = size(
            self.config(), contract, {"maker": "0", "taker": "0"}, book,
            equity="100000", cash="100000", portfolio_notional="0",
        )
        self.assertEqual(result["reason"], "minimum_order_exceeds_unit")

    def test_entry_size_is_also_bounded_by_conservative_exit_depth(self):
        book = dict(
            bids=[{"price": "100", "qty": "20"}],
            asks=[{"price": "100.1", "qty": "10000"}],
        )
        result = size(
            self.config(), CONTRACT, {"maker": "0", "taker": "0"}, book,
            equity="1000000", cash="1000000", portfolio_notional="0",
        )
        self.assertEqual(result["reason"], "minimum_order_exceeds_unit")
        self.assertEqual(result["caps"]["exit_depth"], "2.0")


class FeedTests(unittest.TestCase):
    def market(self):
        return Market("AAA", load(), CONTRACT, UNITS, {"maker": "0", "taker": "0"}, now_ms=2_000_000)

    def test_identity_staleness_and_duplicate_books_fail_closed(self):
        market = self.market()
        row = dict(
            quote_currency="KRW", target_currency="AAA", timestamp=1_999_900, id=1,
            bids=[{"price": "100", "qty": "2"}], asks=[{"price": "100.1", "qty": "3"}],
        )
        market.feed("ORDERBOOK", row, 2_000_000)
        self.assertEqual(market.counts["books"], 1)
        market.feed("ORDERBOOK", row, 2_000_001)
        self.assertEqual(market.counts["duplicate_book"], 1)
        market.feed("ORDERBOOK", {**row, "id": 2, "target_currency": "BBB"}, 2_000_002)
        self.assertEqual(market.counts["invalid"], 1)
        market.feed("ORDERBOOK", {**row, "id": 3, "timestamp": 1}, 2_000_003)
        self.assertEqual(market.counts["stale"], 1)

    def test_seller_maker_trade_is_aggressive_buy_and_deduplicated(self):
        market = self.market()
        book = dict(
            quote_currency="KRW", target_currency="AAA", timestamp=1_999_900, id=1,
            bids=[{"price": "100", "qty": "2"}, {"price": "99.99", "qty": "2"}],
            asks=[{"price": "100.1", "qty": "3"}, {"price": "100.2", "qty": "3"}],
        )
        market.feed("ORDERBOOK", book, 2_000_000)
        trade = dict(
            quote_currency="KRW", target_currency="AAA", timestamp=2_000_000,
            id="trade-1", price="100.1", qty="1.25", is_seller_maker=True,
        )
        market.feed("TRADE", trade, 2_000_010)
        self.assertEqual(market.features.b, 1.25)
        market.feed("TRADE", trade, 2_000_011)
        self.assertEqual(market.counts["duplicate_trade"], 1)


if __name__ == "__main__":
    unittest.main()
