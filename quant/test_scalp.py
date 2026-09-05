"""Scalp invalidation, timer races, domestic FIFO, fees and input causality."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from .collect import candles
from .compare import PhaseSchedule, entry_reading, load_seed, variants
from .config import Config
from .data import Event
from .markouts import Markouts
from .scalp import ResearchEngine
from .test_pipeline import T, config, event, entered
from .venues import BITHUMB_TICKS, BITHUMB_END, Normalizer, research_config, round_price


def cfg(venue="coinone", **research):
    c = research_config(config(), venue, {"XKRW": dict(sides=["long"], qstep=.01, tick=.01, min_order=5, price_ladder=[[0, .01]])}, 100)
    d = c.data
    d["research"].update({"regime": "none", **research})
    return Config.create(d)


def ev(t, **kwargs):
    e = event(t, **kwargs)
    e.message["arg"]["instId"] = "XKRW"
    return Event(t, "XKRW", e.channel, e.message)


def engine(c=None):
    e = ResearchEngine(c or cfg(), observe=False)
    e.process(ev(T))
    b = e.books["XKRW:long"]
    b.strategy.p.update(unit_qty=.7, cap_usdt=5, max_notional=75)
    b.risk = 5
    return e, b


def frame(t, **kwargs):
    f = dict(t=t, mid=100, bid=99.99, ask=100.01, v=0, a=.1, brk=False, bko=False,
             atr=1, atr15=3, bs10=.7, sell_decay=True, dip_low=99.5, pop_high=101,
             side_hint_15m="long", side_hint_1h="long", htf_lows=[], htf_highs=[])
    f.update(kwargs)
    return f


class ScalpTests(unittest.TestCase):
    def test_entry_diagnostics_follow_restored_gate_and_ignore_shadow_signals(self):
        f = frame(T // 1000, sell_decay=False)
        strict = dict(entry_v=1, entry_flow=1, entry_decay=1)
        sigs = [dict(sig="DIP_SLOWING", src="v")]
        self.assertFalse(entry_reading(strict, f, sigs, "long")["configured_quality"])
        self.assertTrue(entry_reading(dict(strict, entry_decay=0), f, sigs, "long")["configured_quality"])
        candle = [dict(sig="DIP_SLOWING", src="1m")]
        self.assertFalse(entry_reading(dict(strict, entry_decay=0), f, candle, "long")["configured_quality"])
        restored = dict(entry_v=0, entry_flow=0, entry_decay=0)
        self.assertTrue(entry_reading(restored, f, candle, "long")["configured_quality"])
        self.assertFalse(entry_reading(restored, f, [dict(candle[0], shadow=True)], "long")["configured_quality"])

    def test_entry_flow_diagnostics_use_current_frame_and_correct_side(self):
        p = dict(entry_v=1, entry_flow=1, entry_decay=1)
        sigs = [dict(sig="POP_STALLING", src="v", bs10=.9, buy_decay=False)]
        f = frame(T // 1000, bs10=.3, buy_decay=True)
        self.assertTrue(entry_reading(p, f, sigs, "short")["configured_quality"])
        self.assertFalse(entry_reading(p, dict(f, bs10=None), sigs, "short")["configured_quality"])
        self.assertFalse(entry_reading(p, dict(f, bs10=.5), sigs, "short")["configured_quality"])

    def test_exit_only_ablation_has_identical_entry_price_size_and_expiry(self):
        entries = []
        for kind in ("reference", "scalp_exit"):
            e, b = engine(cfg(policy=kind))
            b.strategy.now_ms = T + 1500
            b.strategy.trades.append((T, 10))
            out = b.strategy.step(frame(T // 1000), [dict(sig="DIP_SLOWING", src="v")], b.pos)
            entries.append((out["buy"], b.strategy.arm))
        self.assertIsNotNone(entries[0][0])
        self.assertEqual(entries[0], entries[1])

    def test_v1_runner_cannot_silently_ignore_domestic_execution_contract(self):
        from .engine import Engine
        with self.assertRaises(ValueError):
            Engine(cfg())

    def test_dust_remains_unfinished_and_is_not_priced_as_an_executable_exit(self):
        e, b = engine()
        entered(e, b, .01)
        e.advance(T + 60000)
        e.process(ev(T + 61000))
        out = e.result()
        self.assertEqual(b.qty, .01)
        self.assertFalse(out["campaigns"])
        self.assertFalse(out["liquidation_depth_complete"])
        self.assertIsNone(out["liquidated_estimate_net"])

    def test_fee_schedule_enters_risk_reservation(self):
        e, b = engine(cfg(fee_schedule=[[0, .0025, .0025]]))
        e.new_order(b, "buy", (100, .5))
        self.assertAlmostEqual(e.reservations[b.key], 5 + 50 * (.005 + .0005))

    def test_spot_rejects_short_and_keeps_risk_geometry(self):
        d = cfg().data
        d["books"]["XKRW"]["sides"] = ["short"]
        with self.assertRaises(ValueError):
            Config.create(d)
        e, b = engine()
        e.resize(b, frame(T / 1000, atr=2))
        self.assertLessEqual(b.strategy.p["unit_qty"], 5 / (15 * 2))
        self.assertEqual(b.strategy.p["cap_usdt"], 5)

    def test_entry_ttl_cancels_during_complete_market_silence(self):
        e, b = engine()
        b.strategy.arm = ((T + 5000) / 1000, 100, .5)
        e.new_order(b, "buy", (100, .5))
        e.advance(T + 5000)
        self.assertEqual(b.work["buy"]["cancel_at"], T + 6000)
        self.assertIn(b.key, e.reservations)
        e.advance(T + 7000)
        self.assertIsNone(b.work["buy"])
        self.assertNotIn(b.key, e.reservations)

    def test_holding_timer_uses_first_partial_fill_and_waits_for_real_quote(self):
        e, b = engine()
        entered(e, b, .2)
        first = b.strategy.first_fill_ms
        e.advance(T + 30000)
        e.fill(b, "buy", .2, 100, True, dict(oid="second", qty=.2, filled=0))
        self.assertEqual(b.strategy.first_fill_ms, first)
        e.advance(T + 60000)
        self.assertEqual(b.exit_reason, "time")
        self.assertEqual(b.work["trim"]["arrival"], T + 61000)
        self.assertAlmostEqual(b.qty, .4)
        e.process(ev(T + 61000, bid=99, ask=100))
        self.assertEqual(b.qty, 0)
        self.assertEqual(e.campaigns[0]["exit_reason"], "time")
        self.assertFalse(e.campaigns[0]["stop"])
        self.assertLess(e.campaigns[0]["net"], 0)

    def test_cancel_race_entry_is_closed_instead_of_becoming_new_long_hold(self):
        e, b = engine()
        b.strategy.arm = ((T + 5000) / 1000, 100, .5)
        e.new_order(b, "buy", (100, .5))
        e.process(ev(T + 1000))
        e.process(ev(T + 5500, channel="trade", bid=99))
        self.assertGreater(b.qty, 0)
        self.assertEqual(b.exit_reason, "entry_cancel_race")
        e.process(ev(T + 7000, bid=99, ask=100))
        self.assertEqual(b.qty, 0)

    def test_premise_needs_two_distinct_consecutive_closed_seconds_and_flow(self):
        e, b = engine()
        entered(e, b)
        p = b.strategy
        p.premise, p.now_ms = 99, T + 10000
        t = T // 1000 + 9
        p.trades.extend([(T + 8000, 10), (T + 9000, 10), (T + 10000, 10), (T + 12000, 10)])
        f = frame(t, mid=98.9, bid=98.8, ask=99, bs10=.2)
        self.assertNotIn("exit_reason", p.step(f, [], b.pos))
        self.assertNotIn("exit_reason", p.step(f, [], b.pos))
        self.assertEqual(p.bad_n, 1)
        f["t"] += 2
        p.now_ms = (f["t"] + 1) * 1000
        self.assertNotIn("exit_reason", p.step(f, [], b.pos))  # missing second resets confirmation
        f["t"] += 1
        p.now_ms = (f["t"] + 1) * 1000
        self.assertEqual(p.step(f, [], b.pos)["exit_reason"], "premise")

    def test_price_break_with_zero_trades_is_not_renewed_selling(self):
        e, b = engine()
        entered(e, b)
        p = b.strategy
        p.premise, p.now_ms = 99, T + 5000
        for second in (3, 4, 5):
            out = p.step(frame(T // 1000 + second, mid=98, bid=97.99, ask=98.01, bs10=0), [], b.pos)
            self.assertNotIn("exit_reason", out)

    def test_opposite_stall_exits_below_entry_cost(self):
        e, b = engine()
        entered(e, b)
        b.strategy.now_ms = T + 5000
        out = b.strategy.step(frame(T // 1000 + 4, mid=99, bid=98.99, ask=99.01),
                              [dict(sig="POP_STALLING", src="v")], b.pos)
        self.assertEqual(out["exit_reason"], "opposite_stall")
        self.assertIsNotNone(out["stop"])

    def test_lack_of_higher_timeframe_history_vetoes_only_entries(self):
        e, b = engine(cfg(regime="structure"))
        p = b.strategy
        p.now_ms = T + 1000
        p.trades.append((T, 10))
        f = frame(T // 1000, side_hint_1h=None)
        out = p.step(f, [dict(sig="DIP_SLOWING", src="v")], b.pos)
        self.assertIsNone(out["buy"])
        self.assertEqual(p.refusals["structure_unconfirmed"], 1)

    def test_spot_fifo_does_not_fill_from_disappearing_displayed_queue(self):
        e, b = engine()
        e.new_order(b, "buy", (100, .5))
        e.process(ev(T + 1000))
        e.process(ev(T + 2000, depth=.1))
        self.assertEqual(b.qty, 0)
        self.assertEqual(b.work["buy"]["queue"], 100)
        e.process(ev(T + 2500, channel="trade", trades=[dict(price="100", size="100", side="sell")]))
        self.assertEqual(b.qty, 0)
        e.process(ev(T + 2600, channel="trade", trades=[dict(price="100", size="2", side="sell")]))
        self.assertAlmostEqual(b.qty, .2)  # 10% participation, partial fill

    def test_cash_funded_not_leveraged_and_minimum_applies_after_sizing(self):
        e, b = engine()
        e.cash = 10
        e.day_start, b.risk = 10, .5
        e.new_order(b, "buy", (100, .7))
        self.assertAlmostEqual(b.work["buy"]["qty"], .1)
        e2, b2 = engine()
        b2.strategy.p["unit_qty"] = .01
        e2.new_order(b2, "buy", (100, .7))
        self.assertIsNone(b2.work["buy"])
        self.assertEqual(e2.counts["minimum_order_refusals"], 1)

    def test_fee_is_frozen_when_order_submitted_not_when_filled(self):
        e, b = engine(cfg(fee_schedule=[[0, 0, 0], [T + 3000, .001, .001]]))
        e.new_order(b, "buy", (100, .5))
        e.process(ev(T + 1000))
        e.process(ev(T + 4000, channel="trade", bid=99))
        self.assertAlmostEqual(e.cash, 100)
        e.stop(b, "time")
        e.process(ev(T + 6000, bid=100, ask=101))
        self.assertGreater(e.campaigns[0]["fees"], 0)

    def test_free_fee_stress_has_nonzero_fee_and_bithumb_expiry_scenarios(self):
        self.assertEqual(cfg().stressed().data["research"]["fee_schedule"][0][1:], [.0002, .0002])
        matrix = variants(cfg("bithumb"))
        self.assertEqual(matrix["scalp_none_calendar"].data["research"]["fee_schedule"][1], [BITHUMB_END, .0004, .0004])
        self.assertEqual(matrix["scalp_none_no_coupon"].data["research"]["fee_schedule"][0][1:], [.0025, .0025])


class VenueInputTests(unittest.TestCase):
    def test_coinone_unsorted_asks_zero_placeholders_and_trade_direction(self):
        n = Normalizer("coinone", ["X"])
        msg = dict(response_type="DATA", channel="ORDERBOOK", data=dict(target_currency="X", timestamp=T,
                   bids=[dict(price="100", qty="2"), dict(price="99", qty="1")],
                   asks=[dict(price="105", qty="1"), dict(price="101", qty="2"), dict(price="100.5", qty="0")]))
        row = n.parse(T, msg)
        self.assertEqual(row.message["data"][0]["asks"][0], (101, 2))
        trade = dict(response_type="DATA", channel="TRADE", data=dict(target_currency="X", timestamp=T, price="101", qty="2", id="one", is_seller_maker=True))
        self.assertEqual(n.parse(T, trade).message["data"][0]["side"], "buy")
        self.assertIsNone(n.parse(T + 1, trade))
        trade["data"].update(id="two", is_seller_maker=False)
        self.assertEqual(n.parse(T + 2, trade).message["data"][0]["side"], "sell")

    def test_bithumb_microsecond_timestamp_and_snapshot_trade_exclusion(self):
        n = Normalizer("bithumb", ["X"])
        msg = dict(type="orderbook", code="KRW-X", timestamp=T * 1000, orderbook_units=[dict(bid_price=100, bid_size=1, ask_price=101, ask_size=1)])
        self.assertEqual(n.parse(T, msg).message["ts"], T)
        trade = dict(type="trade", code="KRW-X", timestamp=T, trade_timestamp=T, trade_price=100, trade_volume=1, ask_bid="ASK", sequential_id=2, stream_type="SNAPSHOT")
        self.assertIsNone(n.parse(T, trade))

    def test_korbit_replayed_trade_id_is_not_counted_twice(self):
        n = Normalizer("korbit", ["X"])
        msg = dict(type="trade", symbol="x_krw", timestamp=T, snapshot=False,
                   data=[dict(timestamp=T, price="100", qty="1", isBuyerTaker=False, tradeId=1)])
        self.assertEqual(n.parse(T, msg).message["data"][0]["side"], "sell")
        msg["timestamp"] += 1
        self.assertIsNone(n.parse(T + 1, msg))

    def test_dynamic_tick_rounds_across_a_price_band(self):
        self.assertEqual(round_price(BITHUMB_TICKS, 99.999, up=True), 100)
        self.assertEqual(round_price(BITHUMB_TICKS, 100.7), 100)
        self.assertEqual(round_price(BITHUMB_TICKS, 100.7, up=True), 101)

    def test_rest_open_candle_never_enters_seed(self):
        raw = dict(chart=[dict(timestamp=T // 60000 * 60000, open="100", high="101", low="99", close="100", target_volume="5")])
        self.assertEqual(candles("coinone", raw, 1, T), [])

    def test_phase_is_available_after_completion_and_missing_rows_do_not_persist(self):
        t = "2026-09-05T10:00:00+09:00"
        scans = [dict(t=t, hunt=dict(ai_read=1), rows=[dict(symbol="X", phase="markup", side="long", phase_det="markdown", side_det="short")]),
                 dict(t="2026-09-05T10:01:00+09:00", rows=[])]
        p = PhaseSchedule("\n".join(json.dumps(x) for x in scans))
        from .data import epoch_ms
        start = epoch_ms(t)
        self.assertIsNone(p.side("X", start + 999, False, 1200))
        self.assertEqual(p.side("X", start + 1000, False, 1200), "long")
        self.assertEqual(p.side("X", start + 1000, True, 1200), "short")
        self.assertIsNone(p.side("X", start + 61000, False, 1200))

    def test_seed_available_after_window_is_rejected(self):
        e, _ = engine()
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "seed.json"
            p.write_text(json.dumps({"XKRW": dict(available_ms=T + 1, c1=[], c15=[])}))
            with self.assertRaises(ValueError):
                load_seed(p, T, [e])

    def test_markout_waits_for_horizon_plus_latency_and_censors_missing_depth(self):
        e, b = engine()
        study = Markouts()
        study.add("fill", "XKRW", "long", T, .5, e, price=100, entry_fee=0)
        study.quote(ev(T + 1000))
        self.assertFalse(study.values)
        study.quote(ev(T + 2000, bid=101, ask=102))
        self.assertGreater(study.result()["groups"]["fill"][1]["mean_bp"], 90)
        study.quote(ev(T + 4000, depth=.001))
        self.assertEqual(study.result()["censored"]["fill:3:exit_depth"], 1)


if __name__ == "__main__":
    unittest.main()
