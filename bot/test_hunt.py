"""Invariants of the short-hunting side pipeline (bot/hunt.py).  python -m unittest bot.test_hunt"""
import unittest
from bot.hunt import measures, flags_of, exit_flags, verdict, apply, HUNT

def bar(ts, o, c, wick=0.2, v=1000.0): return dict(ts=ts, o=o, h=max(o, c) + wick, l=min(o, c) - wick, c=c, v=v, qv=v * c)

def pump_hours():
    """120 closed 1H bars: 72 flat at 100, 24 rising to 150 (the climax), 24 swinging 141/129 under it (the top is in, churn is on)."""
    out, ts, c = [], 1_700_000_000_000, 100.0
    for i in range(72): out.append(bar(ts + i * 3_600_000, c, c))
    for i in range(24): o = c; c = 100 + 50 * (i + 1) / 24; out.append(bar(ts + (72 + i) * 3_600_000, o, c))
    for i in range(24): o = c; c = 141.0 if i % 2 == 0 else 129.0; out.append(bar(ts + (96 + i) * 3_600_000, o, c))
    return out

def minutes(px=129.0, n=200, wick=0.4): return [bar(1_700_000_000_000 + i * 60_000, px, px, wick=wick) for i in range(n)]

def down_zigzag_15m():
    """Closed 15m bars with lower highs and lower lows in legs of 20 points (2 per bar): the structure reader must say short."""
    closes, c = [], 150.0
    for leg in ((-2, 10), (2, 5), (-2, 10), (2, 5), (-2, 10), (2, 5)):
        for _ in range(leg[1]): c += leg[0]; closes.append(c)
    return [bar(1_700_000_000_000 + i * 900_000, closes[i - 1] if i else 150.0, closes[i], wick=0.1) for i in range(len(closes))]

def row(sym, **kw):
    r = dict(symbol=sym, px=129.0, qv=4e7, base7=4e6, ratio=10.0, chg24=-5.0, fund=0.01, oi=1e7, spread_bp=2.0, lever_max=25, min_notional=1.0, tick_pct=0.001,
             twoway24=30.0, twoway6=8.0, net24=-3.0, run=50.0, off=14.0, high48=150.2, atr_pct=0.6, atr15_pct=2.0, hint15="short", flags=[])
    r.update(kw); r["flags"] = list(kw.get("flags", flags_of(r, HUNT))); return r

class Measures(unittest.TestCase):
    def test_a_pump_that_rolled_over_measures_as_run_off_churn_and_a_short_structure(self):
        m = measures(pump_hours(), minutes(), down_zigzag_15m(), 129.0)
        self.assertGreaterEqual(m["run"], 49.0)                  # 100 -> 150 into the 48h high
        self.assertAlmostEqual(m["off"], (150.2 - 129.0) / 150.2 * 100, 0)   # 14% under the climax high (wick included)
        self.assertGreater(m["twoway24"], 100.0)                 # 24 swings of 12 points: the churn is there
        self.assertLess(abs(m["net24"]), 15.0)
        self.assertAlmostEqual(m["atr_pct"], 0.8 / 129.0 * 100, 1)
        self.assertEqual(m["hint15"], "short")

    def test_a_flat_tape_has_no_run_and_no_churn(self):
        flat = [bar(1_700_000_000_000 + i * 3_600_000, 100.0, 100.0) for i in range(120)]
        m = measures(flat, minutes(px=100.0), [bar(1_700_000_000_000 + i * 900_000, 100.0, 100.0) for i in range(60)], 100.0)
        self.assertLess(m["run"], 1.0); self.assertLess(m["twoway24"], 1.0); self.assertIsNone(m["hint15"])

class Flags(unittest.TestCase):
    def test_every_entry_veto_fires_alone_and_a_good_row_carries_none(self):
        self.assertEqual(flags_of(row("A"), HUNT), [])
        for kw, tag in ((dict(qv=5e6), "vol"), (dict(ratio=2.0), "ratio"), (dict(run=10.0), "run"), (dict(off=1.0), "top"), (dict(twoway24=10.0), "twoway"),
                        (dict(atr_pct=1.5), "atr"), (dict(atr_pct=0.1), "atr"), (dict(atr_pct=None), "atr"), (dict(fund=-0.3), "fund"),
                        (dict(lever_max=10), "lever"), (dict(hint15=None), "hint"), (dict(hint15="long"), "hint")):
            f = flags_of(row("A", **kw), HUNT); self.assertEqual(len(f), 1, (kw, f)); self.assertTrue(f[0].startswith(tag), (kw, f))

    def test_exit_flags_read_the_held_state_and_ignore_entry_only_vetoes(self):
        held = dict(peak=1e8, climax=150.2)
        self.assertEqual(exit_flags(row("A", off=1.0, ratio=1.0, run=5.0), held, HUNT), [])          # off/ratio/run are entry questions
        self.assertTrue(exit_flags(row("A", qv=2e7), held, HUNT)[0].startswith("dead"))            # 0.2 of the peak seen while held
        self.assertEqual(exit_flags(row("A", px=151.0), held, HUNT), ["newhigh"])
        self.assertTrue(exit_flags(row("A", twoway24=5.0), held, HUNT)[0].startswith("flat"))
        self.assertTrue(exit_flags(row("A", hint15="long"), held, HUNT)[0].startswith("hint"))
        self.assertEqual(exit_flags(row("A", hint15=None), held, HUNT), [])                         # no structure read = keep

class Verdicts(unittest.TestCase):
    def test_an_empty_book_adds_the_churn_leader_after_confirm_scans_and_apply_writes_one_short_book(self):
        rows = [row("A", twoway24=30.0), row("B", twoway24=20.0), row("C", ratio=1.0)]
        st, p = {}, dict(strat=dict(symbol="OLD", sides=["long", "short"]), books={})
        v = verdict(rows, {}, HUNT, st, 1000.0); self.assertEqual((v["top"], v["add"]), ("A", None)); self.assertEqual(st["streak"], {"A": 1})
        v = verdict(rows, {}, HUNT, st, 1000.0); self.assertEqual(v["add"], "A")
        acts = apply(p, rows, v, {}, HUNT, st, 1000.0)
        self.assertEqual([a[:2] for a in acts], [("add", "A")])
        self.assertEqual(p["books"], {"A": {"wallet_frac": 1.0, "sides": ["short"], "hunt": 1}})
        self.assertEqual((p["strat"]["symbol"], p["strat"]["side"], p["strat"]["sides"]), ("A", "short", ["long", "short"]))   # the common sides stay
        self.assertEqual(set(p["record"]), {"A", "B", "BTCUSDT"}); self.assertEqual(st["held"]["A"]["climax"], 150.2)

    def test_a_held_coin_leaves_on_confirmed_exit_flags_and_is_replaced_only_when_flat(self):
        rows = [row("A", qv=1e7), row("B", twoway24=20.0)]                     # A: volume 0.1 of its peak
        st = dict(held={"A": dict(peak=1e8, climax=150.2)}); books = {"A": {"wallet_frac": 1.0, "sides": ["short"], "hunt": 1}}
        p = dict(strat=dict(symbol="A"), books=dict(books))
        v = verdict(rows, p["books"], HUNT, st, 1000.0); self.assertIsNone(v["wind"]); self.assertEqual(st["xstreak"], {"A": 1})
        v = verdict(rows, p["books"], HUNT, st, 1000.0); self.assertEqual(v["wind"][0], "A"); self.assertIn("dead", v["wind"][1])
        self.assertEqual(v["add"], "B")                                          # B topped two scans: the slot opens as A leaves
        acts = apply(p, rows, v, {"A": False}, HUNT, st, 1000.0)                 # A not flat yet: winds down, nothing replaces it
        self.assertEqual([a[:2] for a in acts], [("wind", "A")]); self.assertEqual(list(p["books"]), ["A"]); self.assertEqual(p["books"]["A"]["wind_down"], 1)
        v = verdict(rows, p["books"], HUNT, st, 2000.0)
        acts = apply(p, rows, v, {"A": True}, HUNT, st, 2000.0)                  # flat: dropped and replaced in one write, cooldown set
        self.assertEqual([a[:2] for a in acts], [("drop", "A"), ("add", "B")])
        self.assertEqual(list(p["books"]), ["B"]); self.assertEqual(p["strat"]["symbol"], "B")
        self.assertGreater(st["cool"]["A"], 2000.0 + 23 * 3600)
        v = verdict([row("A"), row("B")], p["books"], HUNT, st, 3000.0)          # A is in cooldown: not a candidate even when eligible again
        self.assertIsNone(v["top"])

    def test_a_basket_or_a_hand_book_makes_the_job_refuse(self):
        v = verdict([row("A")], {"HYPEUSDT": {"wallet_frac": 0.3}}, HUNT, {}, 1000.0)
        self.assertIn("non-hunt", v["refuse"]); self.assertIsNone(v["add"])

    def test_books_never_empties_when_the_only_book_is_gone_and_nothing_qualifies(self):
        st = dict(held={"A": {}}); p = dict(strat=dict(symbol="A"), books={"A": {"wallet_frac": 1.0, "sides": ["short"], "hunt": 1, "wind_down": 1}})
        v = verdict([row("C", ratio=1.0)], p["books"], HUNT, st, 1000.0); self.assertIsNone(v["add"])
        apply(p, [row("C", ratio=1.0)], v, {"A": True}, HUNT, st, 1000.0)
        self.assertEqual(list(p["books"]), ["A"])                                # the flat, wound-down book stays until a replacement opens

if __name__ == "__main__":
    unittest.main()
