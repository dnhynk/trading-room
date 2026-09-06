"""Invariants of the lifecycle phase reader (track_b/whale.py).  python -m unittest tests.b.test_whale"""
import unittest
from track_b.whale import footprints, phase, ignition, pre_qualifies, timeline, WHALE

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

    def test_ignition_at_a_fresh_close_high_overrides_the_old_legs_structure_and_climax_votes(self):
        ph, votes = phase(fp(off=2.0, off_close=1.0, hint15="short", ratio=0.8, run=40.0, lower_high=True, exhaustion=True, ign=7.5, up3=12.0))
        self.assertEqual(ph, "markup"); self.assertTrue(votes[0].startswith("ignite"))          # SIREN 03-22 12:00: ratio 0.8x, old structure short, votes from the prior leg — the volume explosion wins
        self.assertEqual(phase(fp(off=2.0, off_close=1.0, hint15="short", ratio=0.8, run=40.0, lower_high=True, exhaustion=True, ign=3.0, up3=12.0))[0], "climax")   # no explosion: the old votes stand
        self.assertEqual(phase(fp(off=2.0, off_close=1.0, hint15=None, ratio=0.8, run=40.0, ign=7.5, up3=-4.0))[0], "unknown")   # volume without a rise is not an ignition
        self.assertEqual(phase(fp(off=35.0, off_close=31.0, hint15=None, ratio=3.0, run=142.0, ign=9.0, up3=15.0))[0], "markdown")   # a bounce 31% under the top on volume is a squeeze/bounce, not an ignition
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
        end15 = lambda hrs: bars15 + [bar(ts + len(closes) * 900_000, closes[-1], hrs[-1]["c"], wick=0.1)]   # px is the last 15m close: close it where the hour closed
        f = footprints(hours, end15(hours), days, dict(qv=50_000.0, fund=0.15))
        self.assertGreaterEqual(f["run"], 49.0); self.assertGreater(f["off"], 10.0); self.assertTrue(f["vmax_at_high"] or f["post_red"] >= 2)
        self.assertEqual(f["hint15"], "short"); self.assertTrue(f["lower_high"]); self.assertEqual(f["ratio"], 50.0)
        self.assertEqual(phase(f)[0], "markdown")                                                                      # off >= 10 with the structure down
        f2 = footprints(hours[:-4], end15(hours[:-4]), days, dict(qv=50_000.0, fund=0.15))                                       # two hours after the peak: still near it
        self.assertLess(f2["off"], 15.0); self.assertEqual(phase(f2)[0], "climax")

class LegInProgress(unittest.TestCase):
    """leg_down (2026-09-03): the leg from the last confirmed pivot high counts as the structure turning down when that high failed to
    exceed the previous one AND the leg is under the last confirmed low — before the pivot low confirms. One sign alone is a pullback."""
    def _series(self, closes15, top):
        ts = 1_700_000_000_000; hours = [bar(ts + i * 3_600_000, 100.0, 100.0, v=100.0) for i in range(72)]
        hours += [bar(ts + (72 + i) * 3_600_000, 100.0 + (top - 100.0) * i / 5, 100.0 + (top - 100.0) * (i + 1) / 5, v=100.0) for i in range(5)]   # a run to `top`
        hours.append(bar(ts + 77 * 3_600_000, top, closes15[-1], v=100.0))                                                                          # the last hour closes where the 15m does
        days = [dict(ts=ts + i * 86_400_000, o=100, h=100, l=100, c=100, v=1, qv=1000.0) for i in range(10)]
        bars15 = [bar(ts + i * 900_000, closes15[i - 1] if i else closes15[0], closes15[i], wick=0.1) for i in range(len(closes15))]
        return footprints(hours, bars15, days, dict(qv=50_000.0, fund=None))

    def _path(self, *legs):
        z = 100.0; out = [z]
        for to, n in legs:
            for i in range(1, n + 1): out.append(round(z + (to - z) * i / n, 3))
            z = to
        return out

    def test_a_failed_high_whose_leg_breaks_the_last_low_reads_markdown_before_the_pivot_low_confirms(self):
        f = self._series(self._path((120, 8), (110, 6), (130, 8), (118, 6), (128, 6), (115, 8)), 130.0)   # H 120 < H 130 > H 128 (failed), the leg under L 118, no bounce yet
        self.assertIsNone(f["hint15"]); self.assertTrue(f["leg_down"]); self.assertGreaterEqual(f["off"], 10.0)   # confirmed structure: highs down, lows up = unreadable
        ph, why = phase(f); self.assertEqual(ph, "markdown"); self.assertIn("leg_down", why)

    def test_one_sign_alone_is_a_pullback(self):
        f = self._series(self._path((120, 8), (110, 6), (130, 8), (118, 6), (128, 6), (119, 6)), 130.0)   # a failed high, but the leg holds above the last low 118
        self.assertFalse(f["leg_down"]); self.assertNotEqual(phase(f)[0], "markdown")
        f = self._series(self._path((120, 8), (110, 6), (130, 8), (118, 6), (136, 6), (115, 8)), 136.0)   # a fresh high, then a shakeout under the last low (STO 04-01)
        self.assertFalse(f["leg_down"]); self.assertGreaterEqual(f["off"], 10.0); self.assertNotEqual(phase(f)[0], "markdown")

    def test_the_price_is_the_last_15m_close(self):
        f = self._series(self._path((120, 8), (110, 6), (130, 8), (118, 6), (128, 6), (116, 6)), 130.0)
        self.assertEqual(f["px"], 116.0); self.assertAlmostEqual(f["off"], -(116.0 / f["high48"] - 1) * 100, 1)   # under the 48h high, from the 15m close

class Audit0903(unittest.TestCase):
    """2026-09-03 audit: the ratio baseline counts closed days like the scanner, an ignition under the ratio gate is found from hourly
    candles alone, and a post-hoc timeline can say squeeze / dead only when it is given funding and a held state."""
    def _days(self, qvs): return [dict(ts=1_700_000_000_000 + i * 86_400_000, o=100, h=100, l=100, c=100, v=1, qv=q) for i, q in enumerate(qvs)]

    def test_the_ratio_baseline_is_the_last_seven_closed_days_and_three_closed_days_are_a_baseline(self):
        ts = 1_700_000_000_000
        hours = [bar(ts + i * 3_600_000, 100.0, 100.0, v=100.0) for i in range(60)]
        bars15 = [bar(ts + i * 900_000, 100.0, 100.0, wick=0.1) for i in range(30)]
        f = footprints(hours, bars15, self._days([1000.0] * 3), dict(qv=50_000.0, fund=None))
        self.assertFalse(f["new"]); self.assertEqual(f["ratio"], 50.0)                                   # three closed days = a baseline (the scanner's rule); [-8:-1] read it as a listing
        f = footprints(hours, bars15, self._days([1000.0] * 2), dict(qv=50_000.0, fund=None))
        self.assertTrue(f["new"]); self.assertEqual(f["ratio"], 99.0)

    def test_the_hourly_pre_read_finds_an_ignition_under_the_ratio_gate(self):
        ts = 1_700_000_000_000
        quiet = [bar(ts + i * 3_600_000, 100.0, 100.0, v=100.0) for i in range(60)]
        burst = [bar(ts + (60 + i) * 3_600_000, 100.0 + 4 * i, 104.0 + 4 * i, v=2000.0) for i in range(3)]   # three closed hours of 20x volume into new highs
        days = self._days([100_000.0] * 10)
        f = footprints(quiet + burst, [], days, dict(qv=300_000.0, fund=None), None)                          # hourly only (bars15 = []): ratio 3x, under the 4x gate
        self.assertLess(f["ratio"], 4.0); self.assertGreaterEqual(f["ign"], 5.0); self.assertGreater(f["up3"], 0); self.assertLessEqual(f["off_close"], 0.0)
        self.assertTrue(ignition(f)); self.assertTrue(pre_qualifies(f)); self.assertEqual(phase(f)[0], "markup")
        f2 = footprints(quiet + [bar(ts + 60 * 3_600_000, 100.0, 100.0, v=100.0)], [], days, dict(qv=300_000.0, fund=None), None)
        self.assertFalse(pre_qualifies(f2)); self.assertNotEqual(phase(f2)[0], "markup")

    def test_a_post_hoc_timeline_reads_squeeze_and_dead_only_when_given_funding_and_a_held_state(self):
        ts = 1_700_000_000_000; hours = []; c = 100.0
        for i in range(72): hours.append(bar(ts + i * 3_600_000, c, c, v=100.0))
        for i in range(24): o = c; c = 100 + 50 * (i + 1) / 24; hours.append(bar(ts + (72 + i) * 3_600_000, o, c, v=100.0 + 40 * i))   # the pump: volume grows into the high
        for i in range(30): o = c; c = c * 0.985; hours.append(bar(ts + (96 + i) * 3_600_000, o, c, v=20.0))                          # the markdown: -1.5%/h on dying volume
        bars15 = []
        for h in hours[60:]:
            for j in range(4): bars15.append(bar(h["ts"] + j * 900_000, h["o"] + (h["c"] - h["o"]) * j / 4, h["o"] + (h["c"] - h["o"]) * (j + 1) / 4, wick=0.05, v=h["v"] / 4))
        data = dict(h=hours, m15=bars15, d=self._days([2400.0] * 10), src="test"); end_ms = ts + len(hours) * 3_600_000
        plain = [ph for _, ph, _, _, _ in timeline("X", end_ms, hours=40, data=data)]
        self.assertIn("markdown", plain); self.assertNotIn("squeeze", plain); self.assertNotIn("dead", plain)   # offline: no funding, no held state
        cold = [ph for _, ph, _, _, _ in timeline("X", end_ms, hours=40, data=data, fund_at=lambda t: -0.3)]
        self.assertIn("squeeze", cold); self.assertNotIn("markdown", cold)                                         # funding fed in: every markdown row is a squeeze
        held = [ph for _, ph, _, _, _ in timeline("X", end_ms, hours=40, data=data, track_held=True)]
        self.assertEqual(held[-1], "dead"); self.assertNotEqual(held[0], "dead")                                   # the held state carries the peak volume: the collapse reads dead

if __name__ == "__main__":
    unittest.main()
