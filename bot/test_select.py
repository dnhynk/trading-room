"""Invariants of the symbol selector (bot/scan.py metrics and engine proxy, bot/select.py verdicts).  python -m unittest bot.test_select"""
import math, time, unittest
from bot.scan import two_way, proxy, flags_of, WIN
from bot.select import decide, record_dict, SELECT

def sine(days=2, period=60, amp=3.0, base=100.0, vol=1000.0):
    """1m candles of a smooth cycle: amp % swings every `period` minutes."""
    out = []
    for i in range(days * WIN):
        c0 = base * (1 + amp / 100 * math.sin(2 * math.pi * i / period)); c1 = base * (1 + amp / 100 * math.sin(2 * math.pi * (i + 1) / period))
        out.append(dict(ts=1_700_000_000_000 + i * 60_000, o=c0, h=max(c0, c1) * 1.0005, l=min(c0, c1) * 0.9995, c=c1, v=vol))
    return out

def line(days=2, slope=0.0, base=100.0):
    return [dict(ts=1_700_000_000_000 + i * 60_000, o=base + slope * i, h=base + slope * i + 0.01, l=base + slope * i - 0.01, c=base + slope * (i + 1), v=1000.0) for i in range(days * WIN)]

class Concept(unittest.TestCase):
    def test_a_cycle_tape_scores_and_a_straight_line_does_not(self):
        w = two_way(sine()[-WIN:], 3.0)
        self.assertGreater(w["legs_h"], 0.5); self.assertGreater(w["bounce"], 0.8); self.assertLess(w["er"], 0.1); self.assertGreater(w["concept"], 0)
        w2 = two_way(line(slope=0.01)[-WIN:], 3.0)
        self.assertEqual(w2["legs_h"], 0.0); self.assertEqual(w2["concept"], 0.0); self.assertGreater(w2["er"], 0.9)   # a straight move offers no legs

class Proxy(unittest.TestCase):
    def test_the_engine_proxy_cycles_a_sine_and_idles_on_a_line(self):
        c = sine(days=3); bounds = [(c[-WIN]["ts"], c[-1]["ts"] + 60_000), (c[-2 * WIN]["ts"], c[-WIN]["ts"])]
        r = proxy(c, [], dict(unit_frac=1.5, cap_frac=0.3), {}, bounds)
        self.assertGreater(r[0]["cycles"], 3); self.assertGreater(r[0]["pnl"], 0); self.assertEqual(r[0]["stops"], 0)
        r2 = proxy(line(days=3), [], {}, {}, bounds)
        self.assertEqual(r2[0]["adds"], 0); self.assertEqual(r2[0]["pnl"], 0.0)

class Flags(unittest.TestCase):
    def test_a_single_crash_day_is_not_a_flag(self):
        x = dict(tick_pct=0.01, spread_bp=1.0, fund=0.01)
        wins = [dict(pump=True, bounce=0.9, er=0.05, net=-12.0), dict(pump=False, bounce=0.8, er=0.1, net=3.0), dict(pump=False, bounce=0.7, er=0.1, net=1.0)]
        self.assertEqual(flags_of(x, wins, -12.0), [])                                              # one crash day with two-way tape after it
        wins[1]["pump"] = True; self.assertIn("pump", flags_of(x, wins, 5.0))                        # the shape on two days is the pump coin
        self.assertIn("pump+60%", flags_of(x, [dict(pump=False, bounce=0.9, er=0.2, net=20.0)] * 3, 60.0))          # +60% over the windows: a pump, however two-way
        self.assertIn("pump+62%", flags_of(x, [dict(pump=False, bounce=0.8, er=0.09, net=54.0), dict(pump=False, bounce=0.9, er=0.02, net=6.0)], 62.0))   # PROMUSDT 2026-08-30: +54% in a day
        self.assertEqual(flags_of(x, [dict(pump=False, bounce=0.6, er=0.1, net=-30.0)] * 2, -45.0), [])            # a crash, however deep, is not a pump
        self.assertTrue(any(f.startswith("ER") for f in flags_of(x, [dict(pump=False, bounce=0.5, er=0.5, net=3.0)], 3.0)))   # one-way right now

class Verdict(unittest.TestCase):
    def rows(self, inc_proxy=1.0, inc_concept=1.0, best_proxy=2.0, best_concept=1.5, best_flags=()):
        return [dict(symbol="B", proxy=best_proxy, concept=best_concept, flags=list(best_flags), side="short"),
                dict(symbol="A", proxy=inc_proxy, concept=inc_concept, flags=[], side="long")]

    def test_hysteresis_dwell_and_flat_gate_the_switch(self):
        sel = dict(SELECT); st = dict(since=0); now = 10 * 86400
        a, why, b = decide(self.rows(), "A", sel, st, True, "d", now); self.assertEqual((a, b["symbol"]), ("keep", "B")); self.assertIn("1/2", why)
        a, why, b = decide(self.rows(), "A", sel, st, False, "d", now); self.assertEqual(a, "wait")                  # confirmed twice, engine positioned
        a, why, b = decide(self.rows(), "A", sel, st, True, "d", now); self.assertEqual(a, "switch")
        st2 = dict(since=now - 3600, streak={"B": 1})
        a, why, b = decide(self.rows(), "A", sel, st2, True, "d", now); self.assertEqual(a, "keep"); self.assertIn("dwell", why)   # held only an hour
        st3 = dict(since=0, streak={"B": 1})
        a, why, b = decide(self.rows(best_concept=0.5), "A", sel, st3, True, "d", now); self.assertEqual(a, "keep"); self.assertEqual(st3["streak"], {})   # less two-way: never
        a, why, b = decide(self.rows(best_concept=1.2), "A", sel, dict(since=0, streak={"B": 1}), True, "d", now); self.assertEqual(a, "keep")   # better, but not 1.5x
        st4 = dict(since=0, streak={"B": 1}, switch_day="d", switches=1)
        a, why, b = decide(self.rows(), "A", sel, st4, True, "d", now); self.assertEqual(a, "keep"); self.assertIn("today", why)    # one switch a day
        rows = self.rows(); rows[1]["flags"] = ["ER0.40"]
        a, why, b = decide(rows, "A", sel, dict(since=now - 3600, streak={"B": 1}), True, "d", now); self.assertEqual(a, "switch")   # a flagged incumbent waives dwell and ratio

    def test_every_engine_must_be_flat_before_a_switch(self):
        """포트폴리오에선 엔진이 여럿이고 state-<SYMBOL>.json 도 여럿이다. 하나라도 물려 있으면 전환은 없다."""
        from bot.select import engine_flat
        now = 1_700_000_000
        def snap(lots): return dict(t=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 5)),
                                    books=dict(long=dict(pos=dict(lots=lots), working=dict(buy=None, trim=None))))
        self.assertTrue(engine_flat(snap([]), now))                                  # 단일 엔진 스냅샷도 그대로 받는다
        self.assertTrue(engine_flat({"A": snap([]), "B": snap([])}, now))
        self.assertFalse(engine_flat({"A": snap([]), "B": snap([[1, 2, "x"]])}, now))   # 한쪽이 물려 있으면 flat 아님
        stale = snap([]); stale["t"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 600))
        self.assertFalse(engine_flat({"A": snap([]), "B": stale}, now))               # 죽은 엔진은 flat 이 아니다
        self.assertFalse(engine_flat({}, now))

    def test_the_proxy_neither_elects_nor_vetoes(self):
        """The live symbol's proxy was negative in 12 of 18 scans, so a proxy gate cannot choose the symbol the engine is trading."""
        sel = dict(SELECT); now = 10 * 86400
        rows = [dict(symbol="B", proxy=-9.0, concept=2.0, flags=[], side="long"), dict(symbol="A", proxy=5.0, concept=1.0, flags=[], side="long")]
        a, why, b = decide(rows, "A", sel, dict(since=0, streak={"B": 1}), True, "d", now)
        self.assertEqual(a, "switch")                       # a negative proxy does not veto a more two-way candidate
        rows = [dict(symbol="B", proxy=99.0, concept=1.0, flags=[], side="long"), dict(symbol="A", proxy=-9.0, concept=1.0, flags=[], side="long")]
        a, why, b = decide(rows, "A", sel, dict(since=0, streak={"B": 1}), True, "d", now)
        self.assertEqual(a, "keep")                         # and a huge proxy does not elect one that is no more two-way

    def test_an_incumbent_missing_from_the_scan_never_elects_a_challenger(self):
        """2026-09-01 09:55: TRUMPUSDT fell under the 24h volume gate and left the table, so the incumbent read 0.00/0.00 and any
        candidate cleared a zero hurdle. rank(always=incumbent) keeps it measured; this is the backstop if it is missing anyway."""
        st = dict(since=0, streak={"B": 1})
        rows = [dict(symbol="B", proxy=9.0, concept=9.0, flags=[], side="long")]
        a, why, b = decide(rows, "A", dict(SELECT), st, True, "d", 10 * 86400)
        self.assertEqual(a, "keep"); self.assertIn("missing", why); self.assertEqual(st["streak"], {})

    def test_the_prom_near_switch_is_blocked(self):
        """2026-08-30 11:41 live scan: PROMUSDT 9.13/4.03 vs TRUMPUSDT 1.34/3.10 reached 'qualifies 1/2' under the proxy gate; the
        tick backtest then put PROM at -14.32/day against TRUMP +3.54 (RULES 도구 절). concept x1.5 = 4.65 > 4.03 blocks it."""
        st = dict(since=0, streak={"PROMUSDT": 1})
        rows = [dict(symbol="PROMUSDT", proxy=9.13, concept=4.03, flags=[], side="short"),
                dict(symbol="TRUMPUSDT", proxy=1.34, concept=3.10, flags=[], side="long")]
        a, why, b = decide(rows, "TRUMPUSDT", dict(SELECT), st, True, "d", 10 * 86400)
        self.assertEqual(a, "keep"); self.assertEqual(st["streak"], {})

    def test_a_switch_keeps_both_sides_when_the_incumbent_runs_dual(self):
        import bot.select as S
        rows = [dict(symbol="B", flags=[], proxy=1, concept=1)]; written = {}
        orig = S.write_json; S.write_json = lambda path, obj, **kw: written.update(obj)
        try:
            p = dict(strat=dict(symbol="A", side="long", sides=["long", "short"]), record={})
            self.assertEqual(S.switch(p, dict(symbol="B", side="short"), rows, dict(SELECT)), "short")
            self.assertEqual((p["strat"]["symbol"], p["strat"]["side"], p["strat"]["sides"]), ("B", "short", ["long", "short"]))   # 쌍검 survives the switch
            p = dict(strat=dict(symbol="A", side="long", sides=["long"]), record={})
            S.switch(p, dict(symbol="B", side="short"), rows, dict(SELECT)); self.assertEqual(p["strat"]["sides"], ["short"])       # a single book follows the structure side
        finally: S.write_json = orig

    def test_record_set_is_the_incumbent_plus_candidates_plus_btc_candles(self):
        rows = [dict(symbol=s, flags=[] if s != "F" else ["pump"], proxy=1, concept=1) for s in ("B", "C", "F", "D", "BTCUSDT")]
        rec = record_dict("A", rows, dict(SELECT, record_top=2))
        self.assertEqual(list(rec), ["A", "B", "C", "BTCUSDT"]); self.assertEqual(rec["BTCUSDT"], ["candle1m"])

    def test_the_record_set_still_carries_the_proxys_own_pick(self):
        """The proxy no longer decides but stays under validation: whatever it ranks first keeps a tape for the tick backtest."""
        rows = [dict(symbol=s, flags=[], proxy=p, concept=c) for s, p, c in (("B", 0.0, 3.0), ("C", 0.0, 2.0), ("D", 9.0, 0.5))]
        self.assertEqual(list(record_dict("A", rows, dict(SELECT, record_top=2))), ["A", "B", "C", "D", "BTCUSDT"])

if __name__ == "__main__":
    unittest.main()
