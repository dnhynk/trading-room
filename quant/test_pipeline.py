"""Adversarial accounting, causality, execution and experiment tests."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest

from .config import Config
from .coverage import attribute_episodes, universe_coverage
from .data import Event, Funding, Reader
from .engine import Engine
from .registry import Registry
from .validation import entry_study, gates, summarize, walk_forward

T = 1788470000000


def config(**execution):
    d = Config.read(Path(__file__).parent / "configs" / "reference.json").data
    d["books"] = {"XUSDT": {"sides": ["long", "short"], "qstep": .01, "tick": .01}}
    d["execution"].update(execution)
    return Config.create(d)


def event(t, channel="books15", bid=100, ask=101, trades=None, mark=None, depth=100):
    if channel == "books15":
        data = [dict(bids=[[str(bid - i * .01), str(depth)] for i in range(5)],
                     asks=[[str(ask + i * .01), str(depth)] for i in range(5)], ts=str(t))]
    elif channel == "trade":
        data = trades or [dict(price=str(bid), size="1000", side="sell")]
    else:
        data = [dict(markPrice=str(mark))]
    return Event(t, "XUSDT", channel, dict(ts=t, action="update", arg=dict(channel=channel, instId="XUSDT"), data=data))


def engine():
    e = Engine(config(), observe=False)
    e.process(event(T))
    b = e.books["XUSDT:long"]
    b.strategy.p.update(unit_qty=.7, cap_usdt=5.0, max_notional=75)
    b.risk = 5
    return e, b


def entered(e, b, qty=.5, price=100):
    assert e.reserve(b, qty, price)
    order = dict(oid="test-entry", qty=qty, filled=0, lot=None)
    e.fill(b, "buy", qty, price, True, order)
    b.stop = price - b.s * 10
    b.strategy.arm = None


class ConfigurationTests(unittest.TestCase):
    def test_nested_values_cannot_mutate_config(self):
        c = config()
        old = c.id
        c.data["strategy"]["unit_frac"] = 50
        self.assertEqual(c.id, old)
        self.assertEqual(c.data["strategy"]["unit_frac"], .75)

    def test_live_or_expanded_risk_cannot_load(self):
        for key, value in (("mode", "live"), ("max_units", 3), ("unit_frac", 2), ("cap_frac", .1)):
            with self.subTest(key=key):
                d = config().data
                d["strategy"][key] = value
                with self.assertRaises(ValueError):
                    Config.create(d)

    def test_no_missing_defaults_nan_or_zero_latency(self):
        d = config().data
        del d["signal"]["v_hl"]
        with self.assertRaises(ValueError):
            Config.create(d)
        with self.assertRaises(ValueError):
            config(latency_ms=0)
        with self.assertRaises(ValueError):
            config(taker=float("nan"))


class ReaderTests(unittest.TestCase):
    def line(self, e):
        return f"{e.t}\t{json.dumps(e.message)}\n"

    def test_reception_order_duplicate_and_late_messages(self):
        r = Reader(["XUSDT"])
        first = self.line(event(T))
        self.assertIsNotNone(r.parse(first))
        self.assertIsNone(r.parse(first))
        self.assertIsNone(r.parse(self.line(event(T - 1))))
        self.assertEqual(r.quality["duplicate_messages"], 1)
        self.assertEqual(r.quality["reception_regressions"], 1)

    def test_damage_is_visible_and_crossed_book_not_used(self):
        r = Reader(["XUSDT"])
        for raw in ("torn\n", self.line(event(T, bid=102, ask=101)), self.line(event(T + 1, bid=float("nan")))):
            self.assertIsNone(r.parse(raw))
        self.assertEqual(r.quality["invalid_messages"], 3)

    def test_channel_time_regression_is_not_reordered_into_past(self):
        r = Reader(["XUSDT"])
        r.parse(self.line(event(T)))
        old = event(T + 100)
        old.message["data"][0]["ts"] = str(T - 20)
        self.assertIsNone(r.parse(self.line(old)))
        self.assertEqual(r.quality["late_channel_messages"], 1)

    def test_funding_coverage_and_duplicates(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "funding.json"
            d = dict(source="exchange-settlements-export", coverage_start=T, coverage_end=T + 10000,
                     symbols=["XUSDT"], settlements=[dict(t=T + 1000, symbol="XUSDT", rate=.001, mark=100)])
            path.write_text(json.dumps(d))
            f = Funding(path)
            self.assertTrue(f.covers(T, T + 9999, ["XUSDT"]))
            self.assertFalse(f.covers(T, T + 10001, ["XUSDT"]))
            self.assertFalse(Funding().covers(T, T + 1, ["XUSDT"]))
            d["settlements"] *= 2
            path.write_text(json.dumps(d))
            with self.assertRaises(ValueError):
                Funding(path)


class ExecutionTests(unittest.TestCase):
    def test_reference_policy_two_element_entry_contract(self):
        e, b = engine()
        e.reconcile(b, dict(buy=(100, .5), trim=None, stop=None))
        self.assertEqual(b.work["buy"]["kind"], "maker")
        e.reconcile(b, dict(buy=(100, .5), trim=None, stop=None))
        self.assertNotIn("cancel_at", b.work["buy"])

    def test_no_fill_on_signal_or_acknowledgement_message(self):
        e, b = engine()
        e.new_order(b, "buy", (100, .5, "maker"))
        e.process(event(T + 100, "trade", bid=99))
        self.assertEqual(b.qty, 0)
        e.process(event(T + 1000))
        self.assertEqual(b.qty, 0)
        e.process(event(T + 2100, "trade", bid=99))
        self.assertEqual(b.qty, .5)
        self.assertAlmostEqual(e.cash, 100 - .5 * 100 * .0002)

    def test_crossed_on_arrival_cancels(self):
        e, b = engine()
        e.new_order(b, "buy", (100, .5, "maker"))
        e.process(event(T + 1000, bid=98, ask=99))
        self.assertEqual(b.qty, 0)
        self.assertIsNone(b.work["buy"])

    def test_cancel_in_flight_can_still_fill(self):
        e, b = engine()
        e.new_order(b, "buy", (100, .5, "maker"))
        e.process(event(T + 1000))
        b.work["buy"]["cancel_at"] = T + 4000
        e.process(event(T + 2500, "trade", bid=99))
        self.assertEqual(b.qty, .5)

    def test_acknowledged_cancel_cannot_fill(self):
        e, b = engine()
        e.new_order(b, "buy", (100, .5, "maker"))
        e.process(event(T + 1000))
        b.work["buy"]["cancel_at"] = T + 2000
        e.process(event(T + 2100, "trade", bid=99))
        self.assertEqual(b.qty, 0)

    def test_stale_quotes_do_not_fill_market_exit(self):
        e, b = engine()
        entered(e, b)
        e.new_order(b, "trim", (100, .5, "taker"))
        e.process(event(T + 6000, "trade", bid=99))
        self.assertEqual(b.qty, .5)
        e.process(event(T + 6100, bid=99, ask=100))
        self.assertEqual(b.qty, 0)

    def test_market_exit_uses_depth_and_waits_for_remaining(self):
        e, b = engine()
        entered(e, b, .5)
        e.new_order(b, "trim", (100, .5, "taker"))
        e.process(event(T + 1000, bid=95, ask=96, depth=.05))
        self.assertAlmostEqual(b.qty, .25)
        self.assertGreater(e.quality["insufficient_depth"], 0)
        e.process(event(T + 1100, bid=94, ask=95))
        self.assertEqual(b.qty, 0)
        self.assertLess(e.campaigns[0]["gross"], -2.5)

    def test_gap_stop_fills_beyond_trigger_in_both_directions(self):
        for side, bid, ask in (("long", 80, 81), ("short", 119, 120)):
            e, _ = engine()
            b = e.books[f"XUSDT:{side}"]
            b.risk = 5
            entered(e, b)
            e.process(event(T + 1000, bid=bid, ask=ask))
            self.assertTrue(b.stopping)
            self.assertGreater(b.qty, 0)
            e.process(event(T + 2100, bid=bid, ask=ask))
            self.assertEqual(b.qty, 0)
            self.assertTrue(e.campaigns[0]["stop"])
            self.assertLess(e.campaigns[0]["net"], -5)

    def test_stop_can_only_tighten(self):
        e, b = engine()
        entered(e, b)
        e.reconcile(b, dict(buy=None, trim=None, stop=95))
        e.reconcile(b, dict(buy=None, trim=None, stop=85))
        self.assertEqual(b.stop, 95)


class AccountingRiskTests(unittest.TestCase):
    def test_pending_entry_reserves_global_pool(self):
        e, b = engine()
        other = e.books["XUSDT:short"]
        other.risk = 5
        e.new_order(b, "buy", (100, .5, "maker"))
        self.assertFalse(e.reserve(other, .5, 101))
        self.assertEqual(len(e.reservations), 1)

    def test_remaining_daily_budget_includes_costs(self):
        e, b = engine()
        e.day_realized = -10
        e.cash = 90
        self.assertFalse(e.reserve(b, .5, 100))

    def test_midnight_does_not_release_position_or_reservation(self):
        e, b = engine()
        entered(e, b)
        e.day_stops = 2
        e.clock((T // 86400000 + 1) * 86400000)
        self.assertEqual(e.day_stops, 0)
        self.assertIn(b.key, e.reservations)
        self.assertEqual(b.qty, .5)

    def test_overnight_marked_loss_is_not_a_new_days_loss(self):
        e, b = engine()
        entered(e, b)
        e.markets[b.symbol]["mid"] = 60
        e.clock((T // 86400000 + 1) * 86400000)
        e.account()
        self.assertFalse(e.halted)

    def test_order_price_cannot_push_notional_above_ceiling(self):
        e, b = engine()
        b.strategy.p["unit_qty"] = 1
        e.new_order(b, "buy", (110, 1))
        self.assertLessEqual(b.work["buy"]["qty"] * 110, 75)

    def test_atr_quantity_is_floored_and_shrinks_with_capital(self):
        e, b = engine()
        e.resize(b, dict(atr=1, mid=100))
        self.assertEqual(b.strategy.p["unit_qty"], .33)
        e.cash = 50
        e.resize(b, dict(atr=1, mid=100))
        self.assertEqual(b.strategy.p["unit_qty"], .16)
        e.cash = .1
        e.resize(b, dict(atr=1, mid=100))
        self.assertEqual(b.strategy.p["unit_qty"], 0)

    def test_fees_funding_partial_fills_and_campaign_conservation(self):
        e, b = engine()
        entered(e, b)
        e.funding(dict(t=T + 10, symbol="XUSDT", mark=100, rate=.001))
        order = dict(oid="exit", qty=.5, filled=0, lot=None)
        e.fill(b, "trim", .2, 102, False, order)
        self.assertEqual(len(e.campaigns), 0)
        self.assertEqual(len(e.result()["unfinished"]), 1)
        e.fill(b, "trim", .3, 103, False, order)
        self.assertEqual(len(e.campaigns), 1)
        c = e.campaigns[0]
        expected = .2 * 2 + .3 * 3 - .01 - (20.4 + 30.9) * .0006 - .05
        self.assertAlmostEqual(c["net"], expected)
        self.assertAlmostEqual(e.cash - 100, c["net"])
        self.assertAlmostEqual(c["gross"] - c["fees"] + c["funding"], c["net"])

    def test_equity_includes_open_losses(self):
        e, b = engine()
        entered(e, b)
        e.markets[b.symbol]["mid"] = 50
        e.account()
        self.assertTrue(e.halted)
        self.assertGreater(e.max_dd, 25)
        self.assertLess(e.result()["marked_equity"], 75)


class EvidenceTests(unittest.TestCase):
    def test_concurrent_registration_cannot_reuse_one_future_window(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "trials.sqlite"
            Registry(path).close()
            barrier, outcomes = threading.Barrier(2), []
            def register(offset):
                r = Registry(path)
                try:
                    barrier.wait()
                    r.register(config(), {"code": {}}, dict(start=T + 1000, end=T + 10000), True, created=T + offset)
                    outcomes.append("registered")
                except ValueError:
                    outcomes.append("rejected")
                finally:
                    r.close()
            threads = [threading.Thread(target=register, args=(i,)) for i in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())
            self.assertCountEqual(outcomes, ["registered", "rejected"])

    def test_universe_sidecar_cannot_certify_a_missing_coin_or_gap(self):
        spec = dict(source="dated-listing-universe", intervals=[dict(start=T, end=T + 10000, symbols=["XUSDT"])])
        valid = universe_coverage(spec, {"XUSDT": [T, T + 4000, T + 8000, T + 10000]}, T, T + 10000, ["XUSDT"], 5000)
        self.assertTrue(valid["complete"])
        missing = universe_coverage(spec, {"XUSDT": [T, T + 10000]}, T, T + 10000, ["XUSDT"], 5000)
        self.assertFalse(missing["complete"])
        missing = universe_coverage(spec, {"XUSDT": [T, T + 4000, T + 8000]}, T, T + 10000, ["XUSDT", "YUSDT"], 5000)
        self.assertFalse(missing["complete"])

    def test_episode_attribution_preserves_cross_day_campaign(self):
        c = dict(symbol="XUSDT", t0=T, t1=T + 86400000, net=-1)
        spec = dict(source="episode-protocol", episodes=[dict(id="pump-1", symbol="XUSDT", start=T - 1, end=T + 86400001)])
        runs = dict(reference=dict(campaigns=[c], unfinished=[], end=c["t1"]))
        out = attribute_episodes(spec, runs, config().data["validation"])
        self.assertTrue(out["complete"])
        self.assertEqual(c["episode"], "pump-1")
        self.assertEqual(out["interval"]["blocks"], 1)
    def test_high_win_rate_can_have_negative_expectancy(self):
        c = dict(t0=T, wallet=100, fees=0, funding=0)
        cs = [dict(c, net=1) for _ in range(9)] + [dict(c, net=-20)]
        run = dict(campaigns=cs, unfinished=[], marked_equity=89, initial_equity=100)
        s = summarize(run, config().data["validation"])
        self.assertEqual(s["win_rate"], .9)
        self.assertLess(s["expectancy_usdt"], 0)
        self.assertEqual(s["day_net_interval"]["lower"], None)

    def test_horizon_is_clock_time_and_gap_censors(self):
        p = config().data["validation"]
        p["horizon_s"] = 2
        obs = [dict(t=T + i * 1000, symbol="XUSDT", bid=100 + i, ask=101 + i, signals=["long"] if i == 0 else []) for i in range(5)]
        result = entry_study(obs, config().data["execution"], p)
        self.assertEqual(result["signals"], 1)
        obs[3]["t"] += 10000
        obs = sorted(obs, key=lambda r: r["t"])
        # Separate case: endpoint exists but the route contains a long data outage.
        p["horizon_s"] = 15
        obs = [dict(t=T + i, symbol="XUSDT", bid=100, ask=101, signals=["long"] if i == 0 else []) for i in (0, 1000, 16000)]
        result = entry_study(obs, config().data["execution"], p)
        self.assertEqual(result["signals"], 0)
        self.assertEqual(result["censored"], 1)

    def test_one_episode_cannot_cross_fold_boundary(self):
        rows = [dict(episode="a", start=0, end=100), dict(episode="b", start=50, end=300),
                dict(episode="b", start=400, end=500), dict(episode="c", start=410, end=600)]
        fold = walk_forward(rows, 200, 350, 700, 50)
        self.assertEqual(fold, dict(train=["a"], validation=["c"], purged=["b"]))

    def test_positive_development_run_still_cannot_promote(self):
        e, _ = engine()
        run = e.result()
        report = dict(runs=dict(reference=run, cost_stress=copy.deepcopy(run)), data_quality={}, funding_complete=False,
                      entry_study=dict(excess_day_interval=dict(blocks=0, lower=None)))
        gate = gates(report, config())
        self.assertEqual(gate["status"], "HOLD")
        self.assertIn("development_or_unregistered_window", gate["reasons"])
        self.assertIn("funding_incomplete", gate["reasons"])
        self.assertFalse(gate["live_authorized"])

    def test_registration_is_single_use_and_rejects_reused_holdout(self):
        with tempfile.TemporaryDirectory() as temp:
            r = Registry(Path(temp) / "trials.sqlite")
            cfg = config()
            trial = r.register(cfg, {"code": {}}, dict(start=T + 1000, end=T + 10000), True, created=T)
            with self.assertRaises(ValueError):
                r.register(cfg, {"code": {}}, dict(start=T + 2000, end=T + 20000), True, created=T + 1)
            r.claim(trial, cfg, {})
            with self.assertRaises(ValueError):
                r.claim(trial, cfg, {})
            r.finish(trial, {"negative_result": True})
            self.assertEqual(r.list()[0]["status"], "finished")
            r.close()

    def test_registration_rejects_past_window_and_changed_code(self):
        with tempfile.TemporaryDirectory() as temp:
            r = Registry(Path(temp) / "trials.sqlite")
            with self.assertRaises(ValueError):
                r.register(config(), {"code": {}}, dict(start=T - 1, end=T + 1), True, created=T)
            trial = r.register(config(), {"code": {"a": "old"}}, created=T)
            with self.assertRaises(ValueError):
                r.claim(trial, config(), {"a": "new"})
            r.close()


if __name__ == "__main__":
    unittest.main()
