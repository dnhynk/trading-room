"""Invariants of the evidence tools (bot/replay.py hold-vs-sell, bot/capture.py direction capture).  python -m unittest bot.test_tools"""
import os, unittest
from bot.replay import hold_pnl
from bot.capture import capture, held_at

class HoldVsSell(unittest.TestCase):
    def test_hold_with_a_trail_exits_at_the_trail_or_the_end(self):
        self.assertAlmostEqual(hold_pnl(100.0, [101, 102, 103, 102.4, 104], 1, 0.5), 2.4)      # the trail (0.5 under 103) is hit at 102.4
        self.assertAlmostEqual(hold_pnl(100.0, [101, 102, 103], 1, 0.5), 3.0)                   # never hit: the end of the path
        self.assertAlmostEqual(hold_pnl(100.0, [99.8, 99.4, 99.6], 1, 0.5), -0.6)               # a stall that reverses: the trail limits the give-back
        self.assertAlmostEqual(hold_pnl(100.0, [99, 98, 98.6], -1, 0.5), 1.4)                   # the short side mirrors
        self.assertIsNone(hold_pnl(100.0, [], 1, 0.5))

class Sweeps(unittest.TestCase):
    def test_a_dip_under_a_confirmed_pivot_that_reclaims_is_one_sweep_with_its_depth(self):
        from bot.sweeps import sweeps
        secs = []
        for i in range(2400):                                   # 40 min at 100 with a pivot low 99 known from the start; at 20 min a 30 s dip to 98.5, then back
            mid = 98.5 if 1200 <= i < 1230 else 100.0
            secs.append((1_700_000_000 + i, mid, 1.0, (99.0,), (101.0,)))
        r = sweeps(secs, 1)
        self.assertEqual(len(r), 1); self.assertTrue(r[0]["reclaimed"]); self.assertAlmostEqual(r[0]["depth_atr"], 0.5); self.assertEqual(r[0]["t1"] - r[0]["t0"], 30)
        self.assertEqual(sweeps(secs, -1), [])                                                 # nothing over the high
        young = [(t, m, a, (99.0,) if i >= 1100 else (), h) for i, (t, m, a, l, h) in enumerate(secs)]
        self.assertEqual(sweeps(young, 1), [])                                                 # a level younger than 15 min (the leg in progress) does not count

class Follow(unittest.TestCase):
    def test_the_active_side_follows_the_hint_and_flips_only_when_flat(self):
        from bot.backtest import Engine
        eng = Engine(None, dict(side="long", unit_qty=70), follow="15m", sides=["long", "short"])
        eng.follow_step(1); self.assertEqual((eng.active, eng.flips), ("long", 0))                  # no hint yet: the configured (incumbent) side trades
        eng.feat.side_hint_15m = "short"; eng.follow_step(2); self.assertEqual((eng.active, eng.flips), ("short", 1))
        eng.books["short"].pos["lots"] = [[70, 3.0, "a"]]
        eng.feat.side_hint_15m = "long"; eng.follow_step(3); self.assertEqual(eng.active, "short")   # positioned: the flip waits
        eng.books["short"].pos["lots"] = []; eng.books["short"].work["buy"] = dict(px=3.1, qty=70, filled=0.0); eng.books["short"].strat.arm = (9, 3.1, 70)
        eng.follow_step(4); self.assertEqual((eng.active, eng.flips), ("long", 2))
        self.assertIsNone(eng.books["short"].work["buy"]); self.assertIsNone(eng.books["short"].strat.arm)   # the side that lost the turn rests nothing
        eng.feat.side_hint_15m = None; eng.follow_step(5); self.assertEqual(eng.active, "long")     # None keeps the side
        self.assertEqual(eng.active_s, {"long": 3, "short": 2})

class Capture(unittest.TestCase):
    def test_a_book_that_holds_only_the_down_legs_captures_no_up(self):
        rows = [dict(ts=(1_700_000_000 + i * 60) * 1000, o=0, h=0, l=0, c=100 + (i if i < 60 else 120 - i), v=1) for i in range(120)]   # up 60 min, down 60 min
        for r in rows: r["h"], r["l"], r["o"] = r["c"] + 0.05, r["c"] - 0.05, r["c"]
        tl = [(1_700_000_000 + 60 * 60, 70.0), (1_700_000_000 + 119 * 60, 0.0)]                # held during the down leg only
        c = capture(rows, tl, 3.0)
        self.assertLess(c["up_held"], 0.05); self.assertGreater(c["dn_held"], 0.9); self.assertAlmostEqual(c["in_mkt"], 0.5, 1)
        self.assertTrue(held_at(tl, 1_700_000_000 + 90 * 60)); self.assertFalse(held_at(tl, 1_700_000_000 + 30 * 60))

class SymbolAttribution(unittest.TestCase):
    """엔진이 여럿이면 events.jsonl 에 심볼이 섞인다 — 심볼(과 방향)로 가르지 않는 분석 도구는 다른 책의 장부를 재게 된다.
    symbol 태그가 없던 시절(2026-09-01 이전)의 이벤트는 직전 START 의 엔진 것이다(그때는 엔진이 하나였다)."""
    LINES = [
        '{"t": "2026-08-31 10:00:00", "ev": "START", "symbol": "TRUMPUSDT", "side": "long", "tick": 0.001, "qstep": 0.1, "lots": []}',
        '{"t": "2026-08-31 10:01:00", "ev": "FILL", "side": "long", "role": "buy", "qty": 70, "px": 3.0, "pnl": 0.0, "oid": "cycL-b1", "pos_qty": 70}',
        '{"t": "2026-08-31 10:02:00", "ev": "SIZING", "side": "long", "unit_qty": 70, "cap_usdt": 20}',
        '{"t": "2026-09-01 20:00:00", "ev": "START", "symbol": "ZECUSDT", "sides": ["long", "short"], "tick": 0.01, "qstep": 0.001, "books": {"long": {"lots": []}, "short": {"lots": []}}}',
        '{"t": "2026-09-01 20:01:00", "ev": "SIZING", "symbol": "ZECUSDT", "side": "long", "unit_qty": 0.3, "cap_usdt": 13}',
        '{"t": "2026-09-01 20:02:00", "ev": "FILL", "symbol": "ZECUSDT", "side": "short", "role": "buy", "qty": 0.3, "px": 850.0, "pnl": 0.0, "oid": "cycS-b1", "pos_qty": 0.3}',
        '{"t": "2026-09-01 20:03:00", "ev": "FILL", "symbol": "TRUMPUSDT", "side": "long", "role": "trim", "qty": 70, "px": 3.1, "pnl": 7.0, "oid": "cycL-t1", "pos_qty": 0}',
    ]

    def setUp(self):
        import tempfile, os
        from bot import capture, recon
        self.dir = tempfile.mkdtemp(); self.path = os.path.join(self.dir, "events.jsonl")
        with open(self.path, "w", encoding="utf-8") as f: f.write(chr(10).join(self.LINES) + chr(10))
        self.old = capture.LOGS, recon.LOG
        capture.LOGS, recon.LOG = self.dir, self.path

    def tearDown(self):
        from bot import capture, recon
        capture.LOGS, recon.LOG = self.old

    def test_the_capture_timeline_takes_only_its_own_symbol_and_side(self):
        from bot.capture import timeline
        self.assertEqual([q for _, q in timeline("20260901", "TRUMPUSDT", "long")], [0.0, 70.0, 0.0])   # 태그 없는 옛 체결은 직전 START(TRUMP) 것이다
        self.assertEqual([q for _, q in timeline("20260901", "ZECUSDT", "short")], [0.0, 0.3])          # ZEC 의 short 책만
        self.assertEqual([q for _, q in timeline("20260901", "ZECUSDT", "long")], [0.0])                # 같은 심볼의 다른 방향도 아니다

    def test_recon_sizes_and_qstep_come_from_that_symbols_own_events(self):
        from bot.recon import load_events, sizes, qstep_of, live
        evs = load_events(); z = 10 ** 11
        self.assertEqual(sizes(evs, z, {"long"}, "ZECUSDT")["unit_qty"], 0.3)      # 방향만 키로 쓰면 TRUMP 의 70 이 덮어쓴다
        self.assertEqual(sizes(evs, z, {"long"}, "TRUMPUSDT")["unit_qty"], 70)
        self.assertEqual((qstep_of(evs, z, "ZECUSDT"), qstep_of(evs, z, "TRUMPUSDT")), (0.001, 0.1))   # 백테스트 유닛 양자화
        self.assertEqual(live(evs, 0, z, "TRUMPUSDT")[0], 7.0)                     # 실현손익도 자기 심볼만

class Ledger(unittest.TestCase):
    """bot.cycles reads the engine's lot bookkeeping back from FILL events: LIFO, except a de-risk cut the engine booked against the core
    (FILL.lot == "core"), which reduces the oldest lot first."""
    LINES = [
        '{"t": "2026-09-02 10:00:00", "ev": "START", "symbol": "AUSDT", "sides": ["long"], "tick": 0.001, "qstep": 0.1, "books": {"long": {"lots": []}}}',
        '{"t": "2026-09-02 10:01:00", "ev": "FILL", "symbol": "AUSDT", "side": "long", "role": "buy", "qty": 70, "px": 3.0, "fee": 0.042, "pnl": -0.042, "oid": "cycL-b1", "pos_qty": 70}',
        '{"t": "2026-09-02 10:05:00", "ev": "FILL", "symbol": "AUSDT", "side": "long", "role": "buy", "qty": 70, "px": 2.95, "fee": 0.041, "pnl": -0.041, "oid": "cycL-b2", "pos_qty": 140}',
        '{"t": "2026-09-02 10:09:00", "ev": "FILL", "symbol": "AUSDT", "side": "long", "role": "trim", "qty": 35, "px": 2.93, "fee": 0.02, "pnl": -1.6, "oid": "cycL-t1", "pos_qty": 105, "lot": "core"}',
        '{"t": "2026-09-02 10:20:00", "ev": "FILL", "symbol": "AUSDT", "side": "long", "role": "trim", "qty": 70, "px": 2.96, "fee": 0.041, "pnl": -1.09, "oid": "cycL-t2", "pos_qty": 35}',
    ]

    def setUp(self):
        import tempfile, os
        from bot import cycles
        self.dir = tempfile.mkdtemp(); self.path = os.path.join(self.dir, "events.jsonl")
        with open(self.path, "w", encoding="utf-8") as f: f.write(chr(10).join(self.LINES) + chr(10))
        self.old = cycles.LOG; cycles.LOG = self.path

    def tearDown(self):
        from bot import cycles
        cycles.LOG = self.old

    def test_a_core_cut_reduces_the_first_lot_and_the_unit_cycles_whole(self):
        from bot.cycles import build, geometry_of, geometry
        done, books, orphan, eng = build(since="2026-09-02 00:00")
        self.assertEqual(orphan, 0.0)
        self.assertEqual([(c["depth"], c["qty"], c["exit"]) for c in done], [(2, 70.0, 2.96)])      # the unit (lot 2) closed whole at 2.96 ...
        self.assertAlmostEqual(sum(l["qty"] for l in books[("AUSDT", "long")]), 35.0)              # ... and 35 of the core lot remain (LIFO would have eaten the unit first)
        self.assertEqual(geometry_of(done)["n"], 1); self.assertIsNone(geometry(since="2026-09-02 00:00", min_n=2))   # too few cycles: the constants stand

class LedgerWindow(unittest.TestCase):
    """bot.cycles.build(until=...): a report for a past day must not read the cycles that came after it (bot.phases' ledger attributed
    every later cycle to the timeline's last phase)."""
    LINES = Ledger.LINES

    def setUp(self): Ledger.setUp(self)
    def tearDown(self): Ledger.tearDown(self)

    def test_until_cuts_the_events_at_the_report_boundary(self):
        from bot import cycles
        self.assertEqual(len(cycles.build(since="2026-09-02 00:00:00", sym="AUSDT")[0]), 1)                                  # the campaign closes at 10:20
        self.assertEqual(cycles.build(since="2026-09-02 00:00:00", until="2026-09-02 10:10:00", sym="AUSDT")[0], [])         # cut before the closing trim: nothing completed

class Slippage(unittest.TestCase):
    """bot.slip joins each fill to the mid its order saw at arrival (PLACE / TAKER .mid) by clientOid; cost is signed by the trade's direction."""
    LINES = [
        '{"t": "2026-09-02 10:00:00", "ev": "START", "symbol": "AUSDT", "sides": ["long"], "tick": 0.001, "qstep": 0.1}',
        '{"t": "2026-09-02 10:01:00", "ev": "PLACE", "symbol": "AUSDT", "side": "long", "role": "buy", "px": 2.999, "qty": 70, "oid": "cycL-b1", "queue": 5.0, "mid": 3.0}',
        '{"t": "2026-09-02 10:01:03", "ev": "FILL", "symbol": "AUSDT", "side": "long", "role": "buy", "qty": 70, "px": 2.999, "fee": 0.042, "pnl": -0.042, "scope": "maker", "oid": "cycL-b1", "mid": 2.997}',
        '{"t": "2026-09-02 10:05:00", "ev": "TAKER", "symbol": "AUSDT", "side": "long", "qty": 70, "oid": "cycL-m1", "mid": 3.02}',
        '{"t": "2026-09-02 10:05:01", "ev": "FILL", "symbol": "AUSDT", "side": "long", "role": "trim", "qty": 70, "px": 3.018, "fee": 0.127, "pnl": 1.2, "scope": "taker", "oid": "cycL-m1", "mid": 3.019}',
        '{"t": "2026-09-02 10:09:00", "ev": "FILL", "symbol": "AUSDT", "side": "long", "role": "trim", "qty": 70, "px": 3.03, "fee": 0.04, "pnl": 2.0, "scope": "maker", "oid": "cycL-t9"}',
    ]

    def test_cost_is_signed_by_direction_and_fills_without_an_arrival_are_counted_not_guessed(self):
        import tempfile, os
        from bot import slip
        d = tempfile.mkdtemp(); path = os.path.join(d, "events.jsonl")
        with open(path, "w", encoding="utf-8") as f: f.write(chr(10).join(self.LINES) + chr(10))
        old = slip.LOG; slip.LOG = path
        try: rs, missing = slip.rows()
        finally: slip.LOG = old
        self.assertEqual(missing, 1); self.assertEqual(len(rs), 2)
        text = slip.summarize(rs)
        self.assertIn("maker  buy", text); self.assertIn("taker  trim", text)
        maker = [l for l in text.splitlines() if "maker" in l][0].split(); taker = [l for l in text.splitlines() if "taker" in l][0].split()
        self.assertAlmostEqual(float(maker[4]), -3.33, places=1)     # bought at 2.999 vs arrival 3.0: -3.3 bp (the touch is half a spread better than the mid)
        self.assertAlmostEqual(float(maker[6]), -10.0, places=1)     # the mid fell to 2.997 by the fill: adverse drift for a buyer
        self.assertAlmostEqual(float(taker[4]), 6.62, places=1)      # sold at 3.018 vs arrival 3.02: paid 6.6 bp

class Pairing(unittest.TestCase):
    """bot.pair: the day's scan medians per symbol beside the live cycles closed that day."""
    def test_estimator_and_live_join_on_symbol_and_utc_day(self):
        import tempfile, os, json
        from bot import pair
        d = tempfile.mkdtemp(); path = os.path.join(d, "scan-history.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for t, pu in (("2026-09-02 09:00:00", 0.6), ("2026-09-02 13:00:00", 0.7), ("2026-09-02 17:00:00", 0.8)):   # KST: all on UTC 2026-09-02
                f.write(json.dumps(dict(t=t, rows=[dict(symbol="AUSDT", p_up=pu, trials_h=2.0, edge=0.1, impact=0.002, entry=True)])) + chr(10))
        est = pair.estimator(path=path)
        self.assertEqual(est[("AUSDT", "20260902")]["p_up"], 0.7); self.assertEqual(est[("AUSDT", "20260902")]["scans"], 3)
        done = [dict(symbol="AUSDT", t0="2026-09-02 12:00:00", t1="2026-09-02 14:00:00", qty=70, entry=3.0, gross=0.5, net=0.4),
                dict(symbol="AUSDT", t0="2026-09-02 14:00:00", t1="2026-09-02 18:00:00", qty=70, entry=3.0, gross=-0.3, net=-0.35)]
        lv = pair.live(done)
        self.assertEqual(lv[("AUSDT", "20260902")]["n"], 2); self.assertAlmostEqual(lv[("AUSDT", "20260902")]["cyc_h"], 2 / 6); self.assertEqual(lv[("AUSDT", "20260902")]["win"], 0.5)
        self.assertIn("AUSDT", pair.table(est, lv))

class BacktestSizing(unittest.TestCase):
    def test_equity_freezes_the_live_sizes_and_switches_the_fractions_off(self):
        from bot.backtest import size_from_equity
        s = size_from_equity(dict(unit_frac=1.5, cap_frac=0.15, daily_loss_frac=0.3, notional_frac=6.5, wallet_frac=0.25, unit_qty=70, cap_usdt=20), 700.0, 82.3, 0.01)
        self.assertAlmostEqual(s["unit_qty"], 3.19, 6); self.assertAlmostEqual(s["cap_usdt"], 26.25, 6); self.assertAlmostEqual(s["daily_loss_limit"], 52.5, 6)
        self.assertEqual((s["unit_frac"], s["cap_frac"], s["daily_loss_frac"], s["notional_frac"]), (0.0, 0.0, 0.0, 0.0))   # the engine cannot resize offline
        self.assertEqual(size_from_equity(dict(unit_qty=70, cap_usdt=20), 700.0, 82.3, 0.01)["unit_qty"], 70)               # no fractions: the file's fixed sizes

    def test_sizing_and_seeding_use_the_first_file_that_carries_the_symbol(self):
        """A warm-up file recorded before the symbol joined the recording is empty for it: the live sizing must still come from equity
        (2026-09-01 nightly: ETH/XAG ran at the file's unit 70 = $170k notional, every signal blocked by max_notional, zero cycles)."""
        from bot import backtest
        secs = [[7200 + i, 100.0, 100.02, [[100.0, 5.0]], [[100.02, 5.0]], None, [], []] for i in range(5)]
        old = backtest.load_seconds, backtest.seed_history, backtest.latest_equity, backtest.contract_meta, backtest.load_params
        seeded = []
        backtest.load_seconds = lambda path, sym: [] if path == "warm" else secs
        backtest.seed_history = lambda sym, start: seeded.append(start) or (None, None, None)
        backtest.latest_equity = lambda: 700.0; backtest.contract_meta = lambda sym: dict(qstep=0.01); backtest.load_params = lambda: {}
        try: m = backtest.run_files(["warm", "day"], "TESTUSDT", strat=dict(unit_frac=1.5, wallet_frac=0.25, max_notional=1e9), sides=["long"])
        finally: backtest.load_seconds, backtest.seed_history, backtest.latest_equity, backtest.contract_meta, backtest.load_params = old
        self.assertAlmostEqual(m["sizing"]["unit_qty"], 2.62, 6)                           # 700 x 0.25 x 1.5 / 100.01, not the file's 70
        self.assertEqual(seeded, [7200])                                                   # seeded from the first second that exists

    def test_the_contract_step_is_read_from_the_cache(self):
        import json, os
        from bot import backtest
        cp = os.path.join(backtest.CACHE, "contract-TESTQUSDT.json")
        os.makedirs(backtest.CACHE, exist_ok=True)
        with open(cp, "w", encoding="utf-8") as f: json.dump(dict(qstep=0.001, tick=0.01), f)
        try: self.assertEqual(backtest.contract_meta("TESTQUSDT")["qstep"], 0.001)
        finally: os.remove(cp)

class SupervisorJob(unittest.TestCase):
    """bot.supervise puts every child in a Windows job object that dies with the supervisor (2026-09-03: a recorder orphaned by a
    Stop-Process on its supervisor wrote duplicate tapes for 4.5 h next to the new recorder)."""
    @unittest.skipUnless(os.name == "nt", "Windows job objects")
    def test_a_child_dies_when_the_supervisor_job_handle_closes(self):
        import ctypes, subprocess, sys, time
        from bot import supervise
        job = supervise.job_object(); self.assertTrue(job)
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.assertTrue(supervise.assign(job, p)); time.sleep(0.2); self.assertIsNone(p.poll())
            ctypes.windll.kernel32.CloseHandle.argtypes = [ctypes.c_void_p]; ctypes.windll.kernel32.CloseHandle(job)   # the supervisor dying closes its last handle
            for _ in range(50):
                if p.poll() is not None: break
                time.sleep(0.1)
            self.assertIsNotNone(p.poll())
        finally:
            if p.poll() is None: p.kill()

class SelectorCounterfactual(unittest.TestCase):
    """bot/campaigns.py walks an entry forward and asks which came first, the stop or an exit. p from this decides the sign of
    the month's geometric growth (NEXT 14/18), so the ordering and the horizon have to be exact."""
    def scans(self, prices, n=8):
        import datetime as dt
        from bot.hunt import HUNT
        base = dt.datetime(2026, 9, 4, 0, 0)
        row = lambda px, **kw: dict(dict(symbol="A", px=px, qv=4e7, ratio=10.0, twoway24=30.0, atr_pct=0.6, off=5.0, off_close=5.0,
                                        high48=200.0, fund=0.0, dead=False, phase="markup", side="long", qv_shape=4e7), **kw)
        return [(base + dt.timedelta(minutes=10 * i), {"A": row(px)}) for i, px in enumerate(prices)], HUNT

    def test_the_stop_is_read_before_any_exit_on_the_same_scan(self):
        from bot.campaigns import campaign
        SC, cfg = self.scans([100.0, 95.0, 80.0])          # -20% on scan 2, and that scan also reads `dead`
        SC[2][1]["A"]["dead"] = True
        k, mv, h, fu, why = campaign(SC, "A", 0, "long", cfg, 10.0, 12.0)
        self.assertEqual((k, why), ("stop", "stop")); self.assertAlmostEqual(mv, -20.0, places=6)

    def test_an_exit_ends_it_when_no_stop_is_reached_and_the_horizon_returns_nothing(self):
        from bot.campaigns import campaign
        SC, cfg = self.scans([100.0, 101.0, 102.0])
        SC[2][1]["A"]["dead"] = True
        self.assertEqual(campaign(SC, "A", 0, "long", cfg, 10.0, 12.0)[4], "dead")
        SC2, _ = self.scans([100.0, 101.0, 102.0])         # nothing fires: not finished, not counted
        self.assertIsNone(campaign(SC2, "A", 0, "long", cfg, 10.0, 12.0))
        self.assertIsNone(campaign(SC2, "A", 0, "long", cfg, 10.0, 0.05))   # horizon shorter than one scan

    def test_the_stop_distance_follows_the_live_geometry_per_entry_row(self):
        """A fixed -10% stop measured a coin whose live stop sits 30 x ATR1m = 40% away as if it were four times tighter (audit 2026-09-04:
        the >= 1.30 ATR bucket read p 21% that way). The default is the live geometry: max(cap_frac/unit_frac, cap_min_atr x ATR1m)."""
        from bot.campaigns import stop_pct
        prof = dict(cap_frac=0.1, unit_frac=1.5, cap_min_atr=15, cap_per_unit=1)
        self.assertAlmostEqual(stop_pct(dict(atr_pct=0.30), prof), 100 / 15, 6)      # 15 x 0.30% = 4.5% sits under the 6.7% money distance: the money binds
        self.assertAlmostEqual(stop_pct(dict(atr_pct=1.30), prof), 19.5, 6)          # 15 x 1.30%: the ATR floor binds
        self.assertEqual(stop_pct(dict(atr_pct=1.30), prof, 10.0), 10.0)             # --stop N stays a fixed number
        self.assertEqual(stop_pct(dict(atr_pct=None), dict(cap_frac=0, unit_frac=0)), 10.0)   # no geometry at all: the old default

if __name__ == "__main__":
    unittest.main()
