"""Campaign accounting, time causality and tail-probability arithmetic."""
import math
import unittest
from bot.research_b import binomial_upper, break_even_stop, reconstruct, grouped_interval, scorecard


class Research(unittest.TestCase):
    def test_exact_binomial_inversion_and_zero_stop_sample_requirement(self):
        self.assertAlmostEqual(binomial_upper(0, 21), 1 - .05 ** (1/21))
        self.assertAlmostEqual(binomial_upper(1, 73), .063343, places=5)
        self.assertEqual(binomial_upper(0, 0), 1)
        self.assertEqual(binomial_upper(3, 3), 1)
        self.assertGreater(binomial_upper(0, 30), .09)

    def test_break_even_uses_log_growth_and_retains_losing_nonstop_exits(self):
        a = (math.log1p(.01) + math.log1p(-.005)) / 2
        self.assertAlmostEqual(break_even_stop([.01, -.005], .05), a/(a-math.log1p(-.05)))
        self.assertEqual(break_even_stop([-.01], .05), 0)
        self.assertIsNone(break_even_stop([], .05))

    def test_partial_fills_are_one_campaign_and_future_scans_never_join(self):
        def e(kind, minute, **kw):
            return dict(ev=kind, t=f"2026-09-04 12:{minute:02}:00", symbol="X", side="long", **kw)
        events = [e("HUNT_ADD", 0), e("START", 0, mode="live"),
                  e("SIZING", 1, wallet=100, unit_qty=2, cap_usdt=5, atr=1),
                  e("FILL", 2, role="buy", qty=1, px=100, pnl=-.02, fee=.02, oid="in", pos_qty=1),
                  e("FILL", 3, role="buy", qty=1, px=100, pnl=-.02, fee=.02, oid="in", pos_qty=2),
                  e("FILL", 4, role="trim", qty=1, px=101, pnl=.98, fee=.02, pos_qty=1),
                  e("FILL", 5, role="trim", qty=1, px=101, pnl=.98, fee=.02, pos_qty=0)]
        scans = [dict(t="2026-09-04 12:01:00", hunt=dict(strat=dict(max_units=1)), rows=[dict(symbol="X", phase="markup")]),
                 dict(t="2026-09-04 12:03:00", hunt=dict(strat=dict(max_units=3)), rows=[dict(symbol="X", phase="markdown")])]
        done, pending, quality = reconstruct(events, scans)
        self.assertEqual(len(done), 1); c = done[0]
        self.assertEqual(c["phase"], "markup"); self.assertEqual(c["profile"]["max_units"], 1)
        self.assertAlmostEqual(c["net"], 1.92); self.assertAlmostEqual(c["return"], .0192)
        self.assertEqual(c["orders"], ["in"]); self.assertEqual(c["hold_s"], 180)
        self.assertFalse(pending); self.assertEqual(quality["orphan_closes"], 0)
        cut, pending, _ = reconstruct(events, scans, until="2026-09-04 12:04:00")
        self.assertFalse(cut); self.assertEqual(len(pending), 1)

    def test_a_single_day_cannot_create_a_precise_bootstrap(self):
        cs = [dict(t0="2026-09-04 12:00:00", symbol="X", issues=[], **{"return": .01}) for _ in range(100)]
        self.assertEqual(grouped_interval(cs)["blocks"], 1)
        self.assertIsNone(grouped_interval(cs)["mean_ci95"])

    def test_incomplete_campaign_losses_are_disclosed_not_silently_discarded(self):
        def e(kind, second, **kw): return dict(ev=kind, t=f"2026-09-04 12:00:{second:02}", symbol="X", side="long", **kw)
        events = [e("START", 0, mode="live", track="B"), e("SIZING", 1, wallet=100),
                  e("FILL", 2, role="buy", qty=1, px=100, pnl=-.02, fee=.02, pos_qty=1),
                  e("START", 3, mode="live", track="B", books=dict(long=dict(lots=[])))]
        r = scorecard(events, [], "2026-09-04 00:00:00", "2026-09-04 23:59:59")
        self.assertEqual(r["summary"]["usable"], 0)
        self.assertEqual(r["quality"]["incomplete_campaigns"], 1)
        self.assertEqual(r["accounting"]["unresolved_recorded_net_usdt"], -.02)
        self.assertEqual(r["accounting"]["known_campaign_fill_net_usdt"], -.02)

    def test_exit_settings_and_build_are_part_of_an_exact_cohort(self):
        events = []
        for i, wait in ((1, 10), (2, 60)):
            def e(kind, sec, **kw): return dict(ev=kind, t=f"2026-09-04 12:0{i}:{sec:02}", symbol="X", side="long", **kw)
            events += [e("START", 0, track="B", mode="live"),
                       e("CAMPAIGN_OPEN", 1, wallet=100, build="build-a", sig=dict(v_hl=8), fees=dict(maker=.0002),
                         profile=dict(unit_frac=.75, cap_frac=.05, max_units=1, trim_taker_after_s=wait)),
                       e("FILL", 2, role="buy", qty=1, px=100, pnl=-.02, fee=.02),
                       e("FILL", 3, role="trim", qty=1, px=101, pnl=.98, fee=.02)]
        r = scorecard(events, [], "2026-09-04 00:00:00", "2026-09-04 23:59:59")
        self.assertTrue(r["summary"]["exact_entry_configuration"])
        self.assertTrue(r["summary"]["mixed_profiles"])
        self.assertIsNone(r["summary"]["scenario_break_even_stop_rate"])
        self.assertEqual(len(r["tables"]["profile"]), 2)


if __name__ == "__main__": unittest.main()
