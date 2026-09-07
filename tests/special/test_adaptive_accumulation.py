from dataclasses import replace
from decimal import Decimal as D
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from track_special.arx_campaign.accumulation import (
    AccumulationStore, DEFAULT_SETTINGS, account_observation, paper_step,
)
from track_special.arx_campaign.execution.bitget_classic_v2 import (
    ClassicReadOnlyClient, ClassicReadError,
)
from track_special.arx_campaign.strategy.accumulation import (
    Frame, decide, market_frame, validate_config,
)
from track_special.arx_campaign.reporting.accumulation import AccumulationOutbox, fill_card
from common import notify


def config():
    return json.loads(DEFAULT_SETTINGS.read_text())


def frame(**changes):
    value = Frame(
        time_ms=1800000000000, bar_ms=1799999940000,
        bid=D("9.99"), ask=D("10"), mark=D("10"), close=D("10"),
        previous_close=D("9.99"), low=D("9.95"), previous_low=D("9.94"),
        range_low=D("9.5"), range_high=D("10.5"), atr=D("0.1"),
        contraction=D("1"), close_location=D("0.7"), buy_share=D("0.6"),
        book_share=D("0.6"), bar_quote_volume=D("100000"),
        asks=((D("10"), D("100000")),), step=D("1"), min_quantity=D("1"),
        min_notional=D("5"), max_quantity=D("10000"), taker_fee=D("0.0006"),
        mmr=D("0.025"), funding_rate=D("0.0001"), next_funding_ms=1800010000000,
    )
    return replace(value, **changes)


def state(**changes):
    value = {
        "e0": "100", "quantity": "0", "notional": "0", "fees": "0", "funding": "0",
        "stop": "0", "last_fill_price": "0", "last_fill_ms": 0,
        "last_signal_bar_ms": None, "pending": False, "terminal": False,
        "next_funding_ms": None, "previous_funding_rate": "0", "fills": 0,
    }
    return value | changes


class AdaptiveAccumulationTests(unittest.TestCase):
    def test_first_clip_is_small_even_when_pressure_is_urgent(self):
        f = frame(close=D("10.4"), buy_share=D("0.8"), book_share=D("0.8"))
        decision = decide(f, state(), config())
        self.assertEqual("BASE", decision.phase)
        self.assertGreater(decision.quantity, 0)
        self.assertLessEqual(decision.quantity * decision.worst_price, D("100"))

    def test_lower_price_absorption_reduces_average_without_enlarging_clip(self):
        existing = state(quantity="10", notional="100", fees="0.06", last_fill_price="10", stop="9.3")
        pullback = frame(bid=D("9.79"), ask=D("9.8"), mark=D("9.8"), close=D("9.8"),
                         previous_close=D("9.9"), close_location=D("0.8"),
                         asks=((D("9.8"), D("100000")),))
        result, report, events = paper_step(pullback, existing, config())
        self.assertEqual("PULLBACK", report["decision"]["phase"])
        self.assertGreater(D(result["quantity"]), 10)
        self.assertLess(D(result["notional"]) / D(result["quantity"]), 10)
        self.assertGreaterEqual(D(result["stop"]), D(existing["stop"]))
        self.assertLessEqual(D(result["notional"]) - 100, 100)
        self.assertIn("simulated_fill", [kind for kind, _ in events])

    def test_downward_price_alone_does_not_trigger_averaging(self):
        f = frame(ask=D("9.8"), bid=D("9.79"), close=D("9.75"),
                  previous_close=D("9.9"), close_location=D("0.1"), buy_share=D("0.2"))
        existing = state(quantity="10", notional="100", last_fill_price="10")
        self.assertEqual(0, decide(f, existing, config()).quantity)

    def test_below_average_permission_is_scoped_and_can_be_disabled(self):
        settings = config() | {"allow_below_average_add": False}
        decision = decide(frame(ask=D("9.8"), bid=D("9.79")),
                          state(quantity="10", notional="100"), settings)
        self.assertEqual("BELOW_AVERAGE_ADD_DISABLED", decision.reason)

    def test_duplicate_closed_bar_cannot_produce_another_fill(self):
        first, _, _ = paper_step(frame(), state(), config())
        later = replace(frame(), time_ms=frame().time_ms + 40000)
        second, _, events = paper_step(later, first, config())
        self.assertEqual(first["quantity"], second["quantity"])
        self.assertNotIn("simulated_fill", [kind for kind, _ in events])

    def test_gradual_pre_breakout_pressure_can_deploy_nearly_all_budget(self):
        current = state(quantity="10", notional="100", fees="0.06", last_fill_price="10")
        for index in range(8):
            f = frame(time_ms=frame().time_ms + index * 60000,
                      bar_ms=frame().bar_ms + index * 60000,
                      ask=D("10.4"), bid=D("10.39"), mark=D("10.4"), close=D("10.4"),
                      low=D("10.2"), previous_low=D("10.1"), buy_share=D("0.75"),
                      asks=((D("10.4"), D("100000")),))
            current, _, _ = paper_step(f, current, config())
        self.assertGreater(D(current["notional"]), 950)
        self.assertLessEqual(D(current["notional"]), 1000)
        committed = D(current["notional"]) / 10 + D(current["fees"]) + D(current["notional"]) * D("0.0006")
        self.assertLessEqual(committed, 100)
        self.assertLess(f.ask, f.range_high)

    def test_thin_book_caps_clip_and_breakout_is_not_chased(self):
        thin = frame(asks=((D("10"), D("8")), (D("11"), D("10000"))))
        self.assertLessEqual(decide(thin, state(), config()).quantity, 2)
        late = frame(ask=D("10.6"), bid=D("10.59"))
        self.assertEqual("MISSED_PRE_BREAKOUT", decide(late, state(), config()).phase)

    def test_pending_unknown_order_blocks_all_additions(self):
        self.assertEqual("UNRECONCILED_RESERVATION", decide(frame(), state(pending=True), config()).reason)

    def test_stop_has_priority_over_missing_trade_flow_and_prevents_reentry(self):
        existing = state(quantity="10", notional="100", stop="9.7", fees="0.06")
        collapse = frame(mark=D("9.5"), bid=D("9.49"), ask=D("9.5"),
                         entry_blockers=("INSUFFICIENT_RECENT_TRADE_FLOW",))
        result, _, events = paper_step(collapse, existing, config())
        self.assertTrue(result["terminal"])
        self.assertIn("simulated_exit_signal", [kind for kind, _ in events])
        self.assertEqual(0, decide(frame(), result, config()).quantity)

    def test_funding_boundary_is_not_debited_twice(self):
        f = frame()
        existing = state(quantity="10", notional="100", stop="9.3",
                         next_funding_ms=f.time_ms, previous_funding_rate="0.001")
        at_boundary = replace(f, next_funding_ms=f.time_ms, entry_blockers=("NO_FLOW",))
        first, _, _ = paper_step(at_boundary, existing, config())
        second, _, _ = paper_step(replace(at_boundary, time_ms=f.time_ms + 10000), first, config())
        self.assertEqual("0.100", first["funding"])
        self.assertEqual(first["funding"], second["funding"])

    def test_restart_and_deposit_do_not_reset_seed_and_binding_is_checked(self):
        with TemporaryDirectory() as temp:
            account = {"available_usdt": "100", "position_count": 0,
                       "open_order_count": 0, "observed_at_ms": 1}
            store = AccumulationStore(Path(temp).resolve())
            store.initialize(account, "synthetic-key-binding", "config-a")
            store.close()
            reopened = AccumulationStore(Path(temp).resolve())
            result = reopened.initialize(account | {"available_usdt": "1000"}, "synthetic-key-binding", "config-a")
            self.assertEqual("100", result["e0"])
            with self.assertRaisesRegex(ValueError, "ACCOUNT_OR_CONFIG_CHANGED"):
                reopened.initialize(account, "different-account", "config-a")
            reopened.close()

    def test_existing_reservations_cannot_initialize_full_available_seed(self):
        with TemporaryDirectory() as temp:
            store = AccumulationStore(Path(temp).resolve())
            with self.assertRaisesRegex(ValueError, "FLAT_UNRESERVED"):
                store.initialize({"position_count": 0, "open_order_count": 1}, "key", "cfg")
            store.close()

    def test_no_live_configuration_or_financial_float(self):
        for settings in (config() | {"mode": "live"}, config() | {"live_enabled": True}):
            with self.assertRaises(ValueError):
                validate_config(settings)
        with self.assertRaises(TypeError):
            validate_config(config() | {"leverage": 10.0})


class ClassicReadBoundaryTests(unittest.TestCase):
    def test_uta_and_write_endpoints_cannot_reach_network(self):
        client = ClassicReadOnlyClient("synthetic-key", "synthetic-secret", "synthetic-pass")
        with patch("track_special.arx_campaign.execution.bitget_classic_v2.build_opener") as opener:
            for path in ("/api/v3/account/assets", "/api/v2/mix/order/place-order", "/api/v2/mix/account/set-leverage"):
                with self.assertRaises(ValueError):
                    client.get(path)
            opener.assert_not_called()
        self.assertFalse(hasattr(client, "post"))

    def test_empty_null_order_lists_and_incomplete_pagination(self):
        client = ClassicReadOnlyClient("key", "secret", "pass")
        with patch.object(client, "get", return_value={"entrustedList": None, "endId": None}):
            self.assertEqual(0, client._pending_count())
        with patch.object(client, "get", return_value={"entrustedList": [{}] * 100, "endId": None}):
            with self.assertRaises(ClassicReadError):
                client._pending_count()

    def test_account_summary_does_not_adopt_positions_or_expose_identifiers(self):
        raw = {"api_family": "classic_v2", "finished_ms": 1,
               "account": {"marginCoin": "USDT", "assetMode": "single", "available": "100", "uid": "private"},
               "positions": [{"symbol": "ARXUSDT", "holdSide": "long", "total": "2", "openPriceAvg": "10", "positionId": "private"}],
               "regular_orders": 0, "partial_orders": 0, "trigger_orders": 0, "protective_orders": 0}
        result = account_observation(raw)
        self.assertEqual("20", result["arx_long_entry_notional"])
        self.assertEqual("observed_only_not_adopted", result["ownership"])
        self.assertNotIn("private", json.dumps(result))


def public_sample():
    now = frame().time_ms
    bars = [[str(now - (32 - index) * 60000), "10", "10.2", "9.8", "10", "100", "1000"]
            for index in range(32)]
    return {
        "api_family": "classic_v2", "started_ms": now - 100, "finished_ms": now,
        "ticker": {"symbol": "ARXUSDT", "ts": str(now), "bidPr": "9.99", "askPr": "10", "markPrice": "10"},
        "book": {"ts": str(now), "asks": [["10", "100"]], "bids": [["9.99", "100"]]},
        "contract": {"symbol": "ARXUSDT", "baseCoin": "ARX", "quoteCoin": "USDT", "symbolType": "perpetual",
                     "supportMarginCoins": ["USDT"], "symbolStatus": "normal", "maxLever": "20", "sizeMultiplier": "1",
                     "minTradeNum": "1", "minTradeUSDT": "5", "maxMarketOrderQty": "10000", "takerFeeRate": "0.0006"},
        "candles": bars,
        "trades": [{"tradeId": str(i), "symbol": "ARXUSDT", "side": "buy", "ts": str(now - i * 1000), "price": "10", "size": "10"}
                   for i in range(6)],
        "tiers": [{"symbol": "ARXUSDT", "startUnit": "0", "endUnit": "5000", "leverage": "20", "keepMarginRate": "0.025"}],
        "funding": {"fundingRate": "0.001", "nextUpdate": str(now + 100000)},
    }


class CausalMarketTests(unittest.TestCase):
    def test_incomplete_future_pump_cannot_change_signal(self):
        raw = public_sample()
        before = market_frame(raw, config(), D("100"))
        raw["candles"] += [[str(raw["finished_ms"]), "10", "100", "1", "90", "10000", "900000"]]
        self.assertEqual(before, market_frame(raw, config(), D("100")))

    def test_stale_quotes_and_missing_bars_are_not_filled_in(self):
        raw = public_sample()
        raw["ticker"]["ts"] = str(raw["finished_ms"] - 30000)
        with self.assertRaisesRegex(ValueError, "STALE"):
            market_frame(raw, config(), D("100"))
        raw = public_sample()
        del raw["candles"][-5]
        with self.assertRaisesRegex(ValueError, "BAR_GAP"):
            market_frame(raw, config(), D("100"))

    def test_duplicate_trade_ids_do_not_inflate_flow(self):
        raw = public_sample()
        raw["trades"] = raw["trades"][:1] * 20
        result = market_frame(raw, config(), D("100"))
        self.assertIn("INSUFFICIENT_RECENT_TRADE_FLOW", result.entry_blockers)


class AccumulationNoticeTests(unittest.TestCase):
    def fill(self):
        _, _, events = paper_step(frame(), state(), config())
        return next(value for kind, value in events if kind == "simulated_fill")

    def test_price_and_seed_percentage_use_filled_cost_and_fixed_seed(self):
        value = self.fill()
        card = fill_card(value, frame().time_ms)
        fields = dict(card["fields"])
        expected = D(value["total_notional"]) / 10 / D(value["e0_usdt"]) * 100
        self.assertIn(f"{expected:.2f}%", fields["누적 시드 투입"])
        self.assertIn("10.00000", fields["평균 매수가"])
        self.assertIn("모의 체결", card["head"])
        with patch.object(notify, "_latest_state", side_effect=AssertionError("AB ledger must not be read")):
            blocks = notify._blocks(**card)
        self.assertEqual("header", blocks[0]["type"])
        self.assertFalse(any("누적 손익" in json.dumps(block, ensure_ascii=False) for block in blocks))

    def test_known_delivery_is_not_repeated_and_uncertain_delivery_is_retained(self):
        import sqlite3

        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        sent = []
        def sender(**kwargs):
            sent.append(kwargs)
            return (200, "ok")
        outbox = AccumulationOutbox(connection, "synthetic-env", sender)
        card = fill_card(self.fill(), frame().time_ms)
        outbox.enqueue("fill:1", card)
        outbox.enqueue("fill:1", card)
        self.assertEqual({"sent": 1}, outbox.flush(1))
        outbox.flush(2)
        self.assertEqual(1, len(sent))
        def uncertain(**kwargs):
            raise notify.NotificationError("test uncertainty", uncertain=True)
        outbox.sender = uncertain
        outbox.enqueue("fill:2", card)
        self.assertEqual({"sent": 1, "unknown": 1}, outbox.flush(3))
        restarted = AccumulationOutbox(connection, "synthetic-env", sender)
        restarted.flush(1000)
        self.assertEqual(1, len(sent))
        connection.close()


if __name__ == "__main__":
    unittest.main()
