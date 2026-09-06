"""Risk regressions: real failure scenarios, isolated files, no exchange calls."""
import json
import datetime as dt
import math
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from common.risk import FillLedger, floor_qty
from common.cycle import Pool, valid_params
from common.signal import STRAT, Strategy
from track_a.backtest import Engine, SimPool, size_from_equity
from tests.common.test_cycle import book


class QuantityBounds(unittest.TestCase):
    def test_rounding_and_damping_never_cross_b_risk_ceiling(self):
        cy, bk = book()
        bk.sp.update(hunt=1, unit_frac=1, cap_frac=.05, cap_min_atr=15)
        bk.dyn = dict(unit_qty=100)
        cy.acct.update(equity=50, upl_all=0)
        bk.feat.f.update(mid=10, atr=1)
        bk.resize()
        self.assertEqual(bk.sp["unit_qty"], .1)  # cap/(15 ATR) = 1/6; old damp would keep 75
        cy.acct["equity"] = 20
        bk.resize()
        self.assertEqual(bk.sp["unit_qty"], 0)  # cannot round a .066 unit UP to a .1 contract
        self.assertEqual(floor_qty(.3, .1), .3)

    def test_small_risk_reductions_are_not_hidden_by_two_percent_dead_zone(self):
        cy, bk = book(); cy.qstep, cy.vp = .001, 3
        bk.sp.update(hunt=1, unit_frac=1, cap_frac=.05, cap_min_atr=0, unit_qty=1)
        bk.dyn = dict(unit_qty=1)
        cy.acct.update(equity=2.97, upl_all=0); bk.feat.f.update(mid=3)
        bk.resize(); self.assertEqual(bk.sp["unit_qty"], .99)

    def test_replay_sizing_and_scaled_signal_do_not_round_a_subminimum_entry_up(self):
        st = size_from_equity(dict(hunt=1, unit_frac=.75, cap_frac=.05, cap_min_atr=15), 1, 100, .01, atr=1)
        self.assertEqual(st["unit_qty"], 0)
        cy, bk = book(); f = {**cy.feat.f, "v": 0, "a": 0}
        p = dict(hunt=1, unit_qty=.1, qstep=.1, against_regime_mult=1)
        s = Strategy(p); pos = dict(bk.pos, unit_mult=.5)
        d = s.step(f, [{"sig": "DIP_SLOWING", "src": "v"}], pos)
        self.assertIsNone(d["buy"])
        self.assertIn("risk_min_qty", [kw.get("why") for _, kw in d["events"]])

    def test_nan_and_infinity_cannot_disable_comparison_guards(self):
        for value in (float("nan"), float("inf"), -float("inf")):
            self.assertIn("cap_frac", valid_params({**STRAT, "cap_frac": value}, {}))
            self.assertIn("unit_qty", valid_params({**STRAT, "unit_qty": value}, {}))


class PoolSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "pool.json")

    def pool(self, states=lambda: {}):
        p = Pool("NEW", path=self.path, states=states); p.cap = 1; return p

    def write(self, value):
        with open(self.path, "w", encoding="utf-8") as fh: fh.write(value)

    def test_corrupt_pool_and_expired_lock_fail_closed_without_overwriting_evidence(self):
        self.write('{"claims":')
        p = self.pool(); self.assertEqual(p.claim(), "pool_error")
        with open(self.path, encoding="utf-8") as fh: self.assertEqual(fh.read(), '{"claims":')
        self.write('{"claims":{}}')
        with open(self.path + ".lock", "w") as fh: fh.write("old owner")
        os.utime(self.path + ".lock", (1, 1)); p.LOCK_WAIT = .05
        self.assertEqual(p.claim(), "pool_error")
        self.assertTrue(os.path.exists(self.path + ".lock"))

    def test_lost_pool_reconstructs_a_position_and_an_unconfirmed_entry(self):
        for b in (dict(pos=dict(qty=2)), dict(working=dict(buy=dict(qty=2))), dict(market_pending=True)):
            self.write('{"claims":{}}')
            p = self.pool(lambda: {"OLD": dict(mode="live", books=dict(short=b))})
            self.assertEqual(p.claim(), "pool")

    def test_missing_file_with_an_existing_live_engine_is_not_silently_initialized(self):
        p = self.pool(lambda: {"OLD": dict(mode="live", books={})})
        self.assertEqual(p.claim(), "pool_error")
        self.assertFalse(os.path.exists(self.path))

    def test_daily_stops_and_full_campaign_budget_are_shared(self):
        day = time.strftime("%Y-%m-%d", time.gmtime())
        p = self.pool(lambda: {"OLD": dict(day=day, books=dict(short=dict(stops_today=3, realized=-2)))})
        p.max_stops = 3
        self.assertEqual(p.claim(), "pool_stops")
        p.max_stops, p.limit, p.risk = 0, 5, 4
        self.assertEqual(p.claim(), "pool_budget")
        p.risk = 3; self.assertEqual(p.claim(), "")

    def test_incomplete_flat_snapshot_cannot_release_an_expired_lease(self):
        self.write(json.dumps(dict(claims=dict(OLD=dict(t=time.time()-400, risk=4)))))
        st = dict(mode="live", t=time.strftime("%Y-%m-%d %H:%M:%S"), books=dict(long=dict(pos=dict(qty=0), exch=dict(total=0))))
        p = self.pool(lambda: {"OLD": st})
        self.assertEqual(p.claim(), "pool")

    def test_other_slots_reserve_risk_and_unknown_or_invalid_risk_is_not_zero(self):
        p = self.pool(); p.cap, p.limit, p.risk = 2, 5, 3
        for risk, why in ((4, "pool_budget"), (None, "pool_budget"), (-1, "pool_error"), (float("nan"), "pool_error"), (2, "")):
            self.write(json.dumps(dict(claims=dict(OLD=dict(t=time.time(), risk=risk)))))
            self.assertEqual(p.claim(), why)

    def test_state_read_failure_does_not_stop_exit_reconciliation(self):
        cy, bk = book(lots=[[1, 3, "a"]])
        cy.pool = self.pool(lambda: (_ for _ in ()).throw(OSError("unreadable")))
        bk.check_daily(cy.feat.f)
        self.assertEqual(bk.pos["halt"], "POOL_ERROR")


class Ledger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "events.jsonl")
        with open(self.path, "w"): pass
        self.reader = FillLedger(self.path)
        self.t = time.strftime("%Y-%m-%d %H:%M:%S")

    def emit(self, **e):
        with open(self.path, "a", encoding="utf-8") as fh: fh.write(json.dumps(dict(t=self.t, symbol="X", side="long", **e)) + "\n")

    def test_partial_stops_rotation_dry_and_transfers(self):
        self.emit(ev="START", mode="live")
        self.emit(ev="FILL", pnl=-.1)
        self.emit(ev="STOP_HIT", pnl=-2, oid="s")
        self.emit(ev="STOP_HIT", pnl=-3, oid="s", partial=True)
        self.emit(ev="START", mode="live")  # new side / new process cannot erase earlier fills
        self.emit(ev="FILL", pnl=1)
        self.emit(ev="SWEEP", pnl=100)
        self.emit(ev="START", mode="dry")
        self.emit(ev="FILL", pnl=500)
        self.assertEqual(self.reader.summary(), (-4.1, 1))
        self.assertEqual(self.reader.summary(), (-4.1, 1))  # rereading adds nothing twice

    def test_a_partial_line_waits_and_a_corrupt_fill_blocks(self):
        self.emit(ev="START", mode="live")
        line = json.dumps(dict(t=self.t, symbol="X", ev="FILL", pnl=2))
        with open(self.path, "a") as fh: fh.write(line[:20])
        self.assertEqual(self.reader.summary(), (0, 0))
        with open(self.path, "a") as fh: fh.write(line[20:] + "\n")
        self.assertEqual(self.reader.summary(), (2, 0))
        with open(self.path, "a") as fh: fh.write('{"ev": "FILL", broken}\n')
        with self.assertRaises(ValueError): self.reader.summary()

    def test_stop_partials_across_utc_days_count_one_stop(self):
        self.reader.modes["X"] = "live"
        for utc, partial in (("2026-09-04T23:59:59+00:00", False), ("2026-09-05T00:00:01+00:00", True)):
            stamp = dt.datetime.fromisoformat(utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            self.reader.add(dict(ev="STOP_HIT", symbol="X", t=stamp, pnl=-1, oid="one", partial=partial))
        self.assertEqual(sum(v[1] for v in self.reader.totals.values()), 1)
        self.assertEqual(len(self.reader.totals), 2)

    def test_missing_start_cannot_silently_discard_cash_events(self):
        self.emit(ev="FILL", pnl=-3)
        with self.assertRaises(ValueError): self.reader.summary()


class ReplayRisk(unittest.TestCase):
    def test_opposite_sides_of_one_coin_cannot_share_the_same_claim(self):
        p = SimPool(2)
        b = lambda: SimpleNamespace(day_realized=0, strat=SimpleNamespace(p={}))
        long, short = p.handle("X", b()), p.handle("X", b())
        self.assertEqual(long.claim(), ""); self.assertFalse(short.mine)
        self.assertEqual(short.claim(), "pool")
        long.tend(False, False); self.assertEqual(short.claim(), "")

    def test_midnight_resets_even_the_book_whose_tape_ended_yesterday(self):
        p = SimPool(1)
        bk = SimpleNamespace(day_realized=-50, stops_today=3, pos=dict(halt="DAILY_STOPS"))
        p.books = [bk]; p.advance(86399); p.advance(86400)
        self.assertEqual((p.day_loss(), p.day_stops(), bk.pos["halt"]), (0, 0, None))

    def test_second_slot_must_fit_after_the_first_slots_reserved_loss(self):
        p = SimPool(2)
        def b(): return SimpleNamespace(day_realized=0, stops_today=0,
                                       strat=SimpleNamespace(p=dict(hunt=1, cap_usdt=3, daily_loss_limit=5)))
        first, second = p.handle("X", b()), p.handle("Y", b())
        self.assertEqual(first.claim(), "")
        self.assertEqual(second.claim(), "pool_budget")

    def test_b_stop_fills_at_the_gap_quote_not_the_old_trigger(self):
        for side, bid, ask, stop, expected in (("long", 90, 91, 95, 90), ("short", 109, 110, 105, 110)):
            eng = Engine(strat=dict(hunt=1), sides=[side], execution=dict(stop_slip=0))
            bk = eng.books[side]; bk.pos.update(lots=[[1, 100, "a"]], avg=100)
            bk.stop = stop
            eng.feat.f = dict(t=1, mid=(bid+ask)/2, bid=bid, ask=ask, mark=(bid+ask)/2)
            eng.tick_book(bk, [])
            self.assertEqual(eng.events[-1][4], expected)
            self.assertLess(bk.realized, -9.9)


if __name__ == "__main__": unittest.main()
