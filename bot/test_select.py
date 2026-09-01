"""Invariants of the symbol selector (bot/scan.py metrics and score, bot/select.py basket verdicts).  python -m unittest bot.test_select"""
import math, time, unittest
from bot.scan import two_way, trials, edge_of, flags_of, WIN, EDGE
from bot.select import plan, apply, flat_of, record_dict, SELECT

def sine(days=2, period=60, amp=3.0, base=100.0, vol=1000.0):
    """1m candles of a smooth cycle: amp % swings every `period` minutes."""
    out = []
    for i in range(days * WIN):
        c0 = base * (1 + amp / 100 * math.sin(2 * math.pi * i / period)); c1 = base * (1 + amp / 100 * math.sin(2 * math.pi * (i + 1) / period))
        out.append(dict(ts=1_700_000_000_000 + i * 60_000, o=c0, h=max(c0, c1) * 1.0005, l=min(c0, c1) * 0.9995, c=c1, v=vol))
    return out

def line(days=2, slope=0.0, base=100.0):
    return [dict(ts=1_700_000_000_000 + i * 60_000, o=base + slope * i, h=base + slope * i + 0.01, l=base + slope * i - 0.01, c=base + slope * (i + 1), v=1000.0) for i in range(days * WIN)]

def bounds_of(c, nw):
    n = len(c)
    return [(c[n - WIN * (j + 1)]["ts"], c[n - WIN * j]["ts"] if j else c[-1]["ts"] + 60_000) for j in range(nw)]

def row(sym, edge=0.10, entry=True, flags=(), side="long", **kw):
    r = dict(symbol=sym, edge=edge, edge_sd=0.0, p_up=0.72, trials_h=3.0, impact=0.005, out=0.10, need=0.66, entry=entry,
             flags=list(flags), side=side, qv=1e8, concept=1.0, legs_h=2.0, leg=1.0, bounce=0.8, er=0.05, atr_pct=0.2,
             tick_pct=0.01, spread_bp=1.0, fund=0.0)
    r.update(kw); return r

class Concept(unittest.TestCase):
    def test_a_cycle_tape_scores_and_a_straight_line_does_not(self):
        w = two_way(sine()[-WIN:], 3.0)
        self.assertGreater(w["legs_h"], 0.5); self.assertGreater(w["bounce"], 0.8); self.assertLess(w["er"], 0.1); self.assertGreater(w["concept"], 0)
        w2 = two_way(line(slope=0.01)[-WIN:], 3.0)
        self.assertEqual(w2["legs_h"], 0.0); self.assertEqual(w2["concept"], 0.0); self.assertGreater(w2["er"], 0.9)   # a straight move offers no legs

class Trials(unittest.TestCase):
    def test_a_cycling_tape_resolves_at_the_win_barrier_and_a_line_offers_nothing(self):
        c = sine(days=3); r = trials(c, {}, EDGE["win_pct"], EDGE["loss_pct"], int(EDGE["hold_min"]), bounds_of(c, 3))
        got = [v for v in r.values()]
        self.assertEqual(len(got), 3)
        self.assertGreater(sum(t["n"] for t in got), 100)
        self.assertEqual(sum(t["dn"] for t in got), 0)                                        # a clean cycle never reaches the loss barrier
        self.assertGreater(edge_of(got[0], EDGE["fee_pct"], 0.005), 0)
        c2 = line(days=3, slope=-0.0007)
        self.assertEqual(sum(t["n"] for t in trials(c2, {}, 0.362, 0.562, 120, bounds_of(c2, 3)).values()), 0)   # no deceleration, no trial

    def test_a_bar_holding_both_barriers_counts_adverse(self):
        c = sine(days=2, period=60, amp=3.0)
        n = len(c); i = n - 200
        c[i + 1] = dict(c[i + 1], h=c[i]["c"] * 1.05, l=c[i]["c"] * 0.90)                      # one bar that spans both barriers
        r = trials(c, {}, 0.362, 0.562, 120, [(c[i]["ts"], c[i]["ts"] + 60_000)])
        t = r.get(0)
        if t and t["n"]: self.assertEqual(t["up"], 0)                                          # the order inside a bar is not observable: read it against us

    def test_no_trial_is_opened_without_a_full_horizon_and_the_rate_stays_honest(self):
        c = sine(days=1)
        r = trials(c, {}, 0.362, 0.562, 120, bounds_of(c, 1))[0]
        self.assertLessEqual(r["bars"], WIN - 120)                                             # the last hold_min bars cannot host a trial
        self.assertEqual(edge_of(dict(n=0, bars=100, out=0.0, up=0, dn=0, to=0), 0.048, 0.0), 0.0)

class Flags(unittest.TestCase):
    def test_a_single_crash_day_is_not_a_flag(self):
        x = dict(tick_pct=0.01, spread_bp=1.0, fund=0.01)
        wins = [dict(pump=True, bounce=0.9, er=0.05, net=-12.0), dict(pump=False, bounce=0.8, er=0.1, net=3.0), dict(pump=False, bounce=0.7, er=0.1, net=1.0)]
        self.assertEqual(flags_of(x, wins, -12.0), [])                                              # one crash day with two-way tape after it
        wins[1]["pump"] = True; self.assertIn("pump", flags_of(x, wins, 5.0))                        # the shape on two days is the pump coin
        self.assertIn("pump+60%", flags_of(x, [dict(pump=False, bounce=0.9, er=0.2, net=20.0)] * 3, 60.0))          # +60% over the windows: a pump, however two-way
        self.assertEqual(flags_of(x, [dict(pump=False, bounce=0.6, er=0.1, net=-30.0)] * 2, -45.0), [])            # a crash, however deep, is not a pump

    def test_our_own_economics_disqualify(self):
        x = dict(tick_pct=0.01, spread_bp=1.0, fund=0.01, qv=2.4e7, impact=0.013)
        w = [dict(pump=False, bounce=0.9, er=0.05, net=-1.0)]
        self.assertEqual(flags_of(x, w, -1.0), [])                                              # no gate passed in: no economic flag
        self.assertIn("vol24M", flags_of(x, w, -1.0, min_vol=5e7, fee=0.048))                   # under the volume backstop
        self.assertEqual(flags_of(dict(x, qv=1e8), w, -1.0, min_vol=5e7, fee=0.048), [])
        self.assertIn("imp0.0600%", flags_of(dict(x, qv=1e8, impact=0.06), w, -1.0, min_vol=5e7, fee=0.048))   # our footprint costs more than the exchange
        self.assertEqual(flags_of(dict(x, qv=1e8, impact=0.06, edge=-9.0), w, -1.0, min_vol=5e7, fee=0.0), [])  # a bad score is never a hard flag

class Basket(unittest.TestCase):
    SEL = {**SELECT, "n": 4, "confirm": 2, "max_per_day": 2}

    def test_a_flagged_holding_leaves_whatever_its_rank(self):
        rows = [row("AUSDT", edge=9.0, flags=["vol24M"]), row("BUSDT", edge=0.1)]
        st = {}
        wind, adds, why = plan(rows, ["AUSDT"], self.SEL, st, "20260901")
        self.assertEqual([s for s, _ in wind], ["AUSDT"])                                       # top of the table and still disqualified

    def test_a_holding_missing_from_the_scan_is_kept(self):
        wind, adds, why = plan([row("BUSDT")], ["AUSDT"], self.SEL, {}, "20260901")
        self.assertEqual(wind, []); self.assertIn("not in the scan", why)                       # absence is not evidence

    def test_an_add_needs_consecutive_scans_and_a_free_slot(self):
        rows = [row("AUSDT", edge=0.5), row("BUSDT", edge=0.2)]
        st = {}
        self.assertEqual(plan(rows, [], self.SEL, st, "20260901")[1], [])                       # 1/2
        self.assertEqual(plan(rows, [], self.SEL, st, "20260901")[1], ["AUSDT", "BUSDT"])       # 2/2
        st2 = {}
        full = ["W", "X", "Y", "Z"]
        for _ in range(3): wind, adds, why = plan(rows, full, self.SEL, st2, "20260901")
        self.assertEqual(adds, []); self.assertIn("basket full", why)                           # no free slot, no streak

    def test_only_entry_eligible_and_not_excluded_symbols_are_added(self):
        rows = [row("BTCUSDT", edge=9.0), row("AUSDT", edge=0.5, entry=False), row("BUSDT", edge=0.1)]
        st = {}
        for _ in range(3): adds = plan(rows, [], {**self.SEL, "exclude": ["BTCUSDT"]}, st, "20260901")[1]
        self.assertEqual(adds, ["BUSDT"])                                                       # excluded, and a score that cannot pay the toll

    def test_openings_are_capped_per_day(self):
        rows = [row(s, edge=1.0 - i / 10) for i, s in enumerate(["AUSDT", "BUSDT", "CUSDT", "DUSDT"])]
        st = {"day": "20260901", "opens": 2}
        for _ in range(3): adds = plan(rows, [], self.SEL, st, "20260901")[1]
        self.assertEqual(adds, [])

class Apply(unittest.TestCase):
    SEL = {**SELECT, "n": 4}

    def params(self, books):
        return dict(strat=dict(symbol=list(books)[0], side="long", sides=["long", "short"]), books=books)

    def test_wind_down_then_drop_when_flat_and_the_last_book_stays(self):
        rows = [row("AUSDT", flags=["vol24M"]), row("BUSDT")]
        p = self.params({"AUSDT": {}, "BUSDT": {}})
        acts = apply(p, rows, [("AUSDT", "vol24M")], [], {"AUSDT": False, "BUSDT": True}, self.SEL)
        self.assertEqual(acts, [("wind", "AUSDT", "vol24M")]); self.assertEqual(p["books"]["AUSDT"]["wind_down"], 1)
        self.assertIn("AUSDT", p["books"])                                                      # still holding: not removed
        acts = apply(p, rows, [("AUSDT", "vol24M")], [], {"AUSDT": True, "BUSDT": True}, self.SEL)
        self.assertEqual(acts, [("drop", "AUSDT", "flat")]); self.assertNotIn("AUSDT", p["books"])
        p2 = self.params({"AUSDT": {"wind_down": 1}})
        apply(p2, rows, [("AUSDT", "vol24M")], [], {"AUSDT": True}, self.SEL)
        self.assertEqual(list(p2["books"]), ["AUSDT"])                                          # never empty: whole-wallet fallback would double the size

    def test_every_book_carries_one_nth_of_the_wallet(self):
        p = self.params({"AUSDT": {"wallet_frac": 1.0}})
        apply(p, [row("AUSDT")], [], [], {}, self.SEL)
        self.assertEqual(p["books"]["AUSDT"]["wallet_frac"], 0.25)                              # 1/n while a slot is empty, so the sum never exceeds one wallet

    def test_an_add_opens_a_book_and_moves_the_default_symbol_when_the_old_one_is_gone(self):
        rows = [row("BUSDT", edge=0.5), row("AUSDT", flags=["vol24M"])]
        p = self.params({"AUSDT": {"wind_down": 1}})
        acts = apply(p, rows, [("AUSDT", "vol24M")], ["BUSDT"], {"AUSDT": True}, self.SEL)
        self.assertEqual([a[0] for a in acts], ["drop", "add"])
        self.assertEqual(list(p["books"]), ["BUSDT"]); self.assertEqual(p["strat"]["symbol"], "BUSDT")
        self.assertEqual(p["strat"]["sides"], ["long", "short"])                                # 쌍검 is preserved across a basket change
        self.assertIn("BUSDT", p["record"]); self.assertEqual(p["record"]["BTCUSDT"], ["candle1m"])

    def test_a_full_basket_refuses_another_book(self):
        p = self.params({s: {} for s in ("AUSDT", "BUSDT", "CUSDT", "DUSDT")})
        apply(p, [row("EUSDT")], [], ["EUSDT"], {}, self.SEL)
        self.assertNotIn("EUSDT", p["books"]); self.assertEqual(len(p["books"]), 4)

class Flat(unittest.TestCase):
    def test_a_stale_or_loaded_engine_is_not_flat(self):
        now = time.time(); fresh = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        empty = dict(pos=dict(lots=[]), working=dict(buy=None, trim=None))
        self.assertTrue(flat_of({"A": dict(t=fresh, books={"long": empty})}, now)["A"])
        self.assertFalse(flat_of({"A": dict(t=fresh, books={"long": dict(pos=dict(lots=[1]), working=dict(buy=None, trim=None))})}, now)["A"])
        stale = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 600))
        self.assertFalse(flat_of({"A": dict(t=stale, books={"long": empty})}, now)["A"])        # an engine that is down is not flat

class Record(unittest.TestCase):
    def test_books_are_always_recorded_and_flagged_candidates_are_not(self):
        rows = [row("AUSDT"), row("BUSDT", flags=["pump"]), row("CUSDT"), row("DUSDT")]
        rec = record_dict(["AUSDT", "ZUSDT"], rows, {**SELECT, "record_top": 1, "record_extra": ["EUSDT"]})
        self.assertIn("ZUSDT", rec)                                                             # a book with no row still gets its tape
        self.assertNotIn("BUSDT", rec); self.assertIn("CUSDT", rec); self.assertIn("EUSDT", rec)

if __name__ == "__main__":
    unittest.main()
