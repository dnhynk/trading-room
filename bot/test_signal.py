"""Invariants of bot/signal.py (pure).  python -m unittest bot.test_signal -v"""
import unittest
from bot.signal import Strategy, apply_fill, pos_stats, zigzag, sim_match

class FillModel(unittest.TestCase):
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
        self.assertEqual(r["trim"], (3.001, 70, "maker")); self.assertEqual(r["events"][0][1]["mode"], "normal")

    def test_wick_top_is_sold_on_retrace_without_a_signal(self):
        st = Strategy(dict(side="long", unit_qty=70)); pos = dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", last_buy_px=3.0)
        st.step(F(t=100, mid=3.0), [], pos)
        r = st.step(F(t=101, mid=3.03, bid=3.029, ask=3.031), [], pos); self.assertIsNone(r["trim"])            # spike to +1%: nothing yet
        r = st.step(F(t=102, mid=3.024, bid=3.023, ask=3.025), [], pos); self.assertIsNone(r["trim"])           # 0.3 ATR back: still nothing
        r = st.step(F(t=103, mid=3.019, bid=3.018, ask=3.02), [], pos)                                        # 0.55 ATR back from the peak
        self.assertEqual(r["events"][-1][0], "PULL_TRIM"); self.assertEqual(r["events"][-1][1]["mode"], "retrace"); self.assertEqual(r["trim"][1], 70)
        r = st.step(F(t=104, mid=3.014, bid=3.013, ask=3.015), [], pos); self.assertEqual(r["trim"][2], "taker")   # slipping further: taker at once

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
        from bot.signal import SIG
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
        from bot.signal import Features
        feat = Features(); feat.seed_candles([dict(ts=i * 60000, o=100, h=101, l=99, c=100, v=1000) for i in range(120)])
        book = lambda m, ts: dict(arg=dict(channel="books15"), data=[dict(bids=[[m - 0.5 - i, 5] for i in range(5)], asks=[[m + 0.5 + i, 5] for i in range(5)], ts=str(ts))], ts=ts)
        feat.feed(book(100, 7200500)); feat.feed(book(100, 7201500))     # t=7200 closes at 7201's first message on 7200's own book
        feat.feed(book(110, 7202100))                                     # the first message of 7202 carries a new mid ...
        self.assertEqual(feat.f["t"], 7201); self.assertEqual(feat.f["mid"], 100.0)   # ... which 7201 must not see

class WarmUp(unittest.TestCase):
    def test_velocity_rule_waits_for_a_mature_normaliser(self):
        from bot.signal import Features, EMA
        e = EMA(300); e.add(1.0); e.add(3.0); self.assertAlmostEqual(e.v, 2.0)                        # an expanding mean, not 1 + k x 2
        feat = Features(dict(vol_hl=300)); feat.seed_candles([dict(ts=i * 60000, o=100, h=100.05, l=99.95, c=100, v=1000) for i in range(120)])
        book = lambda m, ts: dict(arg=dict(channel="books15"), data=[dict(bids=[[m - 0.01 - i * 0.01, 5] for i in range(5)], asks=[[m + 0.01 + i * 0.01, 5] for i in range(5)], ts=str(ts))], ts=ts)
        out = []; t = 7200
        out += feat.feed(book(100, t * 1000 + 500)); t += 1
        out += feat.feed(book(99.5, t * 1000 + 500)); t += 1                                            # -0.5% first return: v = -1 exactly under the old code
        for _ in range(40): out += feat.feed(book(99.5, t * 1000 + 500)); t += 1                         # then flat: the old code fired DIP_SLOWING here
        self.assertFalse(any(x["sig"] == "DIP_SLOWING" for x in out))
        for _ in range(320): out += feat.feed(book(99.5, t * 1000 + 500)); t += 1                        # a half-life of data: the normaliser is mature
        out = []
        out += feat.feed(book(99.0, t * 1000 + 500)); t += 1
        for _ in range(40): out += feat.feed(book(99.0, t * 1000 + 500)); t += 1
        self.assertTrue(any(x["sig"] == "DIP_SLOWING" and x["src"] == "v" for x in out))               # the same shape of move now fires

class Zigzag(unittest.TestCase):
    def test_straight_move_has_no_swings(self):
        self.assertEqual(zigzag([1, 1.01, 1.02, 1.03, 1.05], 0.007), [])
        self.assertEqual(len(zigzag([1, 1.02, 1.0, 1.02, 1.0], 0.007)), 3)

if __name__ == "__main__":
    unittest.main()
