"""Invariants of the phase evidence tables (track_b/phases.py).  python -m unittest tests.b.test_phases"""
import unittest
from track_b.phases import phase_at, forward_table, ledger_table, flow_table, end_ms_of, fund_from_flow

def tl_row(t_ms, ph, px, next1h=None, next4h=None):
    return (t_ms, ph, [], {"px": px, "next1h": next1h}, next4h)

H = 3_600_000

class Join(unittest.TestCase):
    def test_phase_at_takes_the_last_row_at_or_before_the_time(self):
        tl = [tl_row(0, "markup", 1.0), tl_row(H, "climax", 1.1), tl_row(2 * H, "markdown", 1.0)]
        self.assertEqual(phase_at(tl, H - 1), "markup")
        self.assertEqual(phase_at(tl, H), "climax")
        self.assertEqual(phase_at(tl, 2 * H + 999), "markdown")
        self.assertIsNone(phase_at([], H))

    def test_forward_table_averages_the_price_move_after_each_phase(self):
        tl = [tl_row(0, "markdown", 1.0, next1h=-0.5, next4h=-2.0), tl_row(H, "markdown", 1.0, next1h=-0.1, next4h=None), tl_row(2 * H, "markup", 1.0, next1h=+0.4, next4h=+1.0)]
        fwd = forward_table(tl)
        self.assertEqual(fwd["markdown"], (2, -0.3, -2.0))     # next4h averages only the non-None
        self.assertEqual(fwd["markup"], (1, 0.4, 1.0))

    def test_ledger_places_each_cycle_by_the_phase_of_its_open_hour_and_aggregates_by_side(self):
        tl = [tl_row(_ms("2026-09-01 00:00:00"), "markdown", 1.0), tl_row(_ms("2026-09-01 04:00:00"), "markup", 1.0)]
        done = [cyc("2026-09-01 01:00:00", "long", -0.5, 100, 1.0),    # opened in markdown: a long that lost
                cyc("2026-09-01 02:00:00", "short", +0.3, 100, 1.0),   # markdown short: won
                cyc("2026-09-01 05:00:00", "long", +0.4, 100, 1.0)]    # markup long: won
        led = ledger_table(done, tl)
        self.assertEqual(led[("markdown", "long")], (1, 0.0, -0.500, -0.5))    # net% = -0.5 / (100 x 1.0) x 100
        self.assertEqual(led[("markdown", "short")], (1, 1.0, +0.300, +0.3))
        self.assertEqual(led[("markup", "long")], (1, 1.0, +0.400, +0.4))

    def test_flow_table_reads_cvd_oi_and_funding_by_phase(self):
        tl = [tl_row(0, "markup", 1.0), tl_row(H, "climax", 1.1)]
        flow = {0: dict(cvd=5000.0, oi0=1000.0, oi1=1100.0, fund=0.02, trades=50),      # markup: buyers, OI rising
                H: dict(cvd=-3000.0, oi0=1100.0, oi1=990.0, fund=0.05, trades=40)}       # climax: sellers, OI falling
        ft = flow_table(tl, flow)
        self.assertEqual(ft["markup"], (1, 5000.0, +10.0, 0.02))
        self.assertEqual(ft["climax"], (1, -3000.0, -10.0, 0.05))
        self.assertNotIn("markdown", ft)                                                # an hour with no flow is skipped

    def test_an_hour_with_no_trades_is_skipped_by_flow(self):
        tl = [tl_row(0, "markup", 1.0)]
        self.assertEqual(flow_table(tl, {0: dict(cvd=0.0, oi0=None, oi1=None, fund=None, trades=0)}), {})

def _ms(t):
    import time
    return int(time.mktime(time.strptime(t, "%Y-%m-%d %H:%M:%S")) * 1000)

def cyc(t0, side, net, qty, entry):
    return dict(symbol="X", side=side, t0=t0, t1=t0, net=net, qty=qty, entry=entry, gross=net, fee=0.0)

class Funding(unittest.TestCase):
    """The post-hoc timeline takes its funding from the recordings, so squeeze / fund_hot can fire in the evidence table at all — with
    ticker=None every historical row had fund None and those votes were unreachable (audit 2026-09-03)."""
    def test_an_hour_carries_the_last_funding_at_or_before_it(self):
        flow = {0: {"fund": 0.05}, 2 * H: {"fund": None}, 3 * H: {"fund": -0.12}}
        at = fund_from_flow(flow)
        self.assertEqual(at(H // 2), 0.05)                    # inside the recorded hour
        self.assertEqual(at(2 * H + 5), 0.05)                 # an hour with no funding push inherits the last one
        self.assertEqual(at(3 * H), -0.12)
        self.assertIsNone(at(-1))                             # before anything was recorded
        self.assertIsNone(fund_from_flow({}))                 # no recordings: the votes stay silent, as offline
        self.assertIsNone(fund_from_flow({0: {"fund": None}}))

class DayWindow(unittest.TestCase):
    """--day names a UTC day (the recordings and the nightly are keyed by UTC hour). The old code applied the local zone twice and in
    KST ended the day 18 h early, so the nightly's phases table covered 00:00-06:00 UTC of the day it reported on."""
    def test_a_day_ends_at_2359_utc_and_an_explicit_end_stays_local(self):
        import calendar, time
        self.assertEqual(end_ms_of("20260903"), calendar.timegm((2026, 9, 3, 23, 59, 59, 0, 0, 0)) * 1000)
        self.assertEqual(end_ms_of(None, "2026-09-03 20:00"), int(time.mktime(time.strptime("2026-09-03 20:00", "%Y-%m-%d %H:%M")) * 1000))
        self.assertEqual(end_ms_of("20260903", "2026-09-03 20:00"), end_ms_of(None, "2026-09-03 20:00"))   # --end wins

if __name__ == "__main__":
    unittest.main()
