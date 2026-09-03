"""Invariants of the lifecycle phase reader (bot/whale.py).  python -m unittest bot.test_whale"""
import unittest
from bot.whale import footprints, phase, WHALE

def bar(ts, o, c, wick=0.2, v=1000.0): return dict(ts=ts, o=o, h=max(o, c) + wick, l=min(o, c) - wick, c=c, v=v, qv=v)

def fp(**kw):
    f = dict(px=129.0, high48=150.0, run=50.0, off=14.0, off_close=14.0, age_h=6, vmax_at_high=False, post_red=0, vmax_share=0.1, upwick=0.2, lower_high=False,
             exhaustion=False, hint15=None, twoway24=30.0, ratio=10.0, new=False, qv=4e7, fund=0.0, dead=False, atr15_pct=2.0, ign=None, up3=None)
    f.update(kw)
    if "off_close" not in kw: f["off_close"] = f["off"]     # unless a test separates wick and close, the two distances agree
    return f

class Rules(unittest.TestCase):
    def test_the_order_of_the_rules(self):
        self.assertEqual(phase(fp(dead=True))[0], "dead")
        self.assertEqual(phase(fp(ratio=1.0, twoway24=5.0))[0], "quiet")
        self.assertEqual(phase(fp(off=14.0, hint15="short"))[0], "markdown")
        self.assertEqual(phase(fp(off=14.0, hint15="short", fund=-0.2))[0], "squeeze")
        self.assertEqual(phase(fp(off=5.0, hint15="short"))[0], "unknown")                       # the top is not far enough in for markdown, structure down: nothing
        self.assertEqual(phase(fp(off=32.0, off_close=31.0, hint15=None, run=142.0))[0], "markdown")   # far under the top after a run, structure unreadable: the top is in by distance (AKE)
        self.assertEqual(phase(fp(off=30.7, off_close=-10.0, hint15=None, run=359.0, ratio=20.0))[0], "markup")   # 30% under a spike WICK but AT the highest close: still the markup (STO 04-02 00:00, then +230%)
        self.assertEqual(phase(fp(off=31.8, off_close=0.0, hint15=None, run=202.0, exhaustion=True))[0], "climax")   # exhaustion at the max close under a blow-off wick: the top (SYN 06-26 00:00)
        self.assertEqual(phase(fp(off=37.3, off_close=8.0, hint15=None, run=202.0))[0], "unknown")                   # a bounce 8% under the climax close is not a markup (SYN 06-26 03:00, then -13%)
        self.assertEqual(phase(fp(off=12.0, off_close=4.0, hint15="long", run=142.0))[0], "markup")                  # just under a fresh close-high: the markup continues
        self.assertEqual(phase(fp(off=12.0, hint15=None, run=142.0))[0], "unknown")              # not far enough for the distance read, structure silent: nothing
        self.assertEqual(phase(fp(off=32.0, hint15="long", run=142.0))[0], "unknown")            # structure says up: distance alone does not call a markdown
        self.assertEqual(phase(fp(off=32.0, hint15=None, run=10.0))[0], "unknown")               # no run behind it: a drifter, not a pump's markdown
        self.assertEqual(phase(fp(off=3.0, hint15="long", ratio=10.0, run=50.0))[0], "markup")
        self.assertEqual(phase(fp(off=3.0, hint15=None, ratio=99.0, new=True, run=140.0))[0], "markup")   # a fresh listing (AKE)
        self.assertEqual(phase(fp(off=3.0, hint15="long", ratio=2.5, run=50.0))[0], "unknown")    # an old baseline (TRUMP 2.8x, MAGMA 1.8x) is not an episode
        self.assertEqual(phase(fp(off=1.8, hint15=None, ratio=3.8, run=142.0))[0], "markup")      # but a doubling in 48h is one whatever the baseline (AKE)

    def test_climax_needs_two_votes_near_the_high_and_effort_fail_needs_the_red_follow_through(self):
        self.assertEqual(phase(fp(off=3.0, hint15="long", vmax_at_high=True, post_red=0))[0], "markup")               # the biggest hour at the high alone is just a markup
        self.assertEqual(phase(fp(off=3.0, hint15="long", vmax_at_high=True, post_red=2))[0], "markup")               # one vote
        ph, votes = phase(fp(off=3.0, hint15="long", vmax_at_high=True, post_red=2, upwick=0.45))
        self.assertEqual(ph, "climax"); self.assertEqual(sorted(votes), ["effort_fail2", "upwick0.45"])
        self.assertEqual(phase(fp(off=3.0, hint15="long", lower_high=True, fund=0.2))[0], "climax")
        self.assertEqual(phase(fp(off=20.0, hint15="long", lower_high=True, fund=0.2))[0], "unknown")                 # too far under the high to be a top read
        self.assertEqual(phase(fp(off=3.0, hint15="long", post_red=3, upwick=0.45))[0], "markup")                     # reds without the volume peak at the high

    def test_quiet_exhaustion_after_a_big_pump_reads_climax_but_a_modest_run_does_not(self):
        self.assertEqual(phase(fp(off=2.0, hint15=None, run=142.0, exhaustion=True))[0], "climax")                    # AKE: +142% then fading bodies + dry volume, no red bar
        self.assertIn("exhaustion", phase(fp(off=2.0, hint15=None, run=142.0, exhaustion=True))[1])
        self.assertEqual(phase(fp(off=2.0, hint15="long", run=50.0, exhaustion=True))[0], "markup")                   # a modest run: exhaustion alone is one vote, not a climax
        self.assertEqual(phase(fp(off=2.0, hint15="long", run=142.0, exhaustion=True, upwick=0.45))[0], "climax")     # with a second vote it needs no big_run guard
        self.assertNotEqual(phase(fp(off=20.0, hint15="long", run=142.0, exhaustion=True))[0], "climax")             # far under the high: exhaustion does not force a top read

class Footprints(unittest.TestCase):
    def test_a_pump_then_a_rollover_measures_and_reads_as_climax_then_markdown(self):
        ts = 1_700_000_000_000; hours = []; c = 100.0
        for i in range(72): hours.append(bar(ts + i * 3_600_000, c, c, v=100.0))
        for i in range(24): o = c; c = 100 + 50 * (i + 1) / 24; hours.append(bar(ts + (72 + i) * 3_600_000, o, c, v=100.0 + 40 * i))   # volume grows into the high
        top = c
        for i in range(6): o = c; c = c * 0.97; hours.append(bar(ts + (96 + i) * 3_600_000, o, c, wick=1.0, v=300.0))                  # red follow-through
        days = [dict(ts=ts + i * 86_400_000, o=100, h=100, l=100, c=100, v=1, qv=1000.0) for i in range(10)]
        closes = [150.0]; z = 150.0
        for leg in ((-2, 10), (2, 5), (-2, 10), (2, 5), (-2, 10), (2, 5)):
            for _ in range(leg[1]): z += leg[0]; closes.append(z)
        bars15 = [bar(ts + i * 900_000, closes[i - 1] if i else 150.0, closes[i], wick=0.1) for i in range(len(closes))]
        f = footprints(hours, bars15, days, dict(qv=50_000.0, fund=0.15))
        self.assertGreaterEqual(f["run"], 49.0); self.assertGreater(f["off"], 10.0); self.assertTrue(f["vmax_at_high"] or f["post_red"] >= 2)
        self.assertEqual(f["hint15"], "short"); self.assertTrue(f["lower_high"]); self.assertEqual(f["ratio"], 50.0)
        self.assertEqual(phase(f)[0], "markdown")                                                                      # off >= 10 with the structure down
        f2 = footprints(hours[:-4], bars15, days, dict(qv=50_000.0, fund=0.15))                                       # two hours after the peak: still near it
        self.assertLess(f2["off"], 15.0); self.assertEqual(phase(f2)[0], "climax")

if __name__ == "__main__":
    unittest.main()
