"""Invariants of the evidence tools (bot/replay.py hold-vs-sell, bot/capture.py direction capture).  python -m unittest bot.test_tools"""
import unittest
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

if __name__ == "__main__":
    unittest.main()
