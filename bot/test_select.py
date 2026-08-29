"""Invariants of the symbol selector (bot/scan.py metrics and engine proxy, bot/select.py verdicts).  python -m unittest bot.test_select"""
import math, unittest
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
        wins = [dict(pump=True, bounce=0.9, er=0.05), dict(pump=False, bounce=0.8, er=0.1), dict(pump=False, bounce=0.7, er=0.1)]
        self.assertEqual(flags_of(x, wins, -12.0), [])                                              # one crash day with two-way tape after it
        wins[1]["pump"] = True; self.assertIn("pump", flags_of(x, wins, 5.0))                        # the shape on two days is the pump coin
        self.assertIn("parabolic+60%", flags_of(x, [dict(pump=False, bounce=0.1, er=0.2)] * 3, 60.0))
        self.assertTrue(any(f.startswith("ER") for f in flags_of(x, [dict(pump=False, bounce=0.5, er=0.5)], 3.0)))   # one-way right now

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
        a, why, b = decide(self.rows(best_proxy=1.2), "A", sel, dict(since=0, streak={"B": 1}), True, "d", now); self.assertEqual(a, "keep")   # not 1.5x better
        st4 = dict(since=0, streak={"B": 1}, switch_day="d", switches=1)
        a, why, b = decide(self.rows(), "A", sel, st4, True, "d", now); self.assertEqual(a, "keep"); self.assertIn("today", why)    # one switch a day
        rows = self.rows(); rows[1]["flags"] = ["ER0.40"]
        a, why, b = decide(rows, "A", sel, dict(since=now - 3600, streak={"B": 1}), True, "d", now); self.assertEqual(a, "switch")   # a flagged incumbent waives dwell and ratio

    def test_record_set_is_the_incumbent_plus_candidates_plus_btc_candles(self):
        rows = [dict(symbol=s, flags=[] if s != "F" else ["pump"], proxy=1, concept=1) for s in ("B", "C", "F", "D", "BTCUSDT")]
        rec = record_dict("A", rows, dict(SELECT, record_top=2))
        self.assertEqual(list(rec), ["A", "B", "C", "BTCUSDT"]); self.assertEqual(rec["BTCUSDT"], ["candle1m"])

if __name__ == "__main__":
    unittest.main()
