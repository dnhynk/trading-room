"""Invariants of the symbol selector (bot/scan.py metrics and score, bot/select.py basket verdicts).  python -m unittest bot.test_select"""
import math, time, unittest
from bot.scan import two_way, trials, edge_of, flags_of, WIN, EDGE
from bot import select
from bot.select import plan, apply, flat_of, flats_now, record_dict, recent_engines, SELECT

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

    def test_every_main_book_carries_one_nth_of_the_main_pool(self):
        p = self.params({"AUSDT": {"wallet_frac": 1.0}})
        apply(p, [row("AUSDT")], [], [], {}, self.SEL)
        self.assertEqual(p["books"]["AUSDT"]["wallet_frac"], 0.225)                             # (1 - probe 0.1) / n while a slot is empty, so the sum never exceeds one wallet
        apply(p, [row("AUSDT")], [], [], {}, dict(self.SEL, probe=0))
        self.assertEqual(p["books"]["AUSDT"]["wallet_frac"], 0.25)                              # no probe slot: the whole wallet over n

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

def cyc(sym, t0, t1, net, qty=10.0, entry=100.0, gross=None):
    return dict(symbol=sym, t0=t0, t1=t1, qty=qty, entry=entry, net=net, gross=net + 0.02 if gross is None else gross)

class Evidence(unittest.TestCase):
    NOW = time.mktime(time.strptime("2026-09-02 12:00:00", "%Y-%m-%d %H:%M:%S"))

    def test_live_edge_per_hour_from_the_window_of_completed_cycles(self):
        from bot.select import evidence
        done = [cyc("AUSDT", "2026-09-02 08:00:00", "2026-09-02 08:30:00", 1.0), cyc("AUSDT", "2026-09-02 09:00:00", "2026-09-02 10:00:00", 3.0),
                cyc("AUSDT", "2026-08-20 09:00:00", "2026-08-20 10:00:00", -50.0)]                # outside the window: not evidence
        e = evidence(self.NOW, 5, done)["AUSDT"]
        self.assertEqual(e["n"], 2); self.assertAlmostEqual(e["mean"], 0.2); self.assertAlmostEqual(e["hours"], 2.0); self.assertAlmostEqual(e["cyc_h"], 1.0)
        self.assertAlmostEqual(e["edge_h"], 0.2); self.assertGreater(e["se"], 0)

    def test_the_leader_needs_two_measured_books_and_a_gap_outside_the_noise(self):
        from bot.select import leader_of
        sel = dict(SELECT, min_cycles=10, sigma=2.0)
        ev = {"AUSDT": dict(n=50, edge_h=0.30, se_h=0.02), "BUSDT": dict(n=50, edge_h=0.10, se_h=0.02), "CUSDT": dict(n=5, edge_h=9.0, se_h=0.01)}
        self.assertEqual(leader_of(["AUSDT", "BUSDT", "CUSDT"], ev, sel), "AUSDT")                 # C is unmeasured (n < min_cycles) however high it reads
        ev["BUSDT"]["se_h"] = 0.15
        self.assertIsNone(leader_of(["AUSDT", "BUSDT"], ev, sel))                                # the gap is inside 2 se: equal shares
        self.assertIsNone(leader_of(["AUSDT"], ev, sel))

    def test_more_books_than_slots_never_over_allocates_and_the_weakest_measured_leaves(self):
        from bot.select import shares, verdict
        sel = dict(SELECT, n=3, probe=0.1, min_cycles=10, confirm=2)
        books = {"AUSDT": {}, "BUSDT": {}, "CUSDT": {}, "DUSDT": {}}                          # n was cut to 3 while four are held
        s = shares(books, sel); self.assertAlmostEqual(sum(s.values()), 0.9); self.assertAlmostEqual(s["AUSDT"], 0.225)   # divided by the books, not by n
        ev = {"AUSDT": dict(n=40, mean=0.2, se=0.05, edge_h=0.4, se_h=0.1), "BUSDT": dict(n=40, mean=-0.05, se=0.05, edge_h=-0.1, se_h=0.1), "CUSDT": dict(n=2, mean=-9, se=1, edge_h=-9, se_h=1)}
        v = verdict([row(s_) for s_ in books], books, sel, {}, "20260902", ev, time.time())
        self.assertEqual([s_ for s_, _ in v["evict"]], ["BUSDT"])                                  # the weakest MEASURED book leaves; C's -9 on 2 cycles is not evidence, D has none

    def test_shares_sum_to_the_pool_and_tilt_to_the_leader(self):
        from bot.select import shares
        sel = dict(SELECT, n=4, probe=0.1, lead=0.5)
        books = {"AUSDT": {}, "BUSDT": {}, "CUSDT": {}, "PUSDT": {"probe": 1}}
        s = shares(books, sel)
        self.assertAlmostEqual(s["AUSDT"], 0.225); self.assertAlmostEqual(s["PUSDT"], 0.1); self.assertLessEqual(sum(s.values()), 1.0)   # one slot empty: money idle, never over-allocated
        s = shares(books, sel, leader="AUSDT")
        self.assertAlmostEqual(s["AUSDT"], 0.45); self.assertAlmostEqual(s["BUSDT"], 0.15); self.assertAlmostEqual(s["CUSDT"], 0.15)     # leader half of the pool, the rest over n - 1 slots
        s = shares(books, dict(sel, sigma_norm=1), sigma={"AUSDT": 2.0, "BUSDT": 4.0, "CUSDT": 4.0})
        self.assertAlmostEqual(s["AUSDT"], 2 * s["BUSDT"]); self.assertAlmostEqual(s["AUSDT"] + s["BUSDT"] + s["CUSDT"], 0.675)          # 1/sigma, same total

class Verdict(unittest.TestCase):
    SEL = {**SELECT, "n": 3, "confirm": 2, "max_per_day": 2, "min_cycles": 10, "sigma": 2.0, "probe_days": 5, "per_cluster": 2}
    NOW = time.mktime(time.strptime("2026-09-02 12:00:00", "%Y-%m-%d %H:%M:%S"))

    def test_a_main_book_negative_on_its_own_ledger_is_evicted_after_confirm_scans(self):
        from bot.select import verdict
        rows = [row("AUSDT"), row("BUSDT")]; books = {"AUSDT": {}, "BUSDT": {}}
        ev = {"AUSDT": dict(n=40, mean=-0.30, se=0.05, edge_h=-0.6, se_h=0.1), "BUSDT": dict(n=40, mean=-0.02, se=0.05, edge_h=-0.04, se_h=0.1)}
        st = {}
        v = verdict(rows, books, self.SEL, st, "20260902", ev, self.NOW); self.assertEqual(v["evict"], [])          # first scan: a streak of one
        v = verdict(rows, books, self.SEL, st, "20260902", ev, self.NOW)
        self.assertEqual([s for s, _ in v["evict"]], ["AUSDT"])                                                     # -0.30 is 6 se under zero; B's -0.02 is noise
        ev["AUSDT"]["mean"] = 0.1
        v = verdict(rows, books, self.SEL, st, "20260902", ev, self.NOW); self.assertEqual(v["evict"], []); self.assertEqual(st["evict"]["AUSDT"], 0)

    def test_the_probe_is_promoted_over_the_weakest_measured_main_or_ended(self):
        from bot.select import verdict
        rows = [row("AUSDT"), row("BUSDT"), row("PUSDT")]; books = {"AUSDT": {}, "BUSDT": {}, "PUSDT": {"probe": 1}}
        ev = {"AUSDT": dict(n=40, mean=0.2, se=0.02, edge_h=0.40, se_h=0.04), "BUSDT": dict(n=40, mean=0.05, se=0.02, edge_h=0.10, se_h=0.04),
              "PUSDT": dict(n=12, mean=0.3, se=0.03, edge_h=0.60, se_h=0.06)}
        st = {"probe_t": {"PUSDT": self.NOW - 86400}}
        v = verdict(rows, books, self.SEL, st, "20260902", ev, self.NOW)
        self.assertEqual(v["promote"][:2], ("PUSDT", "BUSDT")); self.assertIsNone(v["probe_end"])                    # beat the weakest measured main by > 2 se
        ev["PUSDT"]["edge_h"] = 0.12
        v = verdict(rows, books, self.SEL, st, "20260902", ev, self.NOW)
        self.assertIsNone(v["promote"]); self.assertEqual(v["probe_end"][0], "PUSDT")                                # judged and not better: it ends
        ev["PUSDT"]["n"] = 3; st["probe_t"]["PUSDT"] = self.NOW - 6 * 86400
        v = verdict(rows, books, self.SEL, st, "20260902", ev, self.NOW); self.assertEqual(v["probe_end"][0], "PUSDT")   # unjudgeable after probe_days: ends too
        st["probe_t"]["PUSDT"] = self.NOW - 86400
        v = verdict(rows, books, self.SEL, st, "20260902", ev, self.NOW); self.assertIsNone(v["probe_end"]); self.assertIsNone(v["promote"])   # young and thin: keeps probing

    def test_the_probe_slot_takes_the_next_candidate_after_confirm_scans_and_respects_cooldown(self):
        from bot.select import verdict
        rows = [row("AUSDT"), row("BUSDT"), row("CUSDT", edge=0.9), row("DUSDT", edge=0.5)]; books = {"AUSDT": {}, "BUSDT": {}}
        st = {"cool": {"CUSDT": self.NOW + 86400}}
        v = verdict(rows, books, self.SEL, st, "20260902", {}, self.NOW)
        self.assertEqual(v["adds"], []); self.assertIsNone(v["probe"])                                              # streaks of one
        v = verdict(rows, books, self.SEL, st, "20260902", {}, self.NOW)
        self.assertEqual(v["adds"], ["DUSDT"]); self.assertIsNone(v["probe"])                                       # C is cooling down: D fills the free main slot, nothing left to probe
        books["DUSDT"] = {}; rows.append(row("EUSDT", edge=0.3))
        for _ in range(2): v = verdict(rows, books, self.SEL, st, "20260902", {}, self.NOW)
        self.assertEqual(v["probe"], "EUSDT")                                                                        # basket full: the best candidate probes

    def test_no_third_main_book_of_one_driver_cluster(self):
        from bot.select import verdict
        rows = [row("AUSDT", cluster="AUSDT"), row("BUSDT", cluster="AUSDT"), row("CUSDT", edge=0.9, cluster="AUSDT"), row("XUSDT", edge=0.2, cluster="XUSDT")]
        books = {"AUSDT": {}, "BUSDT": {}}; st = {}
        for _ in range(2): v = verdict(rows, books, self.SEL, st, "20260902", {}, self.NOW)
        self.assertEqual(v["adds"], ["XUSDT"])                                                                       # C scores best but would be the third of the AUSDT cluster

class ApplyProbe(unittest.TestCase):
    SEL = {**SELECT, "n": 3, "probe": 0.1, "lead": 0.5, "min_cycles": 10, "probe_cooldown_d": 7}

    def params(self, books):
        return dict(strat=dict(symbol=list(books)[0], side="long", sides=["long", "short"]), books=books)

    def test_probe_opens_at_its_fraction_promotion_winds_the_loser_down_and_the_probe_takes_the_slot_when_it_is_gone(self):
        rows = [row("AUSDT"), row("BUSDT"), row("CUSDT"), row("PUSDT", edge=0.7)]
        p = self.params({"AUSDT": {}, "BUSDT": {}, "CUSDT": {}}); st = {}
        acts = apply(p, rows, [], [], {}, self.SEL, probe="PUSDT", st=st, now=1000.0)
        self.assertEqual([a[:2] for a in acts], [("probe", "PUSDT")]); self.assertEqual(p["books"]["PUSDT"], {"probe": 1, "wallet_frac": 0.1}); self.assertEqual(st["probe_t"]["PUSDT"], 1000.0)
        self.assertAlmostEqual(sum(b["wallet_frac"] for b in p["books"].values()), 1.0)
        acts = apply(p, rows, [], [], {}, self.SEL, promote=("PUSDT", "BUSDT", "better"), st=st, now=2000.0)
        self.assertEqual([a[:2] for a in acts], [("promote", "PUSDT")]); self.assertEqual(p["books"]["BUSDT"]["wind_down"], 1); self.assertEqual(p["books"]["PUSDT"]["promote"], 1)
        self.assertEqual(p["books"]["PUSDT"]["wallet_frac"], 0.1)                                                     # still the probe's share while the loser holds its slot
        acts = apply(p, rows, [], [], {"BUSDT": True}, self.SEL, st=st, now=3000.0)
        self.assertEqual([a[:2] for a in acts], [("drop", "BUSDT"), ("main", "PUSDT")])
        self.assertNotIn("probe", p["books"]["PUSDT"]); self.assertAlmostEqual(p["books"]["PUSDT"]["wallet_frac"], 0.3)   # a main book now: (1 - 0.1) / 3
        self.assertGreater(st["cool"]["BUSDT"], 3000.0)                                                                 # the evicted book waits out the cooldown

    def test_a_finished_probe_winds_down_and_the_leader_takes_half_the_pool(self):
        rows = [row("AUSDT"), row("BUSDT"), row("PUSDT")]
        p = self.params({"AUSDT": {}, "BUSDT": {}, "PUSDT": {"probe": 1}}); st = {}
        ev = {"AUSDT": dict(n=40, edge_h=0.5, se_h=0.05), "BUSDT": dict(n=40, edge_h=0.1, se_h=0.05)}
        acts = apply(p, rows, [], [], {}, self.SEL, probe_end=("PUSDT", "not better"), ev=ev, st=st, now=1000.0)
        self.assertEqual([a[:2] for a in acts], [("probe_end", "PUSDT")]); self.assertEqual(p["books"]["PUSDT"]["wind_down"], 1)
        self.assertAlmostEqual(p["books"]["AUSDT"]["wallet_frac"], 0.45); self.assertAlmostEqual(p["books"]["BUSDT"]["wallet_frac"], 0.225)   # leader: 0.9 x 0.5; the rest over n - 1 = 2 slots
        apply(p, rows, [], [], {"PUSDT": True}, self.SEL, ev=ev, st=st, now=2000.0)
        self.assertNotIn("PUSDT", p["books"]); self.assertIn("PUSDT", st["cool"])

class Clusters(unittest.TestCase):
    def test_correlated_symbols_share_a_cluster_and_the_slow_flag_reads_trials_per_hour(self):
        from bot.scan import clusters, corr
        import random
        random.seed(1); base = [random.gauss(0, 1) for _ in range(96)]
        r = lambda k, noise: {1000 + i: base[i] * k + random.gauss(0, noise) for i in range(96)}
        rows = [dict(symbol="ETH", qv=3e9, _r15=r(1, 0.3)), dict(symbol="SOL", qv=1e9, _r15=r(1, 0.5)), dict(symbol="XAG", qv=1e8, _r15={1000 + i: random.gauss(0, 1) for i in range(96)})]
        clusters(rows, 0.5)
        self.assertEqual([x["cluster"] for x in rows], ["ETH", "ETH", "XAG"]); self.assertNotIn("_r15", rows[0])
        self.assertAlmostEqual(corr(list(range(12)), [2 * x for x in range(12)]), 1.0); self.assertEqual(corr([1.0] * 12, list(range(12))), 0.0)
        self.assertEqual(corr([1, 2, 3], [2, 4, 6]), 0.0)                                                            # under 10 points a correlation is noise: none
        x = dict(tick_pct=0.01, spread_bp=1.0, fund=0.0, qv=1e8, trials_h=0.8)
        self.assertEqual(flags_of(x, [], 0.0, 0.0, 0.0, 1.3), ["slow0.8/h"]); self.assertEqual(flags_of(dict(x, trials_h=2.0), [], 0.0, 0.0, 0.0, 1.3), [])

class Flat(unittest.TestCase):
    def test_a_stale_or_loaded_engine_is_not_flat(self):
        now = time.time(); fresh = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        empty = dict(pos=dict(lots=[]), working=dict(buy=None, trim=None))
        self.assertTrue(flat_of({"A": dict(t=fresh, books={"long": empty})}, now)["A"])
        self.assertFalse(flat_of({"A": dict(t=fresh, books={"long": dict(pos=dict(lots=[1]), working=dict(buy=None, trim=None))})}, now)["A"])
        stale = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 600))
        self.assertFalse(flat_of({"A": dict(t=stale, books={"long": empty})}, now)["A"])        # an engine that is down is not flat

    def test_the_verdict_reads_the_state_files_after_the_scan(self):
        """A scan takes over a minute; states read before it are older than the 60 s flat window by the time the verdict is made, so
        BOOK_DROP could never fire (every SELECT through 2026-09-02 shows flat=False, the wound-down book included)."""
        now = time.time(); empty = dict(pos=dict(lots=[]), working=dict(buy=None, trim=None))
        stamp = lambda age: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - age))
        states = {"A": dict(t=stamp(0), books={"long": empty}), "B": dict(t=stamp(70), books={"long": empty})}
        orig = select.load_states; select.load_states = lambda: states
        try: flats = flats_now()
        finally: select.load_states = orig
        self.assertTrue(flats["A"]); self.assertFalse(flats["B"])                                # what the files say now, not what they said before the scan
        self.assertEqual(recent_engines(states, now), ["A", "B"]); self.assertEqual(recent_engines({"C": dict(t=stamp(90000))}, now), [])

    def test_a_wound_down_book_whose_flag_is_gone_adds_again(self):
        p = dict(strat=dict(symbol="AUSDT"), books={"AUSDT": {"wind_down": 1}, "BUSDT": {}})
        acts = apply(p, [row("AUSDT"), row("BUSDT")], [], [], {"AUSDT": False}, {**SELECT, "n": 4})
        self.assertEqual(acts, [("resume", "AUSDT", "flags cleared")]); self.assertNotIn("wind_down", p["books"]["AUSDT"])   # ER is re-judged every scan: the fact that sent it out is gone
        p2 = dict(strat=dict(symbol="AUSDT"), books={"AUSDT": {"wind_down": 1}, "BUSDT": {}})
        apply(p2, [row("AUSDT", flags=["vol24M"]), row("BUSDT")], [("AUSDT", "vol24M")], [], {"AUSDT": False}, {**SELECT, "n": 4})
        self.assertEqual(p2["books"]["AUSDT"]["wind_down"], 1)                                     # still flagged: still winding down

class Record(unittest.TestCase):
    def test_books_are_always_recorded_and_flagged_candidates_are_not(self):
        rows = [row("AUSDT"), row("BUSDT", flags=["pump"]), row("CUSDT"), row("DUSDT")]
        rec = record_dict(["AUSDT", "ZUSDT"], rows, {**SELECT, "record_top": 1, "record_extra": ["EUSDT"]}, recent=["YUSDT"])
        self.assertIn("ZUSDT", rec)                                                             # a book with no row still gets its tape
        self.assertNotIn("BUSDT", rec); self.assertIn("CUSDT", rec); self.assertIn("EUSDT", rec)
        self.assertIn("YUSDT", rec)                                                             # a book that just left keeps its tape for a day

if __name__ == "__main__":
    unittest.main()
