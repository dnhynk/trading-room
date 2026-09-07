"""Invariants of common/signal.py (pure).  python -m unittest tests.common.test_signal -v"""
from collections import deque
import unittest
from common.signal import Features, Strategy, apply_fill, pos_stats, zigzag, sim_match, sim_book, book_params, effective_fast_velocity


class DepthVelocityThreshold(unittest.TestCase):
    def test_zero_knobs_preserve_the_historical_threshold(self):
        p = dict(v_fast=1.0, dip_min_atr=3.0, v_fast_floor=0.0, v_depth_elasticity=0.0)
        self.assertEqual(effective_fast_velocity(p, 3.0), 1.0)
        self.assertEqual(effective_fast_velocity(p, 30.0), 1.0)

    def test_threshold_relaxes_continuously_with_depth_and_has_a_floor(self):
        p = dict(v_fast=1.0, dip_min_atr=3.0, v_fast_floor=0.75, v_depth_elasticity=0.5)
        self.assertEqual(effective_fast_velocity(p, 3.0), 1.0)
        self.assertAlmostEqual(effective_fast_velocity(p, 4.0), (3 / 4) ** 0.5)
        self.assertEqual(effective_fast_velocity(p, 6.0), 0.75)
        self.assertEqual(effective_fast_velocity(p, 30.0), 0.75)

    @staticmethod
    def detector(**overrides):
        feat = Features(dict(vol_hl=1, v_hl=1, a_lag=1, hold_s=1, c1_on=0,
                             s8_dip=0, s8_pop=0, **overrides))
        feat.mid, feat.bid, feat.ask, feat.mark = 94.0, 93.9, 94.1, 94.0
        feat.bids = [(93.9 - i / 10, 10.0) for i in range(5)]
        feat.asks = [(94.1 + i / 10, 10.0) for i in range(5)]
        feat.sec, feat.atr, feat.atr15 = 1000, 1.0, 2.0
        feat.mids = deque([100.0] * 120 + [94.0], maxlen=feat.p["brk_lookback"])
        feat.flow = deque([(1.0, 1.0)] * 60, maxlen=600)
        feat.dbid = deque([50.0] * 60, maxlen=60)
        feat.dask = deque([50.0] * 60, maxlen=60)
        feat.var.v, feat.var.n = 1e-6, 1
        feat.vraw.v, feat.vraw.n = 0.0, 1
        feat.vh = deque([-1.0], maxlen=2)
        feat.dip["minv"] = -0.8
        return feat

    def test_detector_uses_the_effective_threshold_and_records_it(self):
        self.assertFalse(any(x.get("src") == "v" for x in self.detector()._close()))
        out = [x for x in self.detector(v_fast_floor=0.75, v_depth_elasticity=0.5)._close()
               if x["sig"] == "DIP_SLOWING" and x.get("src") == "v"]
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["v_fast_eff"], 0.75)

class FillModel(unittest.TestCase):
    def test_cancellations_at_our_level_drain_the_queue_ahead_of_us(self):
        w = dict(px=3.0, qty=70, filled=0.0, queue=100.0, S=100.0, seen=0.0, t=1)      # placed behind 100 on the bid
        self.assertEqual(sim_book([("A", w, True)], [[3.0, 40.0], [2.999, 500.0]], [[3.001, 5.0]], t=2), [])   # 60 gone with no print: cancelled, uniformly over the level
        self.assertAlmostEqual(w["queue"], 40.0)
        out = sim_match([("A", w, True)], 3.0, 45.0, "sell", 0.1, t=5)                   # 45 hit the bid: 40 ahead of us, 5 for us
        self.assertEqual([(k, f) for k, _, f in out], [("A", 5.0)]); w["filled"] += 5
        self.assertAlmostEqual(w["seen"], 45.0)
        sim_book([("A", w, True)], [[3.0, 20.0]], [[3.001, 5.0]], t=5)                  # the drop 40 -> 20 is what the print took: no cancel, no change
        self.assertAlmostEqual(w["queue"], 0.0); self.assertEqual(w["seen"], 0.0)

    def test_a_touch_worse_than_our_price_means_nobody_ahead_and_deeper_levels_are_unknown(self):
        w = dict(px=3.0, qty=70, filled=0.0, queue=100.0, S=100.0, seen=0.0, t=1)
        sim_book([("A", w, True)], [[3.010, 9.0], [3.009, 9.0], [3.008, 9.0], [3.007, 9.0], [3.006, 9.0]], [[3.011, 1.0]], t=2)   # our bid is below the shown depth
        self.assertEqual(w["queue"], 100.0)
        sim_book([("A", w, True)], [[2.999, 50.0]], [[3.001, 1.0]], t=2)                # best bid under our price: our level emptied
        self.assertEqual(w["queue"], 0.0)
        self.assertEqual([f for _, _, f in sim_match([("A", w, True)], 3.0, 1.0, "sell", 0.1, t=5)], [1.0])
        a = dict(px=3.0, qty=70, filled=0.0, queue=10.0, S=10.0, seen=0.0, t=1)        # an ask: mirror
        sim_book([("A", a, False)], [[2.999, 1.0]], [[3.002, 5.0]], t=2); self.assertEqual(a["queue"], 0.0)

    def test_an_opposite_touch_at_our_price_cancels_in_the_placement_second_and_fills_later(self):
        w = dict(px=3.0, qty=50, filled=0.0, queue=10.0, S=10.0, seen=0.0, t=100)     # a bid placed at second 100
        self.assertEqual(sim_book([("A", w, True)], [[2.998, 9.0]], [[3.0, 4.0], [3.001, 8.0]], t=100, qstep=0.1), [("A", w, None)])   # the ask came to 3.0 with no print: crossed on arrival
        w = dict(px=3.0, qty=50, filled=0.0, queue=10.0, S=10.0, seen=3.0, t=100)     # same second, but the level traded: the order was on the book
        self.assertEqual([(k, f) for k, _, f in sim_book([("A", w, True)], [[2.998, 9.0]], [[3.0, 4.0], [3.001, 8.0]], t=100, qstep=0.1)], [("A", 4.0)])   # the 4 shown at 3.0 match us
        w = dict(px=3.0, qty=50, filled=0.0, queue=10.0, S=10.0, seen=0.0, t=100)
        self.assertEqual([(k, f) for k, _, f in sim_book([("A", w, True)], [[2.997, 9.0]], [[2.999, 30.0], [3.0, 30.0]], t=101, qstep=0.1)], [("A", 50.0)])   # a second later, asks through our price: filled whole
        a = dict(px=3.0, qty=50, filled=0.0, queue=10.0, S=10.0, seen=0.0, t=100)     # an ask: mirror
        self.assertEqual([(k, f) for k, _, f in sim_book([("A", a, False)], [[3.0, 7.0]], [[3.002, 9.0]], t=101, qstep=0.1)], [("A", 7.0)])

    def test_a_print_through_the_price_in_the_first_second_is_a_post_only_cancel_not_a_fill(self):
        w = dict(px=3.0, qty=50, filled=0.0, queue=10.0, t=100)
        self.assertEqual(sim_match([("A", w, True)], 2.999, 1.0, "sell", 0.1, t=100.5), [("A", w, None)])   # crossed before it was on the book
        self.assertEqual([f for _, _, f in sim_match([("A", w, True)], 2.999, 1.0, "sell", 0.1, t=101.0)], [50.0])   # a second later: a fill
        self.assertEqual([f for _, _, f in sim_match([("A", w, True)], 2.999, 1.0, "sell", 0.1)], [50.0])   # no print time: the old rule (tests)

    def test_one_print_is_shared_by_orders_of_two_books_at_the_same_price(self):
        a = dict(px=3.0, qty=70, filled=0.0, queue=10.0, t=1); b = dict(px=3.0, qty=70, filled=0.0, queue=10.0, t=2)   # long add + short trim, both on the bid
        out = sim_match([("A", a, True), ("B", b, True)], 3.0, 15.0, "sell", 0.1)
        self.assertEqual([(k, f) for k, _, f in out], [("A", 5.0)]); a["filled"] += 5   # 10 ahead, 5 left for A, nothing for B (not 5 + 5)
        out = sim_match([("A", a, True), ("B", b, True)], 3.0, 15.0, "sell", 0.1)
        self.assertEqual([(k, f) for k, _, f in out], [("A", 15.0)]); a["filled"] += 15   # A still ahead of B in the queue
        self.assertEqual(sim_match([("A", a, True)], 3.0, 15.0, "buy", 0.1), [])  # a buy aggressor does not hit the bid
        self.assertEqual([f for _, _, f in sim_match([("A", a, True)], 2.999, 1.0, "sell", 0.1)], [50.0])   # a print through the price fills the rest

def F(**kw):
    f = dict(t=100, mid=3.0, bid=2.999, ask=3.001, v=0.1, a=0.1, bko=False, brk=False, atr=0.02, atr15=0.06, htf_lows=[2.85], htf_highs=[3.15])
    f.update(kw); return f

class EntryGate(unittest.TestCase):
    """Entry quality (2026-09-04, track B): a confirmed entry is the velocity rule's stall (`entry_v`) with the last 10 s of aggressor flow
    turned against the move (`entry_flow`), optionally with the volume that made the move fading (`entry_decay`). An unconfirmed entry is
    vetoed or sized entry_mult x (first unit) / add_mult x (add). All 0 = the old behaviour; trims listen to every detector."""
    def flat(self): return dict(lots=[], avg=None, last=None, last_buy_px=None, last_trim_px=None, pause=False)

    def test_entries_listen_to_one_detector_and_trims_to_every_one(self):
        st = Strategy(dict(side="long", unit_qty=70, cap_usdt=20, stop_structural_on=0, entry_v=1)); pos = self.flat()
        r = st.step(F(bs10=0.7), [dict(sig="DIP_SLOWING", src="1m")], pos)
        self.assertIsNone(st.arm); self.assertEqual([e[1]["why"] for e in r["events"] if e[0] == "SKIP"], ["src:v"])
        st.step(F(t=101, bs10=0.7), [dict(sig="DIP_SLOWING", src="v")], pos); self.assertIsNotNone(st.arm)
        st2 = Strategy(dict(side="long", unit_qty=70, cap_usdt=20, stop_structural_on=0, entry_v=1))
        pos2 = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        r = st2.step(F(mid=3.03, bid=3.029, ask=3.031), [dict(sig="POP_STALLING", src="1m")], pos2)      # a 1m stall still sells the unit
        self.assertIsNotNone(r["trim"])
        st3 = Strategy(dict(side="long", unit_qty=70, cap_usdt=20, stop_structural_on=0)); pos3 = self.flat()   # all off: the 1m rule arms as before
        st3.step(F(bs10=0.2), [dict(sig="DIP_SLOWING", src="1m")], pos3); self.assertIsNotNone(st3.arm)

    def test_the_flow_gate_needs_the_last_ten_seconds_against_the_move_on_either_side(self):
        for side, sig, good, bad in (("long", "DIP_SLOWING", 0.7, 0.3), ("short", "POP_STALLING", 0.3, 0.7)):
            st = Strategy(dict(side=side, unit_qty=70, cap_usdt=20, stop_structural_on=0, entry_flow=1)); pos = self.flat()
            r = st.step(F(bs10=bad), [dict(sig=sig, src="v")], pos)
            self.assertIsNone(st.arm, side); self.assertIn("flow", [e[1]["why"] for e in r["events"] if e[0] == "SKIP"])
            st.step(F(t=200, bs10=good), [dict(sig=sig, src="v")], pos); self.assertIsNotNone(st.arm, side)
            st4 = Strategy(dict(side=side, unit_qty=70, cap_usdt=20, stop_structural_on=0, entry_flow=1)); pos4 = self.flat()
            st4.step(F(), [dict(sig=sig, src="v")], pos4); self.assertIsNone(st4.arm)                 # no flow reading at all is not "against the move"

    def test_the_decay_gate_reads_the_side_that_made_the_move(self):
        st = Strategy(dict(side="short", unit_qty=70, cap_usdt=20, stop_structural_on=0, entry_decay=1)); pos = self.flat()
        st.step(F(buy_decay=False, sell_decay=True), [dict(sig="POP_STALLING", src="v")], pos); self.assertIsNone(st.arm)   # the pop was made by buyers: their decay is the one that counts
        st.step(F(t=200, buy_decay=True), [dict(sig="POP_STALLING", src="v")], pos); self.assertIsNotNone(st.arm)

    def test_the_first_unit_is_gated_and_adds_are_sized_by_quality(self):
        """The first unit's quality decided the stops on the 2026-09-04 grid (11 -> 1), the adds' quality only the P&L: risk is gated at the
        campaign's first unit (entry_mult 0), expected value is sized at the adds (add_mult m for an unconfirmed add, the whole unit for a confirmed one)."""
        p = dict(side="long", unit_qty=70, cap_usdt=20, stop_structural_on=0, entry_v=1, entry_flow=1, add_mult=0.5)
        st = Strategy(p); pos = self.flat()
        st.step(F(bs10=0.7), [dict(sig="DIP_SLOWING", src="1m")], pos); self.assertIsNone(st.arm)                       # flat, unconfirmed: nothing (entry_mult 0)
        st.step(F(t=101, bs10=0.7), [dict(sig="DIP_SLOWING", src="v")], pos); self.assertEqual(st.arm[2], 70.0)         # flat, confirmed: the whole unit
        pos2 = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0, pause=False)
        st2 = Strategy(p); r = st2.step(F(mid=2.9, bid=2.899, ask=2.901, bs10=0.2), [dict(sig="DIP_SLOWING", src="1m")], pos2)
        self.assertEqual(st2.arm[2], 35.0); self.assertTrue([e for e in r["events"] if e[0] == "ARM"][0][1]["scaled"])   # positioned, unconfirmed add: half
        st3 = Strategy(p); st3.step(F(mid=2.9, bid=2.899, ask=2.901, bs10=0.8), [dict(sig="DIP_SLOWING", src="v")], pos2); self.assertEqual(st3.arm[2], 70.0)   # confirmed add: whole
        st4 = Strategy({**p, "add_mult": 1.0}); st4.step(F(mid=2.9, bid=2.899, ask=2.901, bs10=0.2), [dict(sig="DIP_SLOWING", src="1m")], pos2); self.assertEqual(st4.arm[2], 70.0)   # add_mult 1: adds as before

    def test_an_unconfirmed_first_unit_can_be_a_smaller_unit_instead_of_none(self):
        st = Strategy(dict(side="long", unit_qty=70, cap_usdt=20, stop_structural_on=0, entry_flow=1, entry_mult=0.5)); pos = self.flat()
        r = st.step(F(bs10=0.2), [dict(sig="DIP_SLOWING", src="v")], pos)
        self.assertEqual(st.arm[2], 35.0); self.assertTrue([e for e in r["events"] if e[0] == "ARM"][0][1]["scaled"])     # flow against the entry: half a unit
        st2 = Strategy(dict(side="long", unit_qty=70, cap_usdt=20, stop_structural_on=0, entry_flow=1, entry_mult=0.5)); pos2 = self.flat()
        st2.step(F(bs10=0.8), [dict(sig="DIP_SLOWING", src="v")], pos2); self.assertEqual(st2.arm[2], 70.0)              # confirmed: the whole unit

class Accounting(unittest.TestCase):
    def test_exchange_style_average_and_lifo_lots(self):
        pos = dict(lots=[], last=None)
        apply_fill(pos, 1, True, 70, 3.041, "a"); apply_fill(pos, 1, True, 70, 3.016, "b")
        self.assertAlmostEqual(pos_stats(pos)[1], 3.0285)
        pnl = apply_fill(pos, 1, False, 70, 3.030, "t", 0.04)
        self.assertAlmostEqual(pnl, (3.030 - 3.0285) * 70 - 0.04, 6)       # booked vs the average ...
        self.assertAlmostEqual(pos_stats(pos)[1], 3.0285)                    # ... and the average does not move on a reduce
        self.assertEqual(pos["lots"], [[70, 3.041, "a"]])                    # LIFO: the low unit left

class Entry(unittest.TestCase):
    def test_one_signal_one_unit_across_partial_fills_and_trims(self):
        st = Strategy(dict(side="long", unit_qty=70, step_add_atr=0.5)); pos = dict(lots=[[70, 3.05, "c"], [70, 3.02, "d"]], avg=3.035, last="buy", last_buy_px=3.02)
        r = st.step(F(mid=2.98, bid=2.979, ask=2.981), [dict(sig="DIP_SLOWING")], pos); self.assertEqual(r["buy"], (2.979, 70))
        apply_fill(pos, 1, True, 30, 2.979, "e"); st.on_fill("buy", 30)
        apply_fill(pos, 1, False, 70, 2.99, "t"); st.on_fill("trim", 70)    # a trim during the arm must not inflate the remainder
        r = st.step(F(t=101, mid=2.98, bid=2.979, ask=2.981), [], pos); self.assertEqual(r["buy"], (2.979, 40))
        apply_fill(pos, 1, True, 40, 2.979, "e"); st.on_fill("buy", 40)
        r = st.step(F(t=102, mid=2.98, bid=2.979, ask=2.981), [], pos); self.assertIsNone(r["buy"]); self.assertIsNone(st.arm)

    def test_second_unit_allowed_within_budget_and_blocked_by_quantity_cap(self):
        st = Strategy(dict(side="long", unit_qty=70, max_units=2, cap_usdt=20)); pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        st.step(F(htf_lows=[2.9]), [], pos)                                   # sets the stop (premise 2.9 - 0.3 x ATR15 = 2.882); loss after the add 140 x 0.093 = 13 < 20
        r = st.step(F(t=101, mid=2.95, bid=2.949, ask=2.951, htf_lows=[2.9]), [dict(sig="DIP_SLOWING")], pos); self.assertEqual(r["events"][0][0], "ARM")
        apply_fill(pos, 1, True, 70, 2.949, "b"); st.on_fill("buy", 70)
        r = st.step(F(t=200, mid=2.9, bid=2.899, ask=2.901), [dict(sig="DIP_SLOWING")], pos); self.assertEqual(r["events"][-1][1]["why"], "max_units")

    def test_an_add_moves_the_money_cap_stop_up_never_down(self):
        st = Strategy(dict(side="long", unit_qty=70, max_units=4, cap_usdt=5, stop_structural_on=0)); pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        s1 = st.step(F(), [], pos)["stop"]; self.assertAlmostEqual(s1, 3.0 - 5 / 70, 3)
        r = st.step(F(t=101, mid=2.95, bid=2.949, ask=2.951), [dict(sig="DIP_SLOWING")], pos); self.assertEqual(r["events"][0][0], "ARM")   # no cap budget: the cap is the stop
        apply_fill(pos, 1, True, 70, 2.949, "b"); st.on_fill("buy", 70)
        s2 = st.step(F(t=102, mid=2.95, bid=2.949, ask=2.951), [], pos)["stop"]
        self.assertAlmostEqual(s2, pos_stats(pos)[1] - 5 / 140, 3); self.assertGreater(s2, s1)                # the loss at the stop is the cap again, and the stop only rose

    def test_flat_book_takes_the_next_deceleration_wherever_it_comes(self):
        st = Strategy(dict(side="long", unit_qty=70)); pos = dict(lots=[], avg=None, last=None, last_buy_px=None, last_trim_px=None)
        apply_fill(pos, 1, True, 70, 2.742, "a"); apply_fill(pos, 1, False, 70, 2.616, fee=0.1)       # entry, then stopped out (last = "trim" at the stop price)
        r = st.step(F(mid=2.64, bid=2.639, ask=2.641), [dict(sig="DIP_SLOWING")], pos)
        self.assertEqual(r["events"][0][0], "ARM")                                                     # +0.9% above the stop: a new campaign needs no gap under the exit
        st = Strategy(dict(side="long", unit_qty=70, cap_usdt=60)); pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="trim", last_trim_px=3.0, last_buy_px=2.98)
        st.step(F(), [], pos)
        r = st.step(F(t=101, mid=2.995, bid=2.994, ask=2.996), [dict(sig="DIP_SLOWING")], pos)
        self.assertEqual(r["events"][0][1]["why"], "gap_rebuy")                                        # with inventory the rebuy must sit gap_rebuy under the trim
        r = st.step(F(t=102, mid=2.99, bid=2.989, ask=2.991), [dict(sig="DIP_SLOWING")], pos)
        self.assertEqual(r["events"][0][0], "ARM")

    def test_resting_entry_is_dropped_by_state_not_by_the_clock(self):
        st = Strategy(dict(side="long", unit_qty=70, pop_min_pct=0.4, buy_ttl_s=900)); pos = dict(lots=[], last=None)
        r = st.step(F(mid=3.0, bid=2.999, ask=3.001), [dict(sig="DIP_SLOWING")], pos); self.assertEqual(r["buy"], (2.999, 70))
        r = st.step(F(t=700, mid=3.006, bid=3.005, ask=3.007), [], pos); self.assertIsNotNone(r["buy"]); self.assertIsNotNone(st.arm)      # a 10-min stall 0.2% above the signal: the same state, the order stays (a 90 s clock would have dropped it)
        r = st.step(F(t=701, mid=3.013, bid=3.012, ask=3.014), [], pos)
        self.assertIn(("DISARM", dict(why="left", mid=3.013)), r["events"]); self.assertIsNone(r["buy"]); self.assertIsNone(st.arm)        # +0.43% = a pop-sized bounce: the signal is consumed
        st.step(F(t=1000, mid=3.0, bid=2.999, ask=3.001), [dict(sig="DIP_SLOWING")], pos); self.assertIsNotNone(st.arm)
        r = st.step(F(t=1900, mid=3.0, bid=2.999, ask=3.001), [], pos); self.assertIn(("DISARM", dict(why="ttl")), r["events"])            # the clock only bounds a stall that never resolves

class Stops(unittest.TestCase):
    def test_stop_never_loosens_after_an_add(self):
        st = Strategy(dict(side="long", unit_qty=70, cap_usdt=20, stop_structural_on=0)); pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        s1 = st.step(F(), [], pos)["stop"]; self.assertAlmostEqual(s1, 3.0 - 20 / 70, 3)
        apply_fill(pos, 1, True, 70, 2.5, "b")
        s2 = st.step(F(t=101, mid=2.5, bid=2.499, ask=2.501), [], pos)["stop"]; self.assertEqual(s2, s1)

    def test_existing_stop_is_not_moved_when_price_reaches_it(self):
        st = Strategy(dict(side="long", unit_qty=70, cap_usdt=20)); pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        s1 = st.step(F(), [], pos)["stop"]
        r = st.step(F(t=101, mid=s1 - 0.01, bid=s1 - 0.011, ask=s1 - 0.009), [], pos)
        self.assertEqual(r["stop"], s1); self.assertFalse(any(e[0] == "STOP_INVALID" for e in r["events"]))

    def test_structural_stop_leaves_room_for_the_add_ladder(self):
        # step = max(0.5%, 0.7 x ATR15/px) = 1.43%; 1 unit of 4 -> 3 adds -> the level must be >= 4.3% under the last buy
        st = Strategy(dict(side="long", unit_qty=70, cap_usdt=20, stop_trail=1)); pos = dict(lots=[[70, 2.929, "a"]], avg=2.929, last="buy", last_buy_px=2.929)
        r = st.step(F(mid=2.93, htf_lows=[2.905, 2.80, 2.70]), [], pos)
        self.assertAlmostEqual(st.struct_stop, 2.80 - 0.3 * 0.06, 3)       # 2.905 (0.8% under) sits inside the ladder: ignored; 2.80 (4.4%) is the premise (soft level)
        self.assertAlmostEqual(r["stop"], 2.929 - 20 / 70, 3)               # the exchange stop is the money cap alone (B)
        self.assertEqual([e[1]["level"] for e in r["events"] if e[0] == "STRUCT_STOP"], [2.80])
        apply_fill(pos, 1, True, 70, 2.90, "b"); pos["last_buy_px"] = 2.90    # 2 units in: 2 adds left -> room 2.9% under 2.90 -> level <= 2.816
        st.step(F(t=101, mid=2.91, htf_lows=[2.81]), [], pos); self.assertAlmostEqual(st.struct_stop, 2.81 - 0.018, 3)    # a higher qualifying low: the soft level trails
        st.step(F(t=102, mid=2.91, htf_lows=[2.85]), [], pos); self.assertAlmostEqual(st.struct_stop, 2.81 - 0.018, 3)    # 2.85 is inside the ladder: no trail
        st2 = Strategy(dict(side="long", unit_qty=70, cap_usdt=20)); pos2 = dict(lots=[[70, 2.929, "a"]], avg=2.929, last="buy", last_buy_px=2.929)
        self.assertAlmostEqual(st2.step(F(mid=2.93, htf_lows=[2.905, 2.88]), [], pos2)["stop"], 2.929 - 20 / 70, 3); self.assertIsNone(st2.struct_stop)   # nothing below the ladder: no premise level

    def test_premise_break_is_a_derisk_trigger_not_an_exchange_stop(self):
        st = Strategy(dict(side="long", unit_qty=70, cap_usdt=20, derisk_pct=3.0, derisk_core_frac=0.5, pop_min_pct=0.4)); pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        r0 = st.step(F(htf_lows=[2.85]), [], pos); self.assertAlmostEqual(st.struct_stop, 2.85 - 0.018, 3); self.assertAlmostEqual(r0["stop"], 3.0 - 20 / 70, 3)
        r1 = st.step(F(t=101, mid=2.80, bid=2.799, ask=2.801, htf_lows=[2.85]), [], pos)
        self.assertTrue(st.prem_broken); self.assertAlmostEqual(r1["stop"], 3.0 - 20 / 70, 3); self.assertIsNone(r1["trim"])   # under the premise: no stop hit, the sale waits for a bounce
        r2 = st.step(F(t=102, mid=2.92, bid=2.919, ask=2.921, htf_lows=[2.85]), [dict(sig="POP_STALLING")], pos)
        self.assertEqual(r2["trim"][1], 35); self.assertAlmostEqual(r2["stop"], 3.0 - 20 / 70, 3)          # a weak bounce within derisk_pct of the average: half the core goes, the exchange stop never moved
        apply_fill(pos, 1, False, 35, 2.92, "t", 0.04); st.on_fill("trim", 35)
        st.step(F(t=103, mid=2.92, bid=2.919, ask=2.921, htf_lows=[2.85]), [], pos)
        self.assertFalse(st.prem_broken); self.assertGreater(st.gate_eff, 0)                                  # the fill clears the evidence and the price is back above the level: normal gates

    def test_a_partial_first_fill_uses_the_full_units_cap_distance(self):
        st = Strategy(dict(side="long", unit_qty=70, cap_usdt=20, stop_structural_on=0)); pos = dict(lots=[[4.3, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        r = st.step(F(), [], pos)
        self.assertAlmostEqual(r["stop"], 3.0 - 20 / 70, 3); self.assertGreater(r["stop"], 0)   # cap over 4.3 contracts would be -1.65 (43011, needless close+HALT 2026-08-31 18:16); the unit's distance holds and risks only qty/unit x cap

    def test_no_stop_when_price_beyond_cap(self):
        st = Strategy(dict(side="long", unit_qty=70, cap_usdt=20, stop_structural_on=0)); pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        r = st.step(F(mid=2.6, bid=2.599, ask=2.601), [], pos); self.assertTrue(r["no_stop"]); self.assertIsNone(r["stop"])

class Trims(unittest.TestCase):
    def test_added_unit_sells_on_its_own_price_below_the_average(self):
        st = Strategy(dict(side="long", unit_qty=70)); pos = dict(lots=[], last=None)
        apply_fill(pos, 1, True, 70, 3.041, "a"); apply_fill(pos, 1, True, 70, 2.985, "b")
        r = st.step(F(mid=3.0, bid=2.999, ask=3.001), [dict(sig="POP_STALLING")], pos)
        self.assertEqual(r["trim"][:3], (3.001, 70, "maker")); self.assertEqual(r["events"][0][1]["mode"], "normal")

    def test_wick_top_is_sold_on_retrace_without_a_signal(self):
        st = Strategy(dict(side="long", unit_qty=70)); pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        st.step(F(t=100, mid=3.0), [], pos)
        r = st.step(F(t=101, mid=3.03, bid=3.029, ask=3.031), [], pos); self.assertIsNone(r["trim"])            # spike to +1%: nothing yet
        r = st.step(F(t=102, mid=3.024, bid=3.023, ask=3.025), [], pos); self.assertIsNone(r["trim"])           # 0.3 ATR back: still nothing
        r = st.step(F(t=103, mid=3.019, bid=3.018, ask=3.02), [], pos)                                        # 0.55 ATR back from the peak
        self.assertEqual(r["events"][-1][0], "PULL_TRIM"); self.assertEqual(r["events"][-1][1]["mode"], "retrace"); self.assertEqual(r["trim"][1], 70)
        r = st.step(F(t=104, mid=3.014, bid=3.013, ask=3.015), [], pos); self.assertEqual(r["trim"][2], "taker")   # slipping further: taker at once

    def test_derisk_does_not_take_the_retrace_exit_below_the_gate(self):
        """RULES derisk: 부분 손절은 **약반등 정체**에서. 되돌림 고점 출구는 게이트 위에서만 — 그 아래에는 고점이 없다.
        옛 코드는 derisk 게이트(−derisk_pct)를 되돌림 조건에도 써서, 래치가 켜지는 순간 진입가 아래에서 즉시 팔았다."""
        st = Strategy(dict(side="long", unit_qty=70, cap_usdt=20, derisk_pct=3.0, derisk_core_frac=0.5,
                           pop_min_pct=0.4, step_add_pct=0.5, step_add_atr=0.0))
        pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        st.step(F(t=100, mid=3.0), [], pos)
        st.step(F(t=101, mid=3.02, bid=3.019, ask=3.021), [], pos)                       # 고점 +0.67% (게이트 0.4% 위)
        r = st.step(F(t=102, mid=2.97, bid=2.969, ask=2.971), [], pos)                   # −1%: 0.5 ATR 넘게 되돌렸고 derisk 무장
        self.assertTrue(st.derisk_armed); self.assertIsNone(r["trim"])                    # 되돌림만으로는 손실에서 안 판다
        r = st.step(F(t=103, mid=2.99, bid=2.989, ask=2.991), [dict(sig="POP_STALLING")], pos)
        self.assertIsNotNone(r["trim"])                                                   # 약반등 정체가 오면 판다(코어 절반)
        self.assertEqual(r["events"][-1][1]["path"], "stall"); self.assertEqual(r["events"][-1][1]["mode"], "derisk")

    def test_a_normal_retrace_still_labels_its_path(self):
        st = Strategy(dict(side="long", unit_qty=70)); pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        st.step(F(t=100, mid=3.0), [], pos); st.step(F(t=101, mid=3.03, bid=3.029, ask=3.031), [], pos)
        r = st.step(F(t=103, mid=3.019, bid=3.018, ask=3.02), [], pos)
        self.assertEqual(r["events"][-1][1]["path"], "retrace"); self.assertEqual(r["events"][-1][1]["mode"], "retrace")

    def test_retrace_needs_the_peak_above_the_gate(self):
        st = Strategy(dict(side="long", unit_qty=70)); pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        st.step(F(t=100, mid=3.0), [], pos); st.step(F(t=101, mid=3.008, bid=3.007, ask=3.009), [], pos)      # peak only +0.27% < 0.4% gate
        r = st.step(F(t=102, mid=2.99, bid=2.989, ask=2.991), [], pos); self.assertIsNone(r["trim"])

    def test_pull_survives_a_one_tick_wiggle_under_the_gate(self):
        st = Strategy(dict(side="long", unit_qty=70, tick=0.001)); pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        st.step(F(), [], pos)
        r = st.step(F(t=101, mid=3.0125, bid=3.012, ask=3.013), [dict(sig="POP_STALLING")], pos); self.assertIsNotNone(r["trim"])      # +0.42% >= 0.4: pull
        r = st.step(F(t=102, mid=3.0115, bid=3.011, ask=3.012), [], pos); self.assertIsNotNone(st.pull); self.assertIsNotNone(r["trim"])   # one tick under the gate: the maker keeps its queue
        r = st.step(F(t=103, mid=3.0095, bid=3.009, ask=3.01), [], pos); self.assertIsNone(st.pull)                                     # three ticks under: dropped

    def test_pull_goes_taker_when_the_stall_turns(self):
        st = Strategy(dict(side="long", unit_qty=70)); pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        r = st.step(F(mid=3.02, bid=3.019, ask=3.021), [dict(sig="POP_STALLING")], pos); self.assertEqual(r["trim"][2], "maker")
        r = st.step(F(t=103, mid=3.017, bid=3.016, ask=3.018), [], pos); self.assertEqual(r["trim"][2], "taker")   # 0.13% under the pull price within 3s

    def test_trail_moves_only_by_whole_ticks(self):
        st = Strategy(dict(side="long", unit_qty=70, stop_trail=1)); pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        st.step(F(htf_lows=[2.85]), [], pos)
        r = st.step(F(t=101, htf_lows=[2.8504]), [], pos); self.assertFalse(any(e[0] == "TRAIL" for e in r["events"]))   # sub-tick creep ignored
        r = st.step(F(t=102, htf_lows=[2.852]), [], pos); self.assertTrue(any(e[0] == "TRAIL" for e in r["events"]))

    def test_a_lot_left_above_the_average_still_exits_on_the_average(self):
        """사이클이 성공하면 더 싼 로트가 덜리고 avg 는 그대로 남는다(거래소 회계) — 남은 추가 유닛이 평단보다 비싼 자리에 놓이면
        로트 기준으로는 영원히 못 파는데 포지션은 이익이다. 2026-09-01 live: 로트 [225@2.418, 225@2.387], avg 2.3597, mid 2.3655
        — 평단 +0.246%(덜면 +1.31 실현)인데 로트 −0.901%라 엔진이 이익 나는 덜기를 거부했다.
        CONCEPT "먹었던 이익이 본전으로 돌아오게 두지 않는다"; 게이트는 코어와 같은 기하이고 거부 횟수를 같이 쓴다."""
        def run(fail_n, mid=2.3655):
            st = Strategy(dict(side="long", unit_qty=225, cap_usdt=54.6, core_units=1, pop_min_pct=0.4,
                               unit_min_pct=0.15, gate_floor_unit_pct=0.05, gate_relax=0.5, stop_structural_on=0))
            pos = dict(lots=[[225.0, 2.418, "a"], [225.0, 2.387, "b"]], avg=2.359704, last="trim", last_trim_px=2.336, last_buy_px=2.387)
            st.fail_n, st.last_qty, st.last_lot = fail_n, 450.0, "b"
            return st.step(F(mid=mid, bid=mid - 0.0005, ask=mid + 0.0005, atr=0.00409, atr15=0.0145), [dict(sig="POP_STALLING")], pos), st, pos
        r, st, _ = run(0); self.assertIsNone(r["trim"]); self.assertAlmostEqual(st.gate_eff, 0.15, 6)      # 거부 전에는 로트 기준 그대로
        r, st, pos = run(3)
        self.assertIsNotNone(r["trim"]); self.assertAlmostEqual(st.gate_eff, 0.05, 6)                      # 거부가 쌓이면 평단 기준(코어 기하)
        self.assertAlmostEqual(st.pull["ref"], pos["avg"], 9)                                              # 기준이 로트가 아니라 평단
        self.assertGreaterEqual((r["trim"][0] - pos["avg"]) * r["trim"][1], 0)                             # 실현 총손익은 음수가 될 수 없다
        self.assertIsNone(run(3, mid=2.3590)[0]["trim"])                                                   # 평단 아래면 여전히 안 판다

    def test_derisk_never_sells_an_added_unit_below_its_price(self):
        st = Strategy(dict(side="long", unit_qty=70, step_add_atr=0)); pos = dict(lots=[[70, 3.041, "a"]], avg=3.041, last="buy", last_buy_px=3.041)
        for t in (100, 101): st.step(F(t=t, mid=2.97, bid=2.969, ask=2.971), [], pos)
        self.assertTrue(st.derisk_armed)
        apply_fill(pos, 1, True, 70, 2.97, "b"); st.on_fill("buy", 70)           # low unit added; still deep under the average (3.0055)
        r = st.step(F(t=102, mid=2.972, bid=2.971, ask=2.973), [dict(sig="POP_STALLING")], pos)
        self.assertFalse(any(e[0] == "PULL_TRIM" for e in r["events"])); self.assertIsNone(r["trim"])          # +0.07% over its price < 0.15%: nothing is sold (no backstop by default)
        r = st.step(F(t=103, mid=2.976, bid=2.975, ask=2.977), [dict(sig="POP_STALLING")], pos)
        self.assertEqual(r["trim"][1], 70); self.assertEqual(r["events"][-1][1]["ref"], 2.97)              # +0.2% over its own price: the added unit goes

    def test_derisk_latch_clears_on_fill_and_rearms_only_later(self):
        st = Strategy(dict(side="long", unit_qty=70, step_add_atr=0)); pos = dict(lots=[[70, 3.041, "a"]], avg=3.041, last="buy", last_buy_px=3.041)
        st.step(F(mid=2.97, bid=2.969, ask=2.971), [], pos)                          # first tick sees the position appear (0 -> 70): latch stays clear
        st.step(F(t=100, mid=2.97, bid=2.969, ask=2.971), [], pos); self.assertTrue(st.derisk_armed)   # -2.3% under the average with no add: armed
        apply_fill(pos, 1, True, 70, 2.97, "b")
        st.step(F(t=101, mid=2.97, bid=2.969, ask=2.971), [], pos); self.assertFalse(st.derisk_armed)   # the fill clears it this tick
        st.step(F(t=102, mid=2.97, bid=2.969, ask=2.971), [], pos); self.assertTrue(st.derisk_armed)    # -2.3% under the new average: re-armed next tick

class DeriskUnderUnits(unittest.TestCase):
    def test_a_weak_bounce_cuts_the_core_lot_while_a_unit_sits_on_top(self):
        """CONCEPT: 손실 중 약반등에 물량을 좀 덜어(부분손절) — 저점 물량은 자기 가격 위에서만, 손실은 평단 기준의 물량에서 감수한다.
        The LIFO unit cannot sell under its price, so the cut comes out of the core lot (trim lot=0); before, a 2-lot underwater position
        had no partial stop at all (all 98 live cuts were 1-lot)."""
        st = Strategy(dict(side="long", unit_qty=70, step_add_atr=0, cap_usdt=60, derisk_pct=3.0, derisk_core_frac=0.5))
        pos = dict(lots=[[70, 3.0, "a"], [70, 2.95, "b"]], avg=2.975, last="buy", last_buy_px=2.95)
        for t in (100, 101): st.step(F(t=t, mid=2.90, bid=2.899, ask=2.901), [], pos)          # -2.5% under the average, no add possible: latched
        self.assertTrue(st.derisk_armed)
        r = st.step(F(t=102, mid=2.93, bid=2.929, ask=2.931), [dict(sig="POP_STALLING")], pos)   # weak bounce: the unit is still -0.7% under its price
        pt = [e for e in r["events"] if e[0] == "PULL_TRIM"][0][1]
        self.assertEqual((pt["qty"], pt["lot"], pt["mode"], pt["path"]), (35, "core", "derisk", "stall")); self.assertEqual(r["trim"][3], 0)
        self.assertAlmostEqual(pt["ref"], 2.975)                                                    # judged against the average, as a core cut is
        apply_fill(pos, 1, False, 35, 2.931, "t", lot=0); st.on_fill("trim", 35)
        self.assertEqual(pos["lots"], [[35, 3.0, "a"], [70, 2.95, "b"]])                            # the core lot shrank; the unit is untouched
        st.step(F(t=103, mid=2.93, bid=2.929, ask=2.931), [], pos)
        r = st.step(F(t=104, mid=2.955, bid=2.954, ask=2.956), [dict(sig="POP_STALLING")], pos)     # +0.17% over the unit: the unit cycles out normally
        self.assertEqual((r["trim"][1], r["trim"][3]), (70, None)); self.assertEqual(r["events"][-1][1]["mode"], "normal")
        st2 = Strategy(dict(side="long", unit_qty=70, step_add_atr=0, cap_usdt=60, derisk_under_units=0))   # the switch: only a lone core is cut
        pos2 = dict(lots=[[70, 3.0, "a"], [70, 2.95, "b"]], avg=2.975, last="buy", last_buy_px=2.95)
        for t in (100, 101): st2.step(F(t=t, mid=2.90, bid=2.899, ask=2.901), [], pos2)
        self.assertIsNone(st2.step(F(t=102, mid=2.93, bid=2.929, ask=2.931), [dict(sig="POP_STALLING")], pos2)["trim"])

    def test_a_unit_sale_at_its_normal_gate_is_not_labelled_derisk(self):
        st = Strategy(dict(side="long", unit_qty=70, step_add_atr=0, cap_usdt=60)); pos = dict(lots=[[70, 3.0, "a"], [70, 2.95, "b"]], avg=2.975, last="buy", last_buy_px=2.95)
        st.step(F(), [], pos); st.regime = "AGAINST"                                                 # a persistent de-risk trigger
        r = st.step(F(t=101, mid=2.96, bid=2.959, ask=2.961), [dict(sig="POP_STALLING")], pos)     # +0.34% over the unit's price >= 0.15%: a normal unit trim
        self.assertEqual(r["trim"][1], 70); self.assertEqual(r["events"][-1][1]["mode"], "normal")   # the label is the gate in force, not the flag (10 of 108 live 'derisk' labels were this)

class NoSacredCore(unittest.TestCase):
    def test_favor_does_not_hold_the_core(self):
        st = Strategy(dict(side="long", unit_qty=70)); pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        base = dict(rg_er=0.23, rg_med_up=3.7, rg_med_dn=1.22, rg_drift=9.66, rg_up=3, rg_dn=1)
        for k in range(1, 5): st.step(F(t=k * 60, rg_t=k, **base), [], pos)
        self.assertEqual(st.regime, "FAVOR")
        r = st.step(F(t=400, mid=3.026, bid=3.025, ask=3.027, rg_t=5, **base), [dict(sig="POP_STALLING")], pos)   # +0.87% >= pop_min x favor_pop_mult (0.8%)
        self.assertIsNotNone(r["trim"]); self.assertEqual(r["trim"][1], 70); self.assertEqual(r["events"][-1][1]["mode"], "favor")   # CONCEPT: no sacred core — the gate is scaled, nothing is exempt

class Premise(unittest.TestCase):
    def test_an_adopted_exchange_stop_is_not_a_premise_and_a_late_level_is_taken(self):
        st = Strategy(dict(side="long", unit_qty=70, cap_usdt=20)); pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        r = st.step(F(htf_lows=[2.95]), [], pos)                                                     # 2.95 is inside the ladder: no premise
        self.assertIsNone(st.struct_stop); self.assertEqual([e[0] for e in r["events"]], []); self.assertIsNotNone(st.stop_px)
        st.adopt_stop(2.8)                                                                           # the exchange stop (liq guard / resync) becomes the baseline ...
        self.assertEqual(st.stop_px, 2.8); self.assertIsNone(st.struct_stop)                        # ... and nothing else
        r = st.step(F(t=101, htf_lows=[2.95, 2.85]), [], pos)                                        # a qualifying pivot confirms later: it is the premise now
        self.assertAlmostEqual(st.struct_stop, 2.85 - 0.018, 3); self.assertEqual([e[0] for e in r["events"]], ["STRUCT_STOP"])
        self.assertEqual(r["stop"], 2.8)                                                             # the exchange stop stays the money cap baseline

    def test_structure_above_the_price_is_reported_once(self):
        st = Strategy(dict(side="long", unit_qty=70, cap_usdt=20)); pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        n = sum(1 for t in (100, 101, 102) for e in st.step(F(t=t, mid=2.80, bid=2.799, ask=2.801, htf_lows=[2.85]), [], pos)["events"] if e[0] == "STRUCT_SKIP")
        self.assertEqual(n, 1)

class RetraceShare(unittest.TestCase):
    def test_a_top_must_give_back_a_share_of_its_bounce(self):
        """0.5 x ATR1m is 0.02-0.06% on the 2026-09-02 basket: the retrace exit fired on any wiggle at the gate and was 63% of all pulls.
        A reversal gives back retrace_frac of the bounce (peak - trough since the last fill); the ATR term stays as the floor."""
        st = Strategy(dict(side="long", unit_qty=70, retrace_frac=0.33)); pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        st.step(F(t=100, mid=3.0), [], pos)
        st.step(F(t=101, mid=3.06, bid=3.059, ask=3.061), [], pos)                                   # +2% bounce from the trough 3.0
        r = st.step(F(t=102, mid=3.045, bid=3.044, ask=3.046), [], pos); self.assertIsNone(r["trim"])   # 0.75 ATR back but only a quarter of the bounce: not a reversal
        r = st.step(F(t=103, mid=3.038, bid=3.037, ask=3.039), [], pos)                              # 0.022 back = 37% of the bounce
        self.assertEqual(r["events"][-1][0], "PULL_TRIM"); self.assertEqual(r["events"][-1][1]["path"], "retrace")
        st2 = Strategy(dict(side="long", unit_qty=70, retrace_frac=0.0)); pos2 = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        st2.step(F(t=100, mid=3.0), [], pos2); st2.step(F(t=101, mid=3.06, bid=3.059, ask=3.061), [], pos2)
        self.assertIsNotNone(st2.step(F(t=102, mid=3.045, bid=3.044, ask=3.046), [], pos2)["trim"])   # 0 restores the ATR-only rule

class UnitFloor(unittest.TestCase):
    def test_the_relaxed_unit_gate_never_falls_under_the_round_trip_fee(self):
        st = Strategy(dict(side="long", unit_qty=70, step_add_atr=0, cap_usdt=60, gate_floor_unit_pct=0.05, fee_rt_pct=0.08))
        pos = dict(lots=[[70, 3.05, "a"], [70, 3.0, "b"]], avg=3.025, last="buy", last_buy_px=3.0)
        st.step(F(), [], pos); st.fail_n = 10
        st.step(F(t=101, mid=3.002, bid=3.001, ask=3.003), [dict(sig="POP_STALLING")], pos)
        self.assertAlmostEqual(st.gate_eff, 0.08, 3)                                                 # CONCEPT: the unit's round trip is a profit — a taker exit included
        self.assertEqual(book_params(dict(unit_qty=70), "long", 0.001, 1, 0.1, fee_rt=0.08)["fee_rt_pct"], 0.08)
        self.assertEqual(book_params(dict(unit_qty=70, fee_rt_pct=0), "long", 0.001, 1, 0.1, fee_rt=0.08)["fee_rt_pct"], 0)   # a file value (0 = off) outranks the contract

class GapReturns(unittest.TestCase):
    def test_a_feed_gap_return_enters_neither_sigma_nor_velocity(self):
        from common.signal import Features
        book = lambda m, ts: dict(arg=dict(channel="books15"), data=[dict(bids=[[m - 0.01 - i * 0.01, 5] for i in range(5)], asks=[[m + 0.01 + i * 0.01, 5] for i in range(5)], ts=str(ts))], ts=ts)
        feat = Features(dict(vol_hl=300)); feat.seed_candles([dict(ts=i * 60000, o=100, h=100.05, l=99.95, c=100, v=1000) for i in range(120)])
        t = 7200
        for i in range(400): feat.feed(book(100 + 0.01 * (i % 2), t * 1000 + 500)); t += 1             # a mature, tiny sigma (1-tick flicker)
        var0 = feat.var.v
        feat.feed(book(101, (t + 30) * 1000 + 500)); t += 31                                          # 30 s of silence, then +1%: the gap return
        out = []
        for _ in range(20): out += feat.feed(book(101, t * 1000 + 500)); t += 1
        self.assertLess(feat.var.v, var0 * 2)                                                         # sigma did not swallow a 1% "1-second" return (x20 without the skip)
        self.assertFalse(any(x["sig"] in ("DIP_SLOWING", "POP_STALLING") and x.get("src") == "v" for x in out))   # and no fake fast->slow transition fired
        feat.feed(book(101, (t + 200) * 1000 + 500))                                                  # > 120 s: an outage restarts the normaliser too (RULES)
        self.assertLessEqual(feat.var.n, 1)                                                          # the normaliser restarts: v is silent for vol_hl again

class LotRouting(unittest.TestCase):
    def test_apply_fill_reduces_the_named_lot_first_then_lifo(self):
        pos = dict(lots=[[70, 3.0, "a"], [70, 2.95, "b"]], avg=2.975, last="buy")
        apply_fill(pos, 1, False, 35, 2.93, "t1", lot=0); self.assertEqual(pos["lots"], [[35, 3.0, "a"], [70, 2.95, "b"]])
        apply_fill(pos, 1, False, 50, 2.93, "t2", lot=0); self.assertEqual(pos["lots"], [[55, 2.95, "b"]])   # the core lot gone, the rest LIFO
        self.assertAlmostEqual(pos["avg"], 2.975)                                                     # exchange accounting: the average never moves on a reduce

class DeriskQuantity(unittest.TestCase):
    def test_a_core_smaller_than_a_unit_is_still_cut_in_half_first(self):
        st = Strategy(dict(side="long", unit_qty=70, step_add_atr=0)); pos = dict(lots=[[60, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        for t in (100, 101): st.step(F(t=t, mid=2.98, bid=2.979, ask=2.981), [], pos)
        r = st.step(F(t=102, mid=2.985, bid=2.984, ask=2.986), [dict(sig="POP_STALLING")], pos); self.assertEqual(r["trim"][1], 30)   # half, not the whole 60 (2026-08-29 21:11 live)

    def test_derisk_mode_sells_the_normal_quantity_at_a_normal_stall(self):
        st = Strategy(dict(side="long", unit_qty=70, step_add_atr=0)); pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        st.step(F(), [], pos); st.regime = "AGAINST"                                                   # a persistent de-risk trigger
        r = st.step(F(t=101, mid=2.99, bid=2.989, ask=2.991), [dict(sig="POP_STALLING")], pos); self.assertEqual(r["trim"][1], 35)     # weak bounce: half the core
        st2 = Strategy(dict(side="long", unit_qty=70, step_add_atr=0)); pos2 = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        st2.step(F(), [], pos2); st2.regime = "AGAINST"
        r = st2.step(F(t=101, mid=3.015, bid=3.014, ask=3.016), [dict(sig="POP_STALLING")], pos2); self.assertEqual(r["trim"][1], 70)   # +0.5% >= pop_min: the whole lot, as outside de-risk

    def test_cut_takes_half_then_the_rest_never_dust(self):
        st = Strategy(dict(side="long", unit_qty=70, step_add_atr=0)); pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        for t in (100, 101): st.step(F(t=t, mid=2.98, bid=2.979, ask=2.981), [], pos)                   # -0.67% under the average with no add: latched
        r = st.step(F(t=102, mid=2.985, bid=2.984, ask=2.986), [dict(sig="POP_STALLING")], pos); self.assertEqual(r["trim"][1], 35)
        apply_fill(pos, 1, False, 35, 2.986, "t1"); st.on_fill("trim", 35)
        for t in (103, 104): st.step(F(t=t, mid=2.975, bid=2.974, ask=2.976), [], pos)
        r = st.step(F(t=105, mid=2.98, bid=2.979, ask=2.981), [dict(sig="POP_STALLING")], pos); self.assertEqual(r["trim"][1], 35)   # the remaining half-unit goes whole, not 17.5

class PullScope(unittest.TestCase):
    def test_pull_quantity_is_whole_exchange_steps_and_completes_within_half_a_step(self):
        st = Strategy(dict(side="long", unit_qty=76.1, step_add_atr=0, qstep=0.1)); pos = dict(lots=[[76.1, 2.713, "a"]], avg=2.713, last="buy", last_buy_px=2.713)
        for t in (100, 101): st.step(F(t=t, mid=2.695, bid=2.694, ask=2.696), [], pos)                  # -0.66% under the average: latched
        r = st.step(F(t=102, mid=2.7065, bid=2.706, ask=2.707), [dict(sig="POP_STALLING")], pos)
        self.assertEqual(r["trim"][1], 38.0)                                                              # half of 76.1 in whole steps (live 21:37: 38.05 left 0.05 nobody could sell)
        apply_fill(pos, 1, False, 38.0, 2.706, "t"); st.on_fill("trim", 38.0)
        r = st.step(F(t=103, mid=2.7065, bid=2.706, ask=2.707), [], pos)
        self.assertIsNone(st.pull); self.assertIsNone(r["trim"])                                          # the pull is complete, no 0.05 chase

    def test_a_pull_does_not_swallow_a_lot_bought_meanwhile(self):
        st = Strategy(dict(side="long", unit_qty=70, step_add_atr=0)); pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        for t in (100, 101): st.step(F(t=t, mid=2.98, bid=2.979, ask=2.981), [], pos)                     # latched: de-risk on
        r = st.step(F(t=102, mid=2.985, bid=2.984, ask=2.986), [dict(sig="POP_STALLING")], pos); self.assertEqual(r["trim"][1], 35)   # weak bounce: half the core, the pull rests
        apply_fill(pos, 1, True, 70, 2.975, "b"); st.on_fill("buy", 70)                                      # the deceleration's unit fills while the pull rests
        r = st.step(F(t=103, mid=2.976, bid=2.975, ask=2.977), [], pos)
        self.assertIsNone(r["trim"]); self.assertIsNone(st.pull)                                             # the pull is dropped, never extended over the new lot (fuzz 2026-08-29)
        r = st.step(F(t=104, mid=2.977, bid=2.976, ask=2.978), [dict(sig="POP_STALLING")], pos)
        self.assertIsNone(r["trim"])                                                                         # +0.07% over the new unit's price < 0.15%: nothing sells

class Relax(unittest.TestCase):
    def test_failed_stalls_lower_the_gate_toward_the_floor(self):
        st = Strategy(dict(side="long", unit_qty=70)); pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        st.step(F(t=100), [], pos)
        r = st.step(F(t=101, mid=3.006, bid=3.005, ask=3.007), [dict(sig="POP_STALLING")], pos)      # +0.2% < 0.4%: fails, gate -> 0.2%
        self.assertIsNone(r["trim"]); self.assertEqual([e for e in r["events"] if e[0] == "GATE_RELAX"][0][1]["fails"], 1)
        r = st.step(F(t=170, mid=3.0075, bid=3.0065, ask=3.0085), [dict(sig="POP_STALLING")], pos)   # +0.25% >= relaxed 0.2%: sells
        self.assertEqual(r["trim"][1], 70); self.assertIn("PULL_TRIM", [e[0] for e in r["events"]])

    def test_the_core_gate_never_relaxes_below_the_round_trip_fee(self):
        st = Strategy(dict(side="long", unit_qty=70, fee_rt_pct=0.08)); pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        st.step(F(t=100), [], pos)
        for i in range(8): st.step(F(t=101 + i * 70, mid=3.0005, bid=3.0004, ask=3.0006), [dict(sig="POP_STALLING")], pos)   # +0.02%: refused 8 times, the gate walks down ...
        self.assertAlmostEqual(st.gate_eff, 0.08 + (0.4 - 0.08) * 0.5 ** 7, places=6)                                            # ... toward 0.08%, never to breakeven (the 8th refusal counts after this tick's gate)
        r = st.step(F(t=700, mid=3.0027, bid=3.0026, ask=3.0028), [dict(sig="POP_STALLING")], pos)                              # +0.09% >= the floor: sells
        self.assertEqual(r["trim"][1], 70)

    def test_partial_trim_keeps_the_lot_refusals_a_new_lifo_lot_resets_them(self):
        st = Strategy(dict(side="long", unit_qty=70, step_add_atr=0, cap_usdt=60)); pos = dict(lots=[[70, 3.05, "a"], [70, 3.0, "b"]], avg=3.025, last="buy", last_buy_px=3.0)
        st.step(F(), [], pos)                                                                          # position seen
        st.step(F(t=101, mid=3.002, bid=3.001, ask=3.003), [dict(sig="POP_STALLING")], pos); self.assertEqual(st.fail_n, 1)   # +0.07% over the unit's price < 0.15%: refused once
        apply_fill(pos, 1, False, 10, 3.003, "t1"); st.on_fill("trim", 10)                            # 10 of the same unit sold
        st.step(F(t=102, mid=3.002, bid=3.001, ask=3.003), [], pos); self.assertEqual(st.fail_n, 1)   # its refusals stand
        apply_fill(pos, 1, False, 60, 3.003, "t2"); st.on_fill("trim", 60)                            # the unit is gone: the core is the LIFO lot now
        st.step(F(t=103, mid=3.002, bid=3.001, ask=3.003), [], pos); self.assertEqual(st.fail_n, 0)   # a fresh expectation

class Confirm(unittest.TestCase):
    def test_dual_add_needs_confirmation(self):
        st = Strategy(dict(side="long", unit_qty=70, add_confirm=1)); pos = dict(lots=[], last=None)
        r = st.step(F(t=100, mid=2.98, bid=2.979, ask=2.981, dip_low=2.978), [dict(sig="DIP_SLOWING", src="1m")], pos)
        self.assertEqual(r["events"][0][1]["why"], "unconfirmed")                                   # one rule, no decay, no retrace
        r = st.step(F(t=130, mid=2.98, bid=2.979, ask=2.981, dip_low=2.978), [dict(sig="DIP_SLOWING", src="v")], pos)
        self.assertEqual(r["events"][0][0], "ARM"); self.assertEqual(r["events"][0][1]["confirm"], "both")   # second rule within 90s
        st2 = Strategy(dict(side="long", unit_qty=70, add_confirm=1))
        r = st2.step(F(t=100, mid=2.992, bid=2.991, ask=2.993, dip_low=2.978), [dict(sig="DIP_SLOWING", src="1m")], dict(lots=[], last=None))
        self.assertEqual(r["events"][0][1]["confirm"], "retrace")                                    # 0.7 ATR back up from the trough

    def test_unit_mult_scales_the_unit(self):
        st = Strategy(dict(side="long", unit_qty=70)); r = st.step(F(mid=2.98, bid=2.979, ask=2.981), [dict(sig="DIP_SLOWING")], dict(lots=[], last=None, unit_mult=0.5))
        self.assertEqual(r["buy"], (2.979, 35.0))

class Breaks(unittest.TestCase):
    def test_break_never_blocks_the_deceleration(self):
        lg = Strategy(dict(side="long", unit_qty=70)); pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        r = lg.step(F(mid=3.03, bid=3.029, ask=3.031, bko=True), [dict(sig="POP_STALLING")], pos)
        self.assertIsNotNone(r["trim"]); self.assertEqual(r["events"][0][0], "PULL_TRIM")            # a long trims into a breakout stall
        r = lg.step(F(t=101, mid=2.9, bid=2.899, ask=2.901, brk=True), [dict(sig="BREAKDOWN"), dict(sig="DIP_SLOWING")], dict(lots=[], last=None))
        self.assertEqual(r["events"][0][0], "ARM")                                                    # and buys the deceleration inside the break (CONCEPT: never blocked)
        sh = Strategy(dict(side="short", unit_qty=70))
        r = sh.step(F(mid=3.03, bid=3.029, ask=3.031, bko=True), [dict(sig="BREAKOUT"), dict(sig="POP_STALLING")], dict(lots=[], last=None))
        self.assertEqual(r["events"][0][0], "ARM")                                                    # a short likewise inside a breakout

    def test_break_derisks_only_the_campaign_it_hit(self):
        st = Strategy(dict(side="long", unit_qty=70, step_add_atr=0)); pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        st.step(F(), [], pos)                                                                          # position seen, stop set
        st.step(F(t=101, mid=2.99, bid=2.989, ask=2.991, brk=True), [dict(sig="BREAKDOWN")], pos)     # the break fires against a held position
        r = st.step(F(t=102, mid=2.988, bid=2.987, ask=2.989, brk=True), [dict(sig="POP_STALLING")], pos)
        ev = [e for e in r["events"] if e[0] == "PULL_TRIM"]
        self.assertEqual((ev[0][1]["mode"], ev[0][1]["qty"]), ("derisk", 35))                           # weak bounce at -0.4%: half the core goes
        st2 = Strategy(dict(side="long", unit_qty=70, step_add_atr=0)); pos2 = dict(lots=[], avg=None, last=None, last_buy_px=None, last_trim_px=None)
        r = st2.step(F(mid=2.99, bid=2.989, ask=2.991, brk=True), [dict(sig="DIP_SLOWING")], pos2); self.assertEqual(r["events"][0][0], "ARM")
        apply_fill(pos2, 1, True, 70, 2.989, "b"); st2.on_fill("buy", 70)                           # bought at the deceleration inside the same break
        r = st2.step(F(t=101, mid=2.992, bid=2.991, ask=2.993, brk=True), [dict(sig="POP_STALLING")], pos2)
        self.assertFalse(any(e[0] == "PULL_TRIM" for e in r["events"])); self.assertIsNone(r["trim"])   # +0.1% stall < pop_min: not that break's victim, no de-risk sale
        st2.step(F(t=102, mid=2.99, bid=2.989, ask=2.991, brk=True), [dict(sig="BREAKDOWN")], pos2)   # a new break hits the position we now hold
        r = st2.step(F(t=103, mid=2.985, bid=2.984, ask=2.986, brk=True), [dict(sig="POP_STALLING")], pos2)
        self.assertTrue(any(e[0] == "PULL_TRIM" and e[1]["mode"] == "derisk" for e in r["events"]))

    def test_a_fill_or_a_recovery_clears_the_break_evidence(self):
        st = Strategy(dict(side="long", unit_qty=70, step_add_atr=0, cap_usdt=60)); pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        st.step(F(), [], pos)
        st.step(F(t=101, mid=2.99, bid=2.989, ask=2.991, brk=True), [dict(sig="BREAKDOWN")], pos); self.assertTrue(st.brk_seen)
        apply_fill(pos, 1, True, 70, 2.985, "b"); st.on_fill("buy", 70)                            # the deceleration came and we added: the campaign is cycling, not stuck
        st.step(F(t=102, mid=2.985, bid=2.984, ask=2.986, brk=True), [], pos); self.assertFalse(st.brk_seen)
        apply_fill(pos, 1, False, 70, 2.99, "t"); st.on_fill("trim", 70)                            # the unit cycles out; the core is alone again (avg 2.9925)
        st.step(F(t=103, mid=2.99, bid=2.989, ask=2.991, brk=True), [], pos)
        r = st.step(F(t=104, mid=2.99, bid=2.989, ask=2.991, brk=True), [dict(sig="POP_STALLING")], pos)
        self.assertFalse(any(e[0] == "PULL_TRIM" for e in r["events"]))                             # -0.08% weak bounce inside the same break window: no de-risk sale (2026-08-29 21:11 live)
        st2 = Strategy(dict(side="long", unit_qty=70, step_add_atr=0)); pos2 = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        st2.step(F(), [], pos2); st2.step(F(t=101, mid=2.99, bid=2.989, ask=2.991, brk=True), [dict(sig="BREAKDOWN")], pos2)
        st2.step(F(t=102, mid=3.013, bid=3.012, ask=3.014, brk=True), [], pos2); self.assertFalse(st2.brk_seen)   # +0.43% >= pop_min: recovered, the break is history

class RegimeAction(unittest.TestCase):
    def test_against_scales_the_unit_when_the_mult_is_set_and_vetoes_otherwise(self):
        st = Strategy(dict(side="long", unit_qty=70, against_regime_mult=0.5)); st.regime = "AGAINST"
        r = st.step(F(), [dict(sig="DIP_SLOWING")], dict(lots=[], last=None)); self.assertEqual(r["buy"][:2], (2.999, 35.0))   # half a unit
        st2 = Strategy(dict(side="long", unit_qty=70)); st2.regime = "AGAINST"
        r = st2.step(F(), [dict(sig="DIP_SLOWING")], dict(lots=[], last=None)); self.assertEqual(r["events"][0][1]["why"], "regime")   # the veto (default)

    def test_against_derisks_the_core_only_when_the_switch_is_on(self):
        for on, expect in ((True, 35), (False, None)):
            st = Strategy(dict(side="long", unit_qty=70, step_add_atr=0, derisk_on_against=on)); pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
            st.step(F(), [], pos); st.regime = "AGAINST"
            r = st.step(F(t=101, mid=2.991, bid=2.99, ask=2.992), [dict(sig="POP_STALLING")], pos)      # -0.3%: not latched (step 0.5%), only the label
            self.assertEqual(r["trim"][1] if r["trim"] else None, expect)

    def test_drift_floor_in_percent_keeps_a_small_grind_two_way(self):
        from common.signal import SIG
        rg = dict(rg_t=1, rg_er=0.2, rg_drift=-7.6, rg_up=0, rg_dn=0, rg_med_up=0.0, rg_med_dn=0.0)
        st = Strategy(dict(side="long"), {**SIG, "rg_confirm": 1}); st.step(F(atr=0.004, mid=3.0, **rg), [], dict(lots=[], last=None))
        self.assertEqual(st.regime, "AGAINST")                                                     # 7.6 ATR of 0.13% = a 1% grind: AGAINST today
        st2 = Strategy(dict(side="long"), {**SIG, "rg_confirm": 1, "rg_drift_min_pct": 2.0}); st2.step(F(atr=0.004, mid=3.0, **rg), [], dict(lots=[], last=None))
        self.assertNotEqual(st2.regime, "AGAINST")                                                 # 1% < 2%: not a one-way that matters (DEAD label: no swings at all)
        st3 = Strategy(dict(side="long"), {**SIG, "rg_confirm": 1, "rg_drift_min_pct": 2.0}); st3.step(F(atr=0.02, mid=3.0, **rg), [], dict(lots=[], last=None))
        self.assertEqual(st3.regime, "AGAINST")                                                    # 7.6 ATR of 0.67% = 5%: the real thing

class Regime(unittest.TestCase):
    def test_counter_swings_prevent_one_way_label(self):
        st = Strategy(dict(side="long")); pos = dict(lots=[], last=None)
        base = dict(rg_er=0.23, rg_med_up=3.7, rg_med_dn=1.22, rg_drift=9.66, rg_up=3, rg_dn=4)
        for k in range(1, 4): st.step(F(t=k * 60, rg_t=k, **base), [], pos)
        self.assertEqual(st.regime, "TWO_WAY")
        for k in range(4, 7): st.step(F(t=k * 60, rg_t=k, **{**base, "rg_dn": 1}), [], pos)
        self.assertEqual(st.regime, "FAVOR")

class FavorFullExit(unittest.TestCase):
    def test_full_exit_sells_the_core_even_in_favor(self):
        st = Strategy(dict(side="long", unit_qty=70)); pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        base = dict(rg_er=0.23, rg_med_up=3.7, rg_med_dn=1.22, rg_drift=9.66, rg_up=3, rg_dn=1)
        for k in range(1, 5): st.step(F(t=k * 60, rg_t=k, **base), [], pos)
        self.assertEqual(st.regime, "FAVOR")
        r = st.step(F(t=400, mid=3.1, bid=3.099, ask=3.101, rg_t=5, **base), [dict(sig="POP_STALLING")], pos)   # +3.33% >= full_exit 3%
        self.assertIsNotNone(r["trim"]); self.assertEqual(r["trim"][1], 70)
        r = st.step(F(t=401, mid=3.02, bid=3.019, ask=3.021, rg_t=5, **base), [dict(sig="POP_STALLING")], dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0))
        self.assertIsNone(Strategy(dict(side="long", unit_qty=70)).pull)   # (below full_exit the FAVOR core hold still applies — covered by the derisk/favor tests)

class UnitStep(unittest.TestCase):
    def test_unit_is_a_whole_number_of_quantity_steps(self):
        st = Strategy(dict(side="long", unit_qty=63.7, qstep=0.1))
        r = st.step(F(mid=2.98, bid=2.979, ask=2.981), [dict(sig="DIP_SLOWING")], dict(lots=[], last=None, unit_mult=0.5))
        q = r["buy"][1]; self.assertAlmostEqual(q * 10, round(q * 10), 9); self.assertEqual(q, st.arm[2]); self.assertIn(q, (31.8, 31.9))

class SecondClose(unittest.TestCase):
    def test_previous_second_closes_on_its_own_quotes(self):
        from common.signal import Features
        feat = Features(); feat.seed_candles([dict(ts=i * 60000, o=100, h=101, l=99, c=100, v=1000) for i in range(120)])
        book = lambda m, ts: dict(arg=dict(channel="books15"), data=[dict(bids=[[m - 0.5 - i, 5] for i in range(5)], asks=[[m + 0.5 + i, 5] for i in range(5)], ts=str(ts))], ts=ts)
        feat.feed(book(100, 7200500)); feat.feed(book(100, 7201500))     # t=7200 closes at 7201's first message on 7200's own book
        feat.feed(book(110, 7202100))                                     # the first message of 7202 carries a new mid ...
        self.assertEqual(feat.f["t"], 7201); self.assertEqual(feat.f["mid"], 100.0)   # ... which 7201 must not see

class WarmUp(unittest.TestCase):
    def test_velocity_rule_waits_for_a_mature_normaliser(self):
        from common.signal import Features, EMA
        e = EMA(300); e.add(1.0); e.add(3.0); self.assertAlmostEqual(e.v, 2.0)                        # an expanding mean, not 1 + k x 2
        feat = Features(dict(vol_hl=300)); feat.seed_candles([dict(ts=i * 60000, o=100, h=100.05, l=99.95, c=100, v=1000) for i in range(120)])
        book = lambda m, ts: dict(arg=dict(channel="books15"), data=[dict(bids=[[m - 0.01 - i * 0.01, 5] for i in range(5)], asks=[[m + 0.01 + i * 0.01, 5] for i in range(5)], ts=str(ts))], ts=ts)
        out = []; t = 7200
        out += feat.feed(book(100, t * 1000 + 500)); t += 1
        out += feat.feed(book(99.5, t * 1000 + 500)); t += 1                                            # -0.5% first return: v = -1 exactly under the old code
        for _ in range(40): out += feat.feed(book(99.5, t * 1000 + 500)); t += 1                         # then flat: the old code fired DIP_SLOWING here
        self.assertFalse(any(x["sig"] == "DIP_SLOWING" and x.get("src") == "v" for x in out))
        for _ in range(320): out += feat.feed(book(99.5, t * 1000 + 500)); t += 1                        # a half-life of data: the normaliser is mature
        out = []
        out += feat.feed(book(99.0, t * 1000 + 500)); t += 1
        for _ in range(40): out += feat.feed(book(99.0, t * 1000 + 500)); t += 1
        self.assertTrue(any(x["sig"] == "DIP_SLOWING" and x["src"] == "v" for x in out))               # the same shape of move now fires

class ThirdDetector(unittest.TestCase):
    def test_s8_fires_when_the_push_dies_after_rebuilding_and_respects_depth_and_cooldown(self):
        from common.signal import s8_state, s8_step, SIG
        st = s8_state(); p = dict(SIG, s8_decel=0.3, s8_rebuild=0.5, s8_cool=60)
        xs = [0.5, 1.0, 2.0, 2.0, 1.5, 0.5, 0.4, 0.3, 1.2, 0.3]
        self.assertEqual([sec for sec, x in enumerate(xs) if s8_step(st, x, sec, True, p)], [5])   # peak 2.0 at sec 2, died to 0.5 <= 0.6 at sec 5
        # sec 8 rebuilt the push to 1.2 (>= half of the leg's 2.0) and sec 9 died again, but the 60 s cooldown holds
        self.assertFalse(s8_step(st, 0.3, 64, True, p)); self.assertTrue(s8_step(st, 0.3, 65, True, p))
        st = s8_state(); self.assertEqual([sec for sec, x in enumerate(xs) if s8_step(st, x, sec, False, p)], [])   # the depth condition gates it
        st = s8_state(); p3 = dict(p, s8_hold=3)
        self.assertEqual([sec for sec, x in enumerate(xs) if s8_step(st, x, sec, True, p3)], [7])                     # dead at 5, 6, 7: the third dead second fires
        st = s8_state(); conf = {5: False, 6: False, 7: True}
        self.assertEqual([sec for sec, x in enumerate(xs) if s8_step(st, x, sec, True, p, conf.get(sec, True))], [7])   # no confirmation at 5-6: it waits, does not forget

    def _slide(self, feat):
        book = lambda m, ts: dict(arg=dict(channel="books15"), data=[dict(bids=[[round(m - 0.01 - i * 0.01, 4), 5] for i in range(5)], asks=[[round(m + 0.01 + i * 0.01, 4), 5] for i in range(5)], ts=str(ts))], ts=ts)
        out = []; t = 7200
        for _ in range(40): out += feat.feed(book(100.0, t * 1000 + 500)); t += 1
        for i in range(60): out += feat.feed(book(round(100.0 - 0.01 * i + (0.02 if i % 2 else 0.0), 4), t * 1000 + 500)); t += 1   # a 0.6% slide with tick noise: v ~ -0.45
        last = round(100.0 - 0.01 * 59 + 0.02, 4)
        for _ in range(20): out += feat.feed(book(last, t * 1000 + 500)); t += 1                        # the slide stalls
        return out

    def test_s8_is_recorded_as_shadow_by_default_and_trades_when_switched_on(self):
        from common.signal import Features
        feat = Features(dict(vol_hl=300)); feat.seed_candles([dict(ts=i * 60000, o=100, h=100.05, l=99.95, c=100, v=1000) for i in range(120)])
        out = self._slide(feat); s8 = [x for x in out if x["sig"] == "DIP_SLOWING" and x.get("src") == "s8"]
        self.assertTrue(s8); self.assertTrue(all(x["shadow"] for x in s8))                              # fires at the stall, recorded only
        self.assertFalse(any(x["sig"] == "DIP_SLOWING" and x.get("src") != "s8" for x in out))          # v (immature normaliser) and 1m (no candles) are silent
        self.assertIsNone(feat.dip["last"])                                                              # the shared cooldown is untouched: live stays bit-identical
        st = Strategy(dict(side="long", unit_qty=70, max_notional=1e6)); pos = dict(lots=[], avg=None)
        self.assertIsNone(st.step(F(mid=99.4, bid=99.39, ask=99.41), [s8[0]], pos)["buy"])              # the Strategy never arms on a shadow signal
        feat = Features(dict(vol_hl=300, s8_on=1)); feat.seed_candles([dict(ts=i * 60000, o=100, h=100.05, l=99.95, c=100, v=1000) for i in range(120)])
        out = self._slide(feat); s8 = [x for x in out if x["sig"] == "DIP_SLOWING" and x.get("src") == "s8"]
        self.assertTrue(s8); self.assertFalse(any(x["shadow"] for x in s8)); self.assertIsNotNone(feat.dip["last"])
        feat = Features(dict(vol_hl=300, s8_on=1, s8_dip=0)); feat.seed_candles([dict(ts=i * 60000, o=100, h=100.05, l=99.95, c=100, v=1000) for i in range(120)])
        self.assertFalse([x for x in self._slide(feat) if x.get("src") == "s8"])               # the dip side switched off: nothing from s8 on a slide
        feat = Features(dict(vol_hl=300, s8_on=1, s8_gap_s=600)); feat.seed_candles([dict(ts=i * 60000, o=100, h=100.05, l=99.95, c=100, v=1000) for i in range(120)])
        feat.dip["last_base"] = 7200 + 100                                                      # a base rule fired a moment ago: the gap-filler stays silent
        self.assertFalse([x for x in self._slide(feat) if x.get("src") == "s8"])
        self.assertEqual(Strategy(dict(side="long", unit_qty=70, max_notional=1e6)).step(F(mid=99.4, bid=99.39, ask=99.41), [s8[0]], dict(lots=[], avg=None))["buy"], (99.39, 70))

class CurrentLeg(unittest.TestCase):
    def _feat(self, **sig):
        from common.signal import Features
        feat = Features(dict(vol_hl=300, rg_theta=0.7, rg_leg_pct=2.0, **sig))
        feat.seed_candles([dict(ts=i * 60000, o=100, h=100.05, l=99.95, c=100, v=1000) for i in range(200)])
        return feat

    def _candles(self, feat, closes, t0=200):
        out = []
        for i, c in enumerate(closes):
            ts = (t0 + i) * 60000
            out += feat.feed(dict(arg=dict(channel="candle1m"), data=[[str(ts), str(c), str(c + 0.02), str(c - 0.02), str(c), "1000", "0", "0"]], ts=ts))
            out += feat.feed(dict(arg=dict(channel="candle1m"), data=[[str(ts + 60000), str(c), str(c), str(c), str(c), "0", "0", "0"]], ts=ts + 60000))   # the next bar opens: this one closes
        return out

    def test_the_leg_is_one_way_from_two_percent_deep_until_the_first_theta_bounce(self):
        feat = self._feat()
        slide = [100 - 0.1 * i for i in range(1, 26)]                    # -2.5% over 25 minutes, no 0.7% bounce inside
        self._candles(feat, slide[:19]); self.assertEqual(feat.leg.get("leg_ow"), 0)       # -1.9%: not yet
        self._candles(feat, slide[19:], t0=219); self.assertEqual(feat.leg["leg_ow"], -1); self.assertEqual(feat.leg["leg_dir"], -1); self.assertGreaterEqual(feat.leg["leg_pct"], 2.0)
        self._candles(feat, [97.5 + 0.2 * i for i in range(1, 6)], t0=225)                 # +1.0% bounce: a theta swing confirms the low, the leg is a new up-leg
        self.assertEqual(feat.leg["leg_dir"], 1); self.assertEqual(feat.leg["leg_ow"], 0)

    def test_the_strategy_reads_the_leg_only_when_switched_on(self):
        from common.signal import Strategy, SIG
        f = dict(F(), rg_t=1, rg_up=0, rg_dn=3, rg_med_up=0.0, rg_med_dn=0.9, rg_drift=-1.0, rg_er=0.5, mid=100.0, atr=0.1, leg_ow=-1, leg_dir=-1, leg_pct=2.4, leg_min=20)
        st = Strategy(dict(side="long", unit_qty=70, max_notional=1e6), dict(SIG, rg_confirm=1, rg_leg_on=1)); ev = []
        st._regime(f, 1, ev); self.assertEqual(st.regime, "AGAINST")                       # the leg runs against the long book: one-way now
        st._regime(dict(f, rg_t=2, leg_ow=0), 1, ev); self.assertEqual(st.regime, "TWO_WAY")   # the bounce ended the leg: released at once, no window to drain
        st2 = Strategy(dict(side="long", unit_qty=70, max_notional=1e6), dict(SIG, rg_confirm=1, rg_leg_on=0)); ev = []
        st2._regime(f, 1, ev); self.assertNotEqual(st2.regime, "AGAINST")                  # off: the window label (drift -1 ATR) says nothing

class StepCap(unittest.TestCase):
    def test_the_ladder_step_is_capped_only_when_the_cap_is_set(self):
        from common.signal import add_step
        p = dict(step_add_pct=0.5, step_add_atr=0.4, step_add_max_pct=0.0); f = dict(atr15=0.093)   # ATR15 3.1% of a 3.0 price: the post-crash inflation
        self.assertAlmostEqual(add_step(p, f, 3.0), 1.24, 2)                                            # no cap: the step follows ATR15
        self.assertAlmostEqual(add_step(dict(p, step_add_max_pct=1.0), f, 3.0), 1.0)                    # capped
        self.assertAlmostEqual(add_step(dict(p, step_add_max_pct=1.0), dict(atr15=0.02), 3.0), 0.5)     # a quiet tape: the floor, untouched by the cap
        st = Strategy(dict(side="long", unit_qty=70, step_add_atr=0.4, step_add_max_pct=1.0)); pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        st.step(F(t=100, atr15=0.093), [], pos)
        r = st.step(F(t=101, mid=2.967, bid=2.966, ask=2.968, atr15=0.093), [dict(sig="DIP_SLOWING")], pos)   # -1.1% under the last buy: past the 1.0% cap, inside the uncapped 1.24%
        self.assertEqual(r["buy"], (2.966, 70))

class Zigzag(unittest.TestCase):
    def test_straight_move_has_no_swings(self):
        self.assertEqual(zigzag([1, 1.01, 1.02, 1.03, 1.05], 0.007), [])
        self.assertEqual(len(zigzag([1, 1.02, 1.0, 1.02, 1.0], 0.007)), 3)

class ExitMode(unittest.TestCase):
    """exit (2026-09-03): the premise broke (hunt: the phase turned) — the whole position sells into the next stall whatever the cost,
    and a floor under the stall: taker after exit_after_s, or exit_atr x ATR further against us."""
    def _st(self, **kw):
        st = Strategy(dict(side="long", unit_qty=70, cap_usdt=20, stop_structural_on=0, exit=1, exit_after_s=600, exit_atr=3.0, **kw))
        pos = dict(lots=[[70, 3.0, "a"], [70, 2.95, "b"]], avg=2.975, last="buy", last_buy_px=2.95, pause=True)
        return st, pos

    def test_a_stall_below_cost_sells_everything_as_a_maker_then_taker(self):
        st, pos = self._st()
        r = st.step(F(t=100, mid=2.9, bid=2.899, ask=2.901), [], pos)
        self.assertEqual(r["events"][0][0], "EXIT_ARMED"); self.assertIsNone(r["trim"])                      # armed, nothing to sell into yet
        r = st.step(F(t=105, mid=2.9, bid=2.899, ask=2.901), [dict(sig="POP_STALLING")], pos)
        ev = [e for e in r["events"] if e[0] == "PULL_TRIM" and e[1]["mode"] == "exit"][0][1]              # (a default-param derisk pull fires first and is replaced)
        self.assertEqual((ev["all"], ev["qty"]), (True, 140))                                                # the whole position, 2.5% under the average
        self.assertEqual(r["trim"], (2.901, 140, "maker", None)); self.assertIsNone(r["buy"])
        r = st.step(F(t=116, mid=2.9, bid=2.899, ask=2.901), [], pos); self.assertEqual(r["trim"][2], "taker")   # trim_taker_after_s: it does not rest for long
        r = st.step(F(t=117, mid=2.88, bid=2.879, ask=2.881), [], pos); self.assertEqual(r["trim"][1], 140)     # a further drop does not drop the pull (gate -1e9)

    def test_no_stall_in_time_or_an_adverse_move_takes_it_at_market(self):
        st, pos = self._st()
        st.step(F(t=100, mid=2.9, bid=2.899, ask=2.901), [], pos)
        r = st.step(F(t=699, mid=2.9, bid=2.899, ask=2.901), [], pos); self.assertIsNone(r["trim"])
        r = st.step(F(t=700, mid=2.9, bid=2.899, ask=2.901), [], pos)
        self.assertEqual(r["trim"], (2.899, 140, "taker", None)); self.assertEqual([e[1]["path"] for e in r["events"] if e[0] == "PULL_TRIM"], ["timeout"])
        st, pos = self._st()
        st.step(F(t=100, mid=2.9, bid=2.899, ask=2.901), [], pos)
        r = st.step(F(t=130, mid=2.84, bid=2.839, ask=2.841), [], pos)                                          # 3 x ATR(0.02) = 0.06 under the flag
        self.assertEqual(r["trim"][2], "taker"); self.assertEqual([e[1]["path"] for e in r["events"] if e[0] == "PULL_TRIM"], ["adverse"])

    def test_the_blow_off_target_rests_for_part_of_the_position_and_yields_to_a_stall_pull(self):
        """blowoff_atr (2026-09-03, hunt long books): half the position rests as a maker at avg + 8 x ATR15; a stall pull takes the slot."""
        st = Strategy(dict(side="long", unit_qty=70, cap_usdt=20, stop_structural_on=0, blowoff_atr=8.0, blowoff_frac=0.5))
        pos = dict(lots=[[70, 3.0, "a"], [70, 2.9, "b"]], avg=2.95, last="buy", last_buy_px=2.9)
        r = st.step(F(t=100, mid=2.92, bid=2.919, ask=2.921, atr15=0.06), [], pos)
        self.assertEqual(r["trim"], (round(2.95 + 8 * 0.06, 10), 70, "maker", None, "blowoff"))       # 3.43, half of 140, resting above the market, tagged for the ledger
        r = st.step(F(t=200, mid=3.02, bid=3.019, ask=3.021, atr15=0.06), [dict(sig="POP_STALLING")], pos)   # a stall above the LIFO lot's cost: the pull wins the slot
        self.assertEqual(r["trim"][2], "maker"); self.assertLess(r["trim"][0], 3.1)
        st0 = Strategy(dict(side="long", unit_qty=70, cap_usdt=20, stop_structural_on=0)); pos0 = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        self.assertIsNone(st0.step(F(t=100, mid=2.92, bid=2.919, ask=2.921, atr15=0.06), [], pos0)["trim"])   # off by default: the basket never rests a target

    def test_the_blow_off_target_sells_its_share_of_the_campaign_once(self):
        """The target is blowoff_frac of the campaign's LARGEST position, not of whatever is left: recomputing it from the remaining qty
        re-armed half of the rest at the same price after every fill (140 -> 70 -> 35 -> ... down to the exchange step)."""
        st = Strategy(dict(side="long", unit_qty=70, cap_usdt=20, stop_structural_on=0, blowoff_atr=8.0, blowoff_frac=0.5))
        pos = dict(lots=[[70, 3.0, "a"], [70, 2.9, "b"]], avg=2.95, last="buy", last_buy_px=2.9)
        r = st.step(F(t=100, mid=2.92, bid=2.919, ask=2.921, atr15=0.06), [], pos); self.assertEqual(r["trim"][1], 70)
        pos["lots"] = [[70, 3.0, "a"]]                                                      # the target filled: 70 of the 140 are gone
        r = st.step(F(t=110, mid=2.92, bid=2.919, ask=2.921, atr15=0.06), [], pos); self.assertIsNone(r["trim"])   # its share is sold; nothing rests again
        pos["lots"] = [[70, 3.0, "a"], [70, 2.9, "b"], [70, 2.85, "c"]]                     # adds rebuild it past the old high-water mark: the target covers the new units
        r = st.step(F(t=120, mid=2.92, bid=2.919, ask=2.921, atr15=0.06), [], pos); self.assertEqual(r["trim"][1], 105.0)   # half of 210, not half of what a fill left
        pos["lots"] = []; st.step(F(t=130, mid=2.92, bid=2.919, ask=2.921, atr15=0.06), [], pos); self.assertIsNone(st.blow_base)   # flat forgets the campaign

    def test_an_exit_pull_is_dropped_when_the_flag_clears(self):
        """hunt clears `exit` when a one-bar misread reverts before the book is flat (HUNT_RESUME). The standing "sell everything at any
        price" pull has gate -1e9, so nothing else would ever drop it and the book would keep dumping."""
        st, pos = self._st()
        st.step(F(t=100, mid=2.9, bid=2.899, ask=2.901), [], pos)
        r = st.step(F(t=105, mid=2.9, bid=2.899, ask=2.901), [dict(sig="POP_STALLING")], pos)
        self.assertTrue(st.pull.get("exit")); self.assertIsNotNone(r["trim"])
        st.p["exit"] = 0
        r = st.step(F(t=106, mid=2.9, bid=2.899, ask=2.901), [], pos)
        self.assertIsNone(st.pull); self.assertIsNone(r["trim"]); self.assertIsNone(st.exit_t)
        self.assertEqual([e[1]["why"] for e in r["events"] if e[0] == "PULL_DROP"], ["exit_off"])

    def test_without_the_flag_nothing_changes_and_a_flat_book_forgets_the_flag(self):
        st = Strategy(dict(side="long", unit_qty=70, cap_usdt=20, stop_structural_on=0)); pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        r = st.step(F(t=100, mid=2.9, bid=2.899, ask=2.901), [dict(sig="POP_STALLING")], pos)
        self.assertIsNone(r["trim"]); self.assertNotIn("EXIT_ARMED", [e[0] for e in r["events"]])            # below the gate: the normal book waits
        st, pos = self._st(); st.step(F(t=100, mid=2.9, bid=2.899, ask=2.901), [], pos); self.assertEqual(st.exit_t, 100)
        pos["lots"] = []; st.step(F(t=101, mid=2.9, bid=2.899, ask=2.901), [], pos); self.assertIsNone(st.exit_t)

    def test_a_retrace_top_after_the_flag_sells_everything_whatever_the_cost(self):
        """RULES: "다음 정체 또는 되돌림 고점에서 원가 무관하게 전량". The cost-gated retrace_top never exists under water, so before this
        (audit 2026-09-04) only a stall or the floor could end the book. The bounce is counted from the flag on: no bounce, no top."""
        st, pos = self._st()
        st.step(F(t=100, mid=2.9, bid=2.899, ask=2.901), [], pos)                                      # armed 2.5% under the average
        r = st.step(F(t=110, mid=2.88, bid=2.879, ask=2.881), [], pos); self.assertIsNone(r["trim"])    # a new low after the flag: the trough, no bounce yet
        r = st.step(F(t=120, mid=2.93, bid=2.929, ask=2.931), [], pos); self.assertIsNone(r["trim"])    # the bounce (+1.7%, still under every cost): its peak
        r = st.step(F(t=130, mid=2.91, bid=2.909, ask=2.911), [], pos)                                  # back 0.02 = 1 x ATR and 40% of the bounce: the top is in
        ev = [e for e in r["events"] if e[0] == "PULL_TRIM" and e[1]["mode"] == "exit"]
        self.assertEqual([e[1]["path"] for e in ev], ["retrace"]); self.assertEqual(r["trim"], (2.911, 140, "maker", None))
        st, pos = self._st()                                                                             # a giveback under the rule's size is a wiggle, not a top
        st.step(F(t=100, mid=2.9, bid=2.899, ask=2.901), [], pos); st.step(F(t=120, mid=2.93, bid=2.929, ask=2.931), [], pos)
        r = st.step(F(t=130, mid=2.925, bid=2.924, ask=2.926), [], pos); self.assertIsNone(r["trim"])

class StopLock(unittest.TestCase):
    """stop_lock_atr / stop_trail_atr (2026-09-03, hunt profile): a gain beyond noise never becomes a loss — the exchange stop rises to
    breakeven once the best mid is lock x ATR15 past the average, trails the best mid at trail x ATR15 when tighter, and never loosens."""
    def _st(self, side="long", **kw):
        st = Strategy({**dict(side=side, unit_qty=70, cap_usdt=20, stop_structural_on=0, stop_lock_atr=2.0, stop_trail_atr=1.5, fee_rt_pct=0.1), **kw})
        return st, dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)

    def test_long_locks_at_two_atr_then_trails_and_never_loosens(self):
        st, pos = self._st()
        r = st.step(F(t=100, mid=3.05, bid=3.049, ask=3.051), [], pos)
        self.assertAlmostEqual(r["stop"], 3.0 - 20 / 70, 3)                                        # +0.8 ATR15: still the money cap (2.714)
        r = st.step(F(t=110, mid=3.13, bid=3.129, ask=3.131), [], pos)                               # +2.17 ATR15 (0.06): lock; trail 3.13 - 0.09 beats breakeven 3.003
        self.assertAlmostEqual(r["stop"], 3.04, 3); self.assertEqual([e[0] for e in r["events"] if e[0] == "STOP_LOCK"], ["STOP_LOCK"])
        r = st.step(F(t=120, mid=3.30, bid=3.299, ask=3.301), [], pos); self.assertAlmostEqual(r["stop"], 3.21, 3)   # the trail follows the best mid
        r = st.step(F(t=130, mid=3.10, bid=3.099, ask=3.101), [], pos)
        self.assertAlmostEqual(r["stop"], 3.21, 3); self.assertNotIn("STOP_LOCK", [e[0] for e in r["events"]])       # a pullback never loosens it (ratchet), no event
        pos["lots"] = []; st.step(F(t=140, mid=3.10, bid=3.099, ask=3.101), [], pos); self.assertIsNone(st.best)      # flat forgets the campaign's best

    def test_breakeven_alone_when_the_trail_is_off_and_a_short_mirrors(self):
        st, pos = self._st(stop_trail_atr=0.0)
        r = st.step(F(t=110, mid=3.13, bid=3.129, ask=3.131), [], pos); self.assertAlmostEqual(r["stop"], 3.003, 3)   # avg x (1 + 0.1% round trip)
        st, pos = self._st(side="short")
        r = st.step(F(t=110, mid=2.87, bid=2.869, ask=2.871), [], pos); self.assertAlmostEqual(r["stop"], 2.96, 3)    # 2.87 + 1.5 x 0.06, tighter than the cap 3.286

    def test_off_by_default_the_basket_keeps_the_money_cap(self):
        st = Strategy(dict(side="long", unit_qty=70, cap_usdt=20, stop_structural_on=0)); pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        r = st.step(F(t=110, mid=3.30, bid=3.299, ask=3.301), [], pos); self.assertAlmostEqual(r["stop"], 3.0 - 20 / 70, 3); self.assertIsNone(st.best)

class MarketTrimGate(unittest.TestCase):
    """trim_market_* (2026-09-03, measured and left OFF): a stall may sell a LIFO unit under its cost once the bounce since the last fill,
    from its trough, is trim_market_atr x ATR15 (or trim_market_frac of the excursion under the lot); _only drops the cost gate; the default
    keeps the cost gate. Two-lot book: core 70 @ 3.1, unit 70 @ 3.0 (F: atr15 0.06)."""
    def _st(self, **kw):
        st = Strategy(dict(side="long", unit_qty=70, stop_structural_on=0, derisk_pct=0, **kw)); pos = dict(lots=[[70, 3.1, "a"], [70, 3.0, "b"]], avg=3.05, last="buy", last_buy_px=3.0)   # derisk off as live
        st.step(F(t=100, mid=3.0), [], pos); st.step(F(t=101, mid=2.90, bid=2.899, ask=2.901), [], pos)   # the unit goes 3.3% under water: trough 2.90
        return st, pos
    def test_default_keeps_the_cost_gate_under_water(self):
        st, pos = self._st(); r = st.step(F(t=102, mid=2.97, bid=2.969, ask=2.971), [dict(sig="POP_STALLING")], pos)   # a 0.07 (1.2 ATR15) bounce stalls 1% under the lot
        self.assertFalse(any(e[0] == "PULL_TRIM" for e in r["events"])); self.assertTrue(any(e[0] == "GATE_RELAX" for e in r["events"]))
    def test_market_gate_sells_the_unit_under_its_cost_after_a_market_sized_bounce(self):
        st, pos = self._st(trim_market_atr=1.0); r = st.step(F(t=102, mid=2.97, bid=2.969, ask=2.971), [dict(sig="POP_STALLING")], pos)
        pt = [e for e in r["events"] if e[0] == "PULL_TRIM"]; self.assertEqual(len(pt), 1); self.assertEqual(pt[0][1]["mode"], "market"); self.assertLess(pt[0][1]["dev_lot"], 0)
        self.assertEqual(r["trim"][1], 70); self.assertEqual(st.pull["gate"], -1e9)                       # one LIFO unit; never dropped on a wiggle (taker after the clock)
        st, pos = self._st(trim_market_atr=2.0); r = st.step(F(t=102, mid=2.97, bid=2.969, ask=2.971), [dict(sig="POP_STALLING")], pos)
        self.assertFalse(any(e[0] == "PULL_TRIM" for e in r["events"]))                                  # 1.2 ATR15 < 2: not market-sized
        st, pos = self._st(trim_market_frac=0.5); r = st.step(F(t=102, mid=2.97, bid=2.969, ask=2.971), [dict(sig="POP_STALLING")], pos)
        self.assertEqual([e[1]["mode"] for e in r["events"] if e[0] == "PULL_TRIM"], ["market"])         # 0.07 of the 0.10 excursion under the lot >= 0.5
        st, pos = self._st(trim_market_atr=1.0, trim_market_against=1); r = st.step(F(t=102, mid=2.97, bid=2.969, ask=2.971), [dict(sig="POP_STALLING")], pos)
        self.assertFalse(any(e[0] == "PULL_TRIM" for e in r["events"]))                                  # gated on the AGAINST label, which this tape never set
    def test_market_only_ignores_cost_both_ways(self):
        st = Strategy(dict(side="long", unit_qty=70, stop_structural_on=0, derisk_pct=0, trim_market_atr=2.0, trim_market_only=1)); pos = dict(lots=[[70, 3.1, "a"], [70, 3.0, "b"]], avg=3.05, last="buy", last_buy_px=3.0)
        st.step(F(t=100, mid=3.0), [], pos); r = st.step(F(t=101, mid=3.01, bid=3.009, ask=3.011), [dict(sig="POP_STALLING")], pos)
        self.assertFalse(any(e[0] == "PULL_TRIM" for e in r["events"])); self.assertFalse(any(e[0] == "GATE_RELAX" for e in r["events"]))   # +0.33% over cost would sell under the cost gate; 0.17 ATR15 bounce is not market-sized

if __name__ == "__main__":
    unittest.main()


class FlowExit(unittest.TestCase):
    """flow_exit_n (2026-09-04, NEXT 21): while positioned, the last 10 s of aggressor flow is read once a minute; n minutes running
    against the position put the campaign in exit mode (as `exit` does: everything into the next stall whatever the cost, taker after
    exit_after_s), latched until flat. exit_random = the same exit at a random minute (a measurement baseline). Off by default."""
    def _pos(self): return dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0, pause=False)

    def test_three_adverse_minutes_running_put_the_campaign_in_exit_mode_and_the_next_stall_sells_everything(self):
        st = Strategy(dict(side="short", unit_qty=70, cap_usdt=20, stop_structural_on=0, flow_exit_n=3)); pos = self._pos()
        for t, bs in ((60, 0.7), (120, 0.7), (130, 0.9), (180, 0.3)):      # buyers hitting for two minutes; a mid-minute read is not a minute; minute 3 with us restarts the run
            st.step(F(t=t, bs10=bs), [], pos); self.assertFalse(st.flow_exit)
        self.assertEqual(st.flow_n, 0)
        for t in (240, 300): st.step(F(t=t, bs10=0.8), [], pos)
        self.assertEqual((st.flow_n, st.flow_exit), (2, False))
        r = st.step(F(t=360, bs10=0.6), [], pos)
        self.assertTrue(st.flow_exit); self.assertEqual([e[0] for e in r["events"] if e[0] in ("FLOW_EXIT", "EXIT_ARMED")], ["FLOW_EXIT", "EXIT_ARMED"])
        self.assertEqual([e[1]["n"] for e in r["events"] if e[0] == "FLOW_EXIT"], [3])
        r = st.step(F(t=365, mid=3.03, bid=3.029, ask=3.031, bs10=0.6), [dict(sig="DIP_SLOWING")], pos)   # the next stall, 1% against the short: everything, whatever the cost
        ev = [e for e in r["events"] if e[0] == "PULL_TRIM" and e[1]["mode"] == "exit"][0][1]; self.assertEqual((ev["all"], ev["qty"]), (True, 70))
        self.assertEqual(r["trim"][1:3], (70, "maker"))
        st.step(F(t=420, bs10=0.9), [], dict(lots=[], avg=None, last=None, last_buy_px=None, last_trim_px=None, pause=False))   # flat: the verdict and the count are the campaign's
        self.assertEqual((st.flow_n, st.flow_exit), (0, False))

    def test_it_reads_the_side_and_is_off_by_default(self):
        st = Strategy(dict(side="long", unit_qty=70, cap_usdt=20, stop_structural_on=0, flow_exit_n=3)); pos = self._pos()
        for t in (60, 120, 180): st.step(F(t=t, bs10=0.2), [], pos)                  # sellers hitting under a long
        self.assertTrue(st.flow_exit)
        st2 = Strategy(dict(side="long", unit_qty=70, cap_usdt=20, stop_structural_on=0, flow_exit_n=3)); pos2 = self._pos()
        for t in (60, 120, 180): st2.step(F(t=t, bs10=0.8), [], pos2)                # buyers hitting under a long: with us
        self.assertEqual((st2.flow_n, st2.flow_exit), (0, False))
        st3 = Strategy(dict(side="long", unit_qty=70, cap_usdt=20, stop_structural_on=0)); pos3 = self._pos()
        for t in (60, 120, 180, 240): st3.step(F(t=t, bs10=0.2), [], pos3)
        self.assertEqual((st3.flow_n, st3.flow_exit), (0, False))                      # off: not even counted
        st4 = Strategy(dict(side="long", unit_qty=70, cap_usdt=20, stop_structural_on=0, flow_exit_n=3)); pos4 = self._pos()
        for t in (60, 120, 180): st4.step(F(t=t), [], pos4)                           # no flow reading is not "against us"
        self.assertEqual((st4.flow_n, st4.flow_exit), (0, False))

    def test_the_random_exit_is_a_per_minute_coin_toss_deterministic_per_second_and_seed(self):
        st = Strategy(dict(side="short", unit_qty=70, cap_usdt=20, stop_structural_on=0, exit_random=1.0, exit_seed=1)); pos = self._pos()
        r = st.step(F(t=60, bs10=0.1), [], pos); self.assertTrue(st.flow_exit); self.assertTrue([e for e in r["events"] if e[0] == "FLOW_EXIT"][0][1]["random"])
        def run(seed, q=0.3):
            s = Strategy(dict(side="short", unit_qty=70, cap_usdt=20, stop_structural_on=0, exit_random=q, exit_seed=seed)); p = self._pos(); out = []
            for t in range(60, 1260, 60): s.step(F(t=t, bs10=0.1), [], p); out.append(s.flow_exit)
            return out
        self.assertEqual(run(7), run(7)); self.assertNotEqual(run(1, 0.5), run(2, 0.5))
        self.assertFalse(any(run(3, 0.0)))

class FailExit(unittest.TestCase):
    """fail_exit_n (2026-09-04, NEXT 3 candidate 1): the N-th stall the LIFO lot could not use puts the campaign in exit mode at that stall —
    everything, whatever the cost, taker after exit_after_s — latched until flat. Off by default (0): the count only relaxes the gate."""
    def _pos(self): return dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0, pause=False)

    def test_the_second_refused_stall_sells_everything_and_the_first_only_relaxes_the_gate(self):
        st = Strategy(dict(side="short", unit_qty=70, cap_usdt=20, stop_structural_on=0, fail_exit_n=2)); pos = self._pos()
        r = st.step(F(t=100, mid=3.03, bid=3.029, ask=3.031), [dict(sig="DIP_SLOWING")], pos)          # a stall 1% against the short: refused
        self.assertEqual([e[0] for e in r["events"] if e[0] in ("GATE_RELAX", "FAIL_EXIT", "EXIT_ARMED", "PULL_TRIM")], ["GATE_RELAX"]); self.assertIsNone(r["trim"])
        r = st.step(F(t=200, mid=3.03, bid=3.029, ask=3.031), [dict(sig="DIP_SLOWING")], pos)          # the second: the campaign leaves at this stall
        self.assertEqual([e[0] for e in r["events"] if e[0] in ("GATE_RELAX", "FAIL_EXIT", "EXIT_ARMED", "PULL_TRIM")], ["GATE_RELAX", "FAIL_EXIT", "EXIT_ARMED", "PULL_TRIM"])
        ev = [e for e in r["events"] if e[0] == "PULL_TRIM"][0][1]; self.assertEqual((ev["mode"], ev["all"], ev["qty"]), ("exit", True, 70))
        self.assertEqual(r["trim"][1:3], (70, "maker")); self.assertTrue(st.flow_exit)
        st.step(F(t=300), [], dict(lots=[], avg=None, last=None, last_buy_px=None, last_trim_px=None, pause=False))   # flat: the verdict is the campaign's
        self.assertEqual((st.fail_n, st.flow_exit), (0, False))

    def test_off_by_default_and_a_fill_restarts_the_count(self):
        st = Strategy(dict(side="short", unit_qty=70, cap_usdt=20, stop_structural_on=0)); pos = self._pos()
        for t in (100, 200, 300): r = st.step(F(t=t, mid=3.03, bid=3.029, ask=3.031), [dict(sig="DIP_SLOWING")], pos)
        self.assertEqual(st.fail_n, 3); self.assertFalse(st.flow_exit); self.assertFalse([e for e in r["events"] if e[0] == "FAIL_EXIT"])
        st2 = Strategy(dict(side="short", unit_qty=70, cap_usdt=20, stop_structural_on=0, fail_exit_n=2)); pos2 = self._pos()
        st2.step(F(t=100, mid=3.03, bid=3.029, ask=3.031), [dict(sig="DIP_SLOWING")], pos2); self.assertEqual(st2.fail_n, 1)
        pos2["lots"].append([70, 3.06, "b"]); pos2["avg"] = 3.03; pos2["last_buy_px"] = 3.06                         # an add: a new lot, a fresh expectation
        r = st2.step(F(t=200, mid=3.09, bid=3.089, ask=3.091), [dict(sig="DIP_SLOWING")], pos2)
        self.assertEqual(st2.fail_n, 1); self.assertFalse(st2.flow_exit)

class PoolClaim(unittest.TestCase):
    """The FCFS basket (2026-09-04): a campaign's first unit takes the capital pool (pos["pool"].claim(): "" = ours, else why not); adds,
    an armed entry and an entry refused for any other reason never ask."""
    class P:
        def __init__(self, why): self.why, self.n = why, 0
        def claim(self): self.n += 1; return self.why
    def flat(self, pool): return dict(lots=[], avg=None, last=None, last_buy_px=None, last_trim_px=None, pause=False, pool=pool)

    def test_the_first_unit_asks_the_pool_and_is_refused_or_armed_by_its_answer(self):
        st = Strategy(dict(side="long", unit_qty=70, cap_usdt=20, stop_structural_on=0)); pool = self.P("pool"); pos = self.flat(pool)
        r = st.step(F(bs10=0.7), [dict(sig="DIP_SLOWING", src="v")], pos)
        self.assertIsNone(st.arm); self.assertEqual([e[1]["why"] for e in r["events"] if e[0] == "SKIP"], ["pool"]); self.assertEqual(pool.n, 1)
        pool.why = ""; st.step(F(t=101, bs10=0.7), [dict(sig="DIP_SLOWING", src="v")], pos); self.assertIsNotNone(st.arm); self.assertEqual(pool.n, 2)
        st.step(F(t=102, bs10=0.7), [dict(sig="DIP_SLOWING", src="v")], pos); self.assertEqual(pool.n, 2)       # armed already: no second ask
        pos2 = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0, pause=False, pool=self.P("pool"))
        st2 = Strategy(dict(side="long", unit_qty=70, cap_usdt=20, stop_structural_on=0))
        st2.step(F(mid=2.9, bid=2.899, ask=2.901, bs10=0.8), [dict(sig="DIP_SLOWING", src="v")], pos2)
        self.assertIsNotNone(st2.arm); self.assertEqual(pos2["pool"].n, 0)                                     # an add never asks: the campaign holds the pool
        st3 = Strategy(dict(side="long", unit_qty=70, cap_usdt=20, stop_structural_on=0, entry_flow=1)); pos3 = self.flat(self.P(""))
        r = st3.step(F(bs10=0.2), [dict(sig="DIP_SLOWING", src="v")], pos3)
        self.assertEqual([e[1]["why"] for e in r["events"] if e[0] == "SKIP"], ["flow"]); self.assertEqual(pos3["pool"].n, 0)   # asked last: a refused entry never takes the pool
        st4 = Strategy(dict(side="long", unit_qty=70, cap_usdt=20, stop_structural_on=0)); pos4 = self.flat(None)
        st4.step(F(bs10=0.7), [dict(sig="DIP_SLOWING", src="v")], pos4); self.assertIsNotNone(st4.arm)          # no pool: as before
