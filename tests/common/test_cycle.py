"""OMS invariants of common/cycle.py (Book against a stub exchange).  python -m unittest tests.common.test_cycle -v"""
import asyncio, json, os, shutil, tempfile, time, unittest
from types import SimpleNamespace
from common.bitget import BitgetError
from common.signal import Features, STRAT, book_params, pos_stats
from common.ws import load_params
from common import cycle
from common.cycle import Book, valid_params, quantize_unit

class FakeREST:
    """Records calls; answers like Bitget."""
    def __init__(self): self.calls = []; self.plans = []; self.pend = []; self.pos = []; self.status = "cancelled"; self.market_raise = None; self.margin_mode = "isolated"
    def cancel_order(self, symbol, order_id=None, client_oid=None): self.calls.append(("cancel", order_id or client_oid)); return {}
    def order_detail(self, symbol, order_id=None, client_oid=None): self.calls.append(("detail", order_id or client_oid)); return dict(status=self.status)
    def market_order(self, symbol, side, size, trade_side="open", client_oid=None, **kw):
        self.calls.append(("market", side, size, trade_side))
        if self.market_raise: e, self.market_raise = self.market_raise, None; raise e
        return dict(orderId="M1")
    def limit_order(self, symbol, side, price, size, **kw): self.calls.append(("limit", side, price, size)); return dict(orderId="L%d" % len(self.calls))
    def pending_plan_orders(self, symbol): return dict(entrustedList=list(self.plans))
    def place_pos_tpsl(self, symbol, hold_side, sl=None, tp=None): self.calls.append(("pos_tpsl", sl)); self.plans.append(dict(planType="pos_loss", posSide=hold_side, triggerPrice=sl, orderId="P-new")); return dict(pos_loss=dict(orderId="P-new"))
    def modify_pos_tpsl(self, symbol, order_id, trigger, hold_side): self.calls.append(("modify", order_id, trigger)); return {}
    def pending_orders(self, symbol): return dict(entrustedList=list(self.pend))
    def positions(self): return list(self.pos)

class StubCy:
    def __init__(self, mode="live"):
        self.sp = {**STRAT, "symbol": "TESTUSDT"}; self.px_tick, self.qstep, self.pp, self.vp = 0.001, 0.1, 3, 1
        self.sides, self.symbol, self.mode = ["long"], "TESTUSDT", mode
        self.feat = Features(); self.feat.f = dict(t=100, mid=3.0, bid=2.999, ask=3.001, atr=0.02, atr15=0.02, mark=3.0, brk=False, bko=False)   # Strategy.step 이 대괄호로 읽는 키까지
        self.maker, self.taker, self.acct = 0.0002, 0.0006, dict(avail=100.0, equity=100.0, upl_all=0.0)
        self.events, self.b, self.day = [], FakeREST(), time.strftime("%Y-%m-%d", time.gmtime())
        self.prv = SimpleNamespace(connected=True)
    def ev(self, kind, **kw): self.events.append((kind, kw))
    def err(self, where, e): self.events.append(("ERR", dict(where=where, msg=str(e))))
    def fpx(self, x): return f"{x:.3f}"
    def fq(self, q): return f"{round(q / 0.1) * 0.1:.1f}"
    def live_ok(self): return True
    async def rest(self, fn, *a, **kw): return fn(*a, **kw)

def book(mode="live", lots=(), stop=None):
    Book.acquire_lock = lambda self: None                 # no lock files from tests
    cy = StubCy(mode); bk = Book(cy, "long")
    bk.pos["lots"] = [list(l) for l in lots]; bk.pos["avg"] = lots[-1][1] if lots else None
    bk.stop = stop
    return cy, bk

def kinds(cy): return [k for k, _ in cy.events]

class LeverageSet(unittest.TestCase):
    """The engine sets params `lever` on its symbol, only while every book is flat: the leverage is the margin each unit locks (the margin
    gate's brake), never a size — a symbol that joins the basket arrives with the exchange's default (HYPEUSDT at 20x, 2026-09-02)."""
    def _cy(self, lots=(), lever=10, acct_lever=20.0, mode="crossed", want_mode="crossed"):
        from common.cycle import Cycle
        cy, bk = book(lots=lots); cy.books = {"long": bk}; cy.sp["lever"] = lever; cy.sp["margin_mode"] = want_mode; cy.lever_t = 0.0
        cy.b.margin_mode = mode
        cy.b.account = lambda symbol: dict(marginMode=mode, crossedMarginLeverage=str(acct_lever), isolatedLongLever=str(acct_lever), isolatedShortLever=str(acct_lever), available="100")
        cy.b.set_leverage = lambda symbol, lev, hold_side=None: cy.b.calls.append(("lever", lev, hold_side)) or {}
        cy.b.set_margin_mode = lambda symbol, m: cy.b.calls.append(("margin_mode", m)) or {}
        return cy, bk, Cycle.refresh_lever

    def test_an_isolated_symbol_is_switched_to_the_contract_mode_while_flat_and_alerted_while_positioned(self):
        cy, bk, refresh = self._cy(mode="isolated")                                       # joined the basket isolated at 20x (HYPE, 2026-09-02)
        asyncio.run(refresh(cy))
        self.assertEqual([c for c in cy.b.calls if c[0] in ("margin_mode", "lever")], [("margin_mode", "crossed"), ("lever", 10, None)])   # mode first, then the crossed leverage
        self.assertIn("MARGIN_MODE_SET", kinds(cy)); self.assertIn("LEVER_SET", kinds(cy)); self.assertEqual(cy.b.margin_mode, "crossed")
        cy, bk, refresh = self._cy(lots=[[70, 3.0, "a"]], mode="isolated")
        asyncio.run(refresh(cy))
        self.assertEqual([c for c in cy.b.calls if c[0] in ("margin_mode", "lever")], []); self.assertIn("MARGIN_MODE_MISMATCH", kinds(cy))   # positioned: the exchange would refuse; alert only
        asyncio.run(refresh(cy)); self.assertEqual(kinds(cy).count("MARGIN_MODE_MISMATCH"), 1)                                                # once an hour, not every 5 minutes

    def test_a_flat_book_is_brought_to_the_configured_leverage(self):
        cy, bk, refresh = self._cy()
        asyncio.run(refresh(cy))
        self.assertEqual([c for c in cy.b.calls if c[0] == "lever"], [("lever", 10, None)]); self.assertIn("LEVER_SET", kinds(cy)); self.assertEqual(bk.lever, 10)

    def test_a_contract_that_caps_under_the_profile_runs_at_its_max_instead_of_erroring_every_five_minutes(self):
        cy, bk, refresh = self._cy(lever=20, acct_lever=10.0); cy.max_lever = 10.0        # BRUSDT: max 10, profile 20 (2026-09-04 19:14, 40797 every 5 min)
        asyncio.run(refresh(cy))
        self.assertEqual([c for c in cy.b.calls if c[0] == "lever"], []); self.assertIn("LEVER_CLAMP", kinds(cy)); self.assertEqual(bk.lever, 10.0)
        asyncio.run(refresh(cy)); self.assertEqual(kinds(cy).count("LEVER_CLAMP"), 1)     # said once
        cy2, bk2, refresh2 = self._cy(lever=20, acct_lever=10.0); cy2.max_lever = 50.0
        asyncio.run(refresh2(cy2)); self.assertEqual([c for c in cy2.b.calls if c[0] == "lever"], [("lever", 20, None)])   # room for the profile: set as before

    def test_a_positioned_book_is_left_alone_and_lever_0_means_hands_off(self):
        cy, bk, refresh = self._cy(lots=[[70, 3.0, "a"]])
        asyncio.run(refresh(cy))
        self.assertEqual([c for c in cy.b.calls if c[0] == "lever"], []); self.assertNotIn("LEVER_SET", kinds(cy)); self.assertEqual(bk.lever, 20.0)   # read, not set
        cy, bk, refresh = self._cy(lever=0)
        asyncio.run(refresh(cy)); self.assertEqual([c for c in cy.b.calls if c[0] == "lever"], [])
        cy, bk, refresh = self._cy(mode="isolated", want_mode="isolated")
        asyncio.run(refresh(cy)); self.assertEqual([c for c in cy.b.calls if c[0] == "lever"], [("lever", 10, "long")])   # isolated by contract: leverage per side

class StopFills(unittest.TestCase):
    def test_stop_fill_is_recognised_by_its_plan_order_identity_before_any_algo_push(self):
        cy, bk = book(lots=[[70, 3.0, "a"]], stop=dict(px=2.9, order_id="PLAN1"))
        asyncio.run(bk.on_private_fill(dict(clientOid="PLAN1", orderId="X1", tradeSide="close", side="sell"), 70, 2.89, 0.12))
        self.assertEqual(bk.pos["lots"], []); self.assertEqual(bk.stops_today, 1)
        self.assertIn("STOP_HIT", kinds(cy)); self.assertNotIn("EXTERNAL_FILL", kinds(cy)); self.assertIsNone(bk.pos["halt"])

    def test_one_stop_order_split_into_fills_counts_once(self):
        cy, bk = book(lots=[[70, 3.0, "a"]], stop=dict(px=2.9, order_id="PLAN1"))
        for q in (35, 35): asyncio.run(bk.on_private_fill(dict(clientOid="PLAN1", tradeSide="close", side="sell"), q, 2.89, 0.06))
        self.assertEqual(bk.pos["lots"], []); self.assertEqual(bk.stops_today, 1)
        self.assertEqual([k for k in kinds(cy) if k == "STOP_HIT"], ["STOP_HIT", "STOP_HIT"])

    def test_unknown_close_fill_waits_for_the_algo_push_then_books_a_stop(self):
        cy, bk = book(lots=[[70, 3.0, "a"]], stop=dict(px=2.9, order_id="PLAN1"))
        bk.stop_trig_t = 0.0
        asyncio.run(bk.on_private_fill(dict(clientOid="PLAN9", tradeSide="close", side="sell"), 70, 2.89, 0.12))
        self.assertEqual(kinds(cy)[-1], "CLOSE_FILL_PENDING"); self.assertEqual(len(bk.pos["lots"]), 1); self.assertIsNone(bk.pos["halt"])
        bk.on_position(dict(total="0", openPriceAvg="3.0", unrealizedPL="0", markPrice="2.89"))       # the size gap does not start the external-fill clock
        self.assertIsNone(bk.mismatch_since)
        asyncio.run(bk.on_algo(dict(planType="psl", status="executing", orderId="PLAN9", triggerPrice="2.9")))
        self.assertEqual(bk.pos["lots"], []); self.assertEqual(bk.stops_today, 1); self.assertIn("STOP_HIT", kinds(cy))

    def test_an_unclassified_close_fill_freezes_new_entries_until_it_is_named(self):
        """분류 대기 15초 안에 담기가 체결되면, 뒤늦게 손절로 판정된 수량이 LIFO 로 그 새 로트를 지우고 옛 로트를 남긴다 —
        거래소는 새 진입가를 들고 장부는 옛 진입가를 든 채 수량만 같아 불일치 감시도 못 잡는다. 그래서 분류될 때까지 담지 않는다."""
        cy, bk = book(lots=[[70, 3.0, "a"]], stop=dict(px=2.9, order_id="PLAN1"))
        bk.work["buy"] = dict(oid="cycL-b1", order_id="L2", px=2.95, qty=70, filled=0.0, t=time.time())
        asyncio.run(bk.on_private_fill(dict(clientOid="PLAN9", tradeSide="close", side="sell"), 70, 2.89, 0.12))
        self.assertEqual(kinds(cy)[kinds(cy).index("CLOSE_FILL_PENDING")], "CLOSE_FILL_PENDING")
        self.assertIn(("cancel", "L2"), cy.b.calls); self.assertIsNone(bk.strat.arm)       # 대기 담기는 지금 거둔다
        asyncio.run(bk.tick([]))
        self.assertTrue(bk.pos["pause"])                                                   # 그리고 분류될 때까지 PAUSE 다
        bk.strat.arm = (10 ** 9, 3.0, 70)
        self.assertIsNone(bk.strat.step(cy.feat.f, [], bk.pos, {})["buy"])                 # PAUSE 면 arm 이 서 있어도 담기 주문은 없다
        asyncio.run(bk.on_algo(dict(planType="psl", status="executing", orderId="PLAN9", triggerPrice="2.9")))
        self.assertEqual(bk.unmatched_close, [])                                          # 이름이 붙으면 풀린다(다음 tick 이 pause 를 되돌린다)

class SettlingFills(unittest.TestCase):
    """2026-09-03 16:38 EGLD: the orders channel said 'filled' (acc 48.3) a second before the fill channel delivered 22.1 and 18.9; the book had
    7.3 booked, re-ordered the 'remainder' 41.0, and both filled -> 89.3 contracts for a 48.3 unit. A finished order whose counted fills are not
    yet booked holds its slot (settling) until they are."""
    def test_a_filled_order_whose_fills_are_in_flight_holds_the_slot_until_they_are_booked(self):
        cy, bk = book(); cy.qstep, cy.vp = 0.1, 1
        async def quiet(sigs): pass
        bk.tick = quiet                                                                       # the OMS alone: no strategy tick after fills
        bk.work["buy"] = dict(oid="cycL-b1", order_id="L1", px=5.266, qty=48.3, filled=0.0, t=time.time())
        asyncio.run(bk.on_fill("buy", 7.3, 5.266, 0.0077, "cycL-b1"))                        # the first fill lands
        bk.on_order(dict(clientOid="cycL-b1", orderId="L1", status="filled", accBaseVolume="48.3", price="5.266", size="48.3"))   # the exchange: done, 48.3 counted
        self.assertIsNotNone(bk.work["buy"]); self.assertEqual(bk.work["buy"]["settling"], 48.3)
        d = dict(buy=(5.266, 41.0), trim=None, stop=None, no_stop=False, events=[])
        asyncio.run(bk.reconcile(d))
        self.assertFalse(any(c[0] == "limit" for c in cy.b.calls))                           # no second order for the "remainder"
        asyncio.run(bk.on_fill("buy", 22.1, 5.266, 0.023, "cycL-b1")); asyncio.run(bk.on_fill("buy", 18.9, 5.266, 0.02, "cycL-b1"))
        self.assertIsNone(bk.work["buy"]); self.assertAlmostEqual(pos_stats(bk.pos)[0], 48.3, 6)   # every counted fill booked: the slot is free, one unit
        bk.on_order(dict(clientOid="cycL-b2", orderId="L2", status="filled", accBaseVolume="10", price="5.0", size="10"))   # a finished order we do not track: ignored
        self.assertIsNone(bk.work["buy"])

    def test_a_cancel_that_finds_the_order_gone_holds_the_slot_too(self):
        cy, bk = book(); cy.qstep, cy.vp = 0.1, 1
        bk.work["buy"] = dict(oid="cycL-b1", order_id="L1", px=5.266, qty=48.3, filled=7.3, t=time.time())
        def gone(*a, **k): raise Exception("43001: The order does not exist")
        cy.b.cancel_order = gone
        asyncio.run(bk.cancel("buy"))
        self.assertIsNotNone(bk.work["buy"]); self.assertEqual(bk.work["buy"]["settling"], 48.3); self.assertIsNone(bk.work["buy"]["cancel_pending"])
        d = dict(buy=(5.266, 41.0), trim=None, stop=None, no_stop=False, events=[])
        asyncio.run(bk.reconcile(d)); self.assertFalse(any(c[0] == "limit" for c in cy.b.calls))
        async def quiet(sigs): pass
        bk.tick = quiet
        asyncio.run(bk.on_fill("buy", 41.0, 5.266, 0.04, "cycL-b1")); self.assertIsNone(bk.work["buy"])

class Cancels(unittest.TestCase):
    def test_taker_waits_until_the_maker_cancel_is_confirmed(self):
        cy, bk = book(lots=[[70, 3.0, "a"]])
        bk.work["trim"] = dict(oid="cycL-t1", order_id="L1", px=3.02, qty=70, filled=0.0, t=time.time())
        d = dict(buy=None, trim=(2.999, 70, "taker"), stop=None, no_stop=False, events=[])
        asyncio.run(bk.reconcile(d))
        self.assertIn(("cancel", "L1"), cy.b.calls); self.assertFalse(any(c[0] == "market" for c in cy.b.calls))   # cancel requested, no market order yet
        self.assertTrue(bk.work["trim"]["cancel_pending"])
        bk.on_order(dict(clientOid="cycL-t1", orderId="L1", status="cancelled"))
        asyncio.run(bk.reconcile(d))
        self.assertTrue(any(c[0] == "market" for c in cy.b.calls))

    def test_stop_hit_keeps_resting_orders_tracked_until_the_exchange_confirms(self):
        cy, bk = book(lots=[[70, 3.0, "a"]], stop=dict(px=2.9, order_id="PLAN1"))
        bk.work["buy"] = dict(oid="cycL-b1", order_id="L2", px=2.95, qty=70, filled=0.0, t=time.time())
        asyncio.run(bk.on_private_fill(dict(clientOid="PLAN1", tradeSide="close", side="sell"), 70, 2.89, 0.12))
        self.assertIn(("cancel", "L2"), cy.b.calls); self.assertIsNotNone(bk.work["buy"]); self.assertTrue(bk.work["buy"]["cancel_pending"])

class Stops(unittest.TestCase):
    def test_per_unit_cap_is_used_by_the_restart_fallback_too(self):
        cy, bk = book(lots=[[70, 3.0, "a"], [70, 3.0, "b"], [70, 3.0, "c"]])
        bk.sp.update(unit_qty=70, cap_usdt=20, cap_per_unit=1)
        self.assertAlmostEqual(bk.fallback_stop(), 3.0 - 20 / 70)
        bk.sp["cap_per_unit"] = 0
        self.assertAlmostEqual(bk.fallback_stop(), 3.0 - 20 / 210)

    def test_a_hard_entry_rejection_ends_the_arm_and_does_not_retry_the_stale_decision(self):
        cy, bk = book(); calls = []
        def reject(*a, **kw): calls.append(1); raise BitgetError("40762", "insufficient available margin")
        cy.b.limit_order = reject
        bk.strat.arm = (time.time() + 30, 3.0, 70.0)
        d = dict(buy=(2.999, 70.0), trim=None, stop=None, no_stop=False, events=[])
        asyncio.run(bk.reconcile(d))
        self.assertIsNone(bk.strat.arm); self.assertEqual(bk.strat.arm_filled, 0.0)
        self.assertEqual(calls, [1]); self.assertIn("REJECT", kinds(cy)); self.assertIn("DISARM", kinds(cy))
        asyncio.run(bk.reconcile(d))
        self.assertEqual(calls, [1])

    def test_a_persistent_trim_rejection_is_retried_no_faster_than_five_seconds(self):
        cy, bk = book(lots=[[70, 3.0, "a"]]); calls = []
        def reject(*a, **kw): calls.append(1); raise BitgetError("40762", "order size exceeded")
        cy.b.limit_order = reject
        d = dict(buy=None, trim=(3.1, 10.0, "maker", None), stop=None, no_stop=False, events=[])
        asyncio.run(bk.reconcile(d)); asyncio.run(bk.reconcile(d))
        self.assertEqual(calls, [1])
        bk.place_retry_after["trim"] = 0.0
        asyncio.run(bk.reconcile(d)); self.assertEqual(calls, [1, 1])

    def test_a_same_price_stop_request_after_the_race_sends_no_modify(self):
        """reconcile and ensure_stop both check for a stop before taking the lock; the loser finds one placed meanwhile and must not
        modify it to the price it already has (MAGMA 2026-09-04 14:20:10: one plan id, two STOP_SET)."""
        cy, bk = book(lots=[[70, 3.0, "a"]], stop=dict(px=2.9, order_id="P-old"))
        asyncio.run(bk.set_stop(2.9)); asyncio.run(bk.set_stop(2.9004))
        self.assertNotIn("modify", [c[0] for c in cy.b.calls]); self.assertNotIn("STOP_SET", kinds(cy))
        asyncio.run(bk.set_stop(2.95)); self.assertEqual([c for c in cy.b.calls if c[0] == "modify"], [("modify", "P-old", "2.950")])

    def test_the_preset_is_cancelled_once_when_two_callers_race(self):
        cy, bk = book(lots=[[70, 3.0, "a"]], stop=dict(px=2.9, order_id="P-1")); bk.preset_plan = "S-1"; calls = []
        cy.b.cancel_plan = lambda symbol, pid, kind: calls.append(pid) or {}
        async def slow(fn, *a, **kw): await asyncio.sleep(0.01); return fn(*a, **kw)     # a real REST call yields: both callers are inside at once
        cy.rest = slow
        async def both(): await asyncio.gather(bk.drop_preset(), bk.drop_preset())
        asyncio.run(both())
        self.assertEqual(calls, ["S-1"]); self.assertIsNone(bk.preset_plan); self.assertEqual(kinds(cy).count("PRESET_DROPPED"), 1)

    def test_the_exchange_saying_there_is_no_position_asks_for_a_resync_instead_of_escalating(self):
        """43023: our lots are stale, so there is nothing to protect and nothing to market-close. Counting it as a stop failure
        drove a 3/s STOP_SET_FAIL -> STOP_FAILED loop that HALTed the book (EGLD 2026-09-03 20:30, audit NEXT 17e)."""
        cy, bk = book(lots=[[70, 3.0, "a"]]); cy.resync_due = False
        def gone(*a, **kw): raise BitgetError("43023", "Insufficient position, can not set profit or stop loss")
        cy.b.place_pos_tpsl = gone
        asyncio.run(bk.set_stop(2.9))
        self.assertEqual(bk.stop_fail, 0); self.assertIsNone(bk.stop)
        self.assertTrue(cy.resync_due); self.assertIn("STOP_NO_POSITION", kinds(cy)); self.assertNotIn("STOP_SET_FAIL", kinds(cy))

    def test_the_same_answer_to_a_modify_keeps_the_old_stop_and_counts_no_failure(self):
        cy, bk = book(lots=[[70, 3.0, "a"]], stop=dict(px=2.9, order_id="P-old")); cy.resync_due = False
        def gone(*a, **kw): raise BitgetError("43023", "Insufficient position, can not set profit or stop loss")
        cy.b.modify_pos_tpsl = gone
        asyncio.run(bk.set_stop(2.8))
        self.assertEqual(bk.stop["px"], 2.9); self.assertEqual(getattr(bk, "modify_fail", 0), 0)
        self.assertTrue(cy.resync_due); self.assertIn("STOP_NO_POSITION", kinds(cy)); self.assertNotIn("STOP_MODIFY_FAIL", kinds(cy))

    def test_no_position_to_close_asks_for_a_resync_instead_of_re_submitting(self):
        """22002 on the order path is the same fact as 43023 on the stop path: the side is empty. Re-submitting achieved nothing
        for nine minutes after a hand close (MUBARAK 2026-09-04 04:50-04:58, 205 rejects)."""
        cy, bk = book(lots=[[70, 3.0, "a"]]); cy.resync_due = False
        def gone(*a, **kw): raise BitgetError("22002", "No position to close")
        cy.b.limit_order = gone
        asyncio.run(bk.place("trim", 3.1, 10.0))
        self.assertTrue(cy.resync_due); self.assertIn("STOP_NO_POSITION", kinds(cy)); self.assertNotIn("REJECT", kinds(cy))
        self.assertIsNone(bk.work["trim"])                      # nothing is tracked as working: the order never existed
        cy2, bk2 = book(lots=[[70, 3.0, "a"]]); cy2.resync_due = False
        cy2.b.market_order = gone
        asyncio.run(bk2.taker(10.0))
        self.assertTrue(cy2.resync_due); self.assertNotIn("REJECT", kinds(cy2))
        cy3, bk3 = book(lots=[[70, 3.0, "a"]])                  # any other error still reports a plain rejection
        cy3.b.limit_order = lambda *a, **kw: (_ for _ in ()).throw(BitgetError("40762", "order size exceeded"))
        asyncio.run(bk3.place("trim", 3.1, 10.0)); self.assertIn("REJECT", kinds(cy3))

    def test_a_timed_out_stop_submission_is_looked_up_before_it_counts_as_a_failure(self):
        cy, bk = book(lots=[[70, 3.0, "a"]])
        def boom(*a, **kw): cy.b.plans.append(dict(planType="pos_loss", posSide="long", triggerPrice="2.900", orderId="P-late")); raise TimeoutError("read timed out")
        cy.b.place_pos_tpsl = boom
        asyncio.run(bk.set_stop(2.9))
        self.assertEqual(bk.stop_fail, 0); self.assertEqual(bk.stop["order_id"], "P-late")

    def test_an_existing_pos_loss_is_adopted_instead_of_duplicated(self):
        cy, bk = book(lots=[[70, 3.0, "a"]])
        cy.b.plans.append(dict(planType="pos_loss", posSide="long", triggerPrice="2.900", orderId="P-old"))
        asyncio.run(bk.set_stop(2.9))
        self.assertEqual(bk.stop["order_id"], "P-old"); self.assertFalse(any(c[0] == "pos_tpsl" for c in cy.b.calls))

    def test_external_close_right_after_a_stop_is_not_a_stop_fill(self):
        cy, bk = book(lots=[[70, 3.0, "a"]], stop=dict(px=2.9, order_id="PLAN1"))
        asyncio.run(bk.on_algo(dict(planType="psl", status="executing", orderId="PLAN1", triggerPrice="2.9")))   # our stop just triggered ...
        asyncio.run(bk.on_private_fill(dict(clientOid="MANUAL-X", tradeSide="close", side="sell"), 10, 2.89, 0.02))   # ... and a manual close arrives within the old 60s window
        self.assertEqual(bk.pos["lots"], [[70, 3.0, "a"]]); self.assertEqual(kinds(cy)[-1], "CLOSE_FILL_PENDING"); self.assertEqual(bk.stops_today, 0)

    def test_resync_gives_a_position_without_exchange_stop_one_now(self):
        cy, bk = book(lots=[[70, 3.0, "a"]])
        cy.b.pos = [dict(symbol="TESTUSDT", holdSide="long", total="70", openPriceAvg="3.0", unrealizedPL="0", markPrice="3.0")]
        asyncio.run(bk.resync(initial=True))
        self.assertTrue(any(c[0] == "pos_tpsl" for c in cy.b.calls)); self.assertEqual(bk.stop["order_id"], "P-new")
        self.assertAlmostEqual(bk.stop["px"], 3.0 - STRAT["cap_usdt"] / 70)                    # the money cap: no features needed

    def test_resync_size_gap_gets_the_same_grace_as_a_position_push(self):
        cy, bk = book(lots=[[70, 3.0, "a"]])
        cy.b.pos = [dict(symbol="TESTUSDT", holdSide="long", total="140", openPriceAvg="3.0", unrealizedPL="0", markPrice="3.0")]
        asyncio.run(bk.resync(initial=True))
        self.assertIsNone(bk.pos["halt"]); self.assertIsNotNone(bk.mismatch_since)             # an own fill may still be queued: 10s, then HALT

class Unconfirmed(unittest.TestCase):
    def test_cancel_of_an_unconfirmed_order_waits_for_its_id(self):
        cy, bk = book(lots=[])
        bk.work["buy"] = dict(oid="cycL-b9", order_id=None, px=2.95, qty=70, filled=0.0, t=time.time() - 6, unconfirmed=time.time() - 6)
        asyncio.run(bk.cancel("buy"))
        self.assertIsNotNone(bk.work["buy"]); self.assertTrue(bk.work["buy"]["cancel_wanted"]); self.assertNotIn(("cancel", None), cy.b.calls)
        cy.b.pend = [dict(clientOid="cycL-b9", orderId="L9", price="2.95", size="70", baseVolume="0")]
        asyncio.run(bk.settle_orders())
        self.assertEqual(bk.work["buy"]["order_id"], "L9"); self.assertIn(("cancel", "L9"), cy.b.calls); self.assertTrue(bk.work["buy"]["cancel_pending"])

    def test_a_timed_out_market_order_is_not_repeated(self):
        cy, bk = book(lots=[[70, 3.0, "a"]]); cy.b.market_raise = TimeoutError("read timed out")
        d = dict(buy=None, trim=(2.999, 70, "taker"), stop=None, no_stop=False, events=[])
        asyncio.run(bk.reconcile(d)); asyncio.run(bk.reconcile(d))
        self.assertEqual(sum(1 for c in cy.b.calls if c[0] == "market"), 1); self.assertIsNotNone(bk.market_pending); self.assertIn("TAKER_UNCONFIRMED", kinds(cy))
        bk.market_pending["t"] -= 5; asyncio.run(bk.settle_orders())
        self.assertIsNone(bk.market_pending); self.assertIn("TAKER_SETTLED", kinds(cy))

class LiqGuard(unittest.TestCase):
    def test_a_stop_beyond_the_liquidation_price_is_pulled_up_to_it(self):
        cy, bk = book(lots=[[70, 3.0, "a"]]); bk.lever = 10.0
        d = dict(buy=None, trim=None, stop=2.4, no_stop=False, events=[])              # cap 42 on one unit: -20%, below a 10x isolated liquidation (~-9%)
        asyncio.run(bk.reconcile(d))
        px = [c[1] for c in cy.b.calls if c[0] == "pos_tpsl"][0]
        self.assertAlmostEqual(float(px), 3.0 * (1 - 0.09), 3); self.assertIn("STOP_LIQ_GUARD", kinds(cy)); self.assertAlmostEqual(bk.strat.stop_px, 2.73, 3)
        bk.on_position(dict(total="70", openPriceAvg="3.0", unrealizedPL="0", markPrice="3.0", liquidationPrice="2.76"))
        self.assertAlmostEqual(bk.guard(2.4, 3.0), 2.76 * 1.01, 6)                     # the exchange's own figure once known

    def test_a_crossed_position_with_a_negative_liquidation_sentinel_has_no_guard(self):
        cy = StubCy(); cy.b.margin_mode = "crossed"; bk = Book(cy, "short"); bk.lever = 10.0
        bk.pos["lots"] = [[28.1, 2.544, "a"]]; bk.pos["avg"] = 2.544
        bk.on_position(dict(total="28.1", openPriceAvg="2.544", unrealizedPL="0", markPrice="2.544", liquidationPrice="-8.336"))
        self.assertAlmostEqual(bk.guard(2.773, 2.544), 2.773, 6); self.assertNotIn("STOP_LIQ_GUARD", kinds(cy))   # the sentinel (the whole account backs it) is not a price — 2026-08-30 23:53 it became a −8.25 stop request
        bk.exch["liq"] = None; self.assertAlmostEqual(bk.guard(2.773, 2.544), 2.773, 6)                            # crossed without a figure: the money cap stands, no isolated formula
        cy2, bk2 = book(lots=[[70, 3.0, "a"]]); cy2.b.margin_mode = "crossed"; bk2.lever = 10.0
        self.assertAlmostEqual(bk2.guard(2.4, 3.0), 2.4, 6)
        bk2.on_position(dict(total="70", openPriceAvg="3.0", unrealizedPL="0", markPrice="3.0", liquidationPrice="3.2"))
        self.assertAlmostEqual(bk2.guard(2.4, 3.0), 2.4, 6)                                                       # a figure on the profit side is not this position's liquidation
        bk2.on_position(dict(total="70", openPriceAvg="3.0", unrealizedPL="0", markPrice="3.0", liquidationPrice="2.5"))
        self.assertAlmostEqual(bk2.guard(2.4, 3.0), 2.5 * 1.01, 6)                                                # a real one on the loss side still guards, crossed or not

    def test_a_stop_request_is_never_a_non_positive_price(self):
        cy, bk = book(lots=[[4.3, 3.0, "a"]]); cy.b.margin_mode = "crossed"; bk.lever = 10.0
        self.assertAlmostEqual(bk.guard(-0.156, 3.0), 0.001, 6)                       # last line of defence: whatever computed it, the exchange needs a positive trigger (43011 otherwise)

class Params(unittest.TestCase):
    def test_zero_windows_and_negative_file_keys_are_rejected(self):
        self.assertEqual(valid_params({**STRAT, "daily_loss_limit": -1}, {"vol_hl": 0}), ["daily_loss_limit", "vol_hl"])
        self.assertEqual(valid_params({**STRAT, "daily_loss_limit": 40, "unit_frac": 1.5, "adopt": False, "sides": ["long"], "mode": "dry", "symbol": "TRUMPUSDT"}, {"vol_hl": 300}), [])

    def test_every_equity_scaled_limit_has_a_fraction_and_they_split_alike(self):
        """A fixed cap beside a scaling unit goes stale as the wallet compounds: on 2026-09-01 max_notional 900 (450 per book) fell under
        one resized unit (546) and the book skipped most of its signals. notional_frac = max_units x unit_frac holds one full ladder at
        any wallet size, and both fractions must split per book or the ladder no longer fits."""
        self.assertEqual(valid_params({**STRAT, "notional_frac": 6.0, "symbol": "TRUMPUSDT"}, {}), [])
        bp = book_params({**STRAT, "unit_frac": 1.5, "notional_frac": 6.0, "max_units": 4}, "long", 0.001, 2, 0.1)
        self.assertEqual((bp["unit_frac"], bp["notional_frac"]), (0.75, 3.0))
        for wallet in (150.0, 728.0, 5000.0):
            self.assertAlmostEqual(wallet * bp["notional_frac"], wallet * bp["unit_frac"] * bp["max_units"], 6)

    def test_an_engine_outside_books_is_not_owned_by_anyone(self):
        """books 가 진실이라 그 밖의 계약은 소유자도 지갑 몫도 없다(wallet_frac 이 공통 기본값 1.0 = 지갑 전액으로 사이징).
        엔진은 시작을 거부하고(Cycle.__init__), 감시견은 다시 올리지 않는다(supervise.gone)."""
        from common.ws import outside_books, strat_for
        from common.supervise import gone
        p = dict(strat=dict(symbol="AUSDT"), books={"AUSDT": dict(wallet_frac=0.5), "BUSDT": dict(wallet_frac=0.5)})
        self.assertFalse(outside_books(p, "AUSDT")); self.assertTrue(outside_books(p, "CUSDT"))
        self.assertFalse(outside_books(dict(strat=dict(symbol="AUSDT")), "CUSDT"))     # books 가 없으면 예전 단일 엔진: 판정하지 않는다
        self.assertEqual(strat_for(p, "CUSDT").get("wallet_frac"), 1.0)                # 거부하지 않으면 이 값으로 주문이 나간다
        self.assertTrue(gone("CUSDT") if (load_params() or {}).get("books") and "CUSDT" not in load_params()["books"] else True)

    def test_a_portfolio_gives_each_engine_its_own_strat_and_wallet_share(self):
        """params["books"] 가 있으면 심볼마다 엔진 하나. 공통 strat 위에 그 심볼의 몫만 덮고, 지갑은 wallet_frac 으로 나눈다 —
        안 나누면 두 엔진이 각자 계좌 전액으로 사이징해 노출이 심볼 수만큼 배가 된다 (NEXT 8)."""
        from common.ws import strat_for, portfolio
        p = dict(strat=dict(symbol="A", sides=["long", "short"], unit_frac=1.5, mode="live"),
                 books={"A": dict(wallet_frac=0.6), "B": dict(wallet_frac=0.4, sides=["long"])})
        self.assertEqual(sorted(portfolio(p)), ["A", "B"])
        a, b = strat_for(p, "A"), strat_for(p, "B")
        self.assertEqual((a["symbol"], a["wallet_frac"], a["sides"]), ("A", 0.6, ["long", "short"]))
        self.assertEqual((b["symbol"], b["wallet_frac"], b["sides"]), ("B", 0.4, ["long"]))
        self.assertEqual(a["unit_frac"], b["unit_frac"])                      # 규칙은 공통이다: 심볼마다 다르면 포트폴리오가 아니라 다른 전략이다
        self.assertAlmostEqual(a["wallet_frac"] + b["wallet_frac"], 1.0, 9)
        self.assertEqual(strat_for(dict(strat=dict(symbol="A")))["symbol"], "A")   # books 없으면 지금과 동일
        self.assertEqual(portfolio(dict(strat=dict(symbol="A"))), ["A"])
        self.assertEqual(valid_params({**STRAT, "wallet_frac": 0.5, "symbol": "AUSDT"}, {}), [])
        self.assertEqual(valid_params({**STRAT, "wallet_frac": 0, "symbol": "AUSDT"}, {}), ["wallet_frac"])   # 0 이면 사이즈가 0 이 된다

    def test_a_new_engine_sizes_from_the_wallet_not_from_another_symbols_unit(self):
        """resize 의 +-25% 클램프는 '변화'를 damp 하는 것이라 이전 동적 값이 있을 때만 건다. 파일의 unit_qty 는 심볼별 계약수라
        다른 심볼로 새로 뜬 엔진의 기준이 못 된다 — 2026-09-01 dry: ZECUSDT(841$)가 TRUMP 기준 70 에 걸려 26.25계약(22,084$)."""
        cy, bk = book(); cy.qstep, cy.vp = 0.01, 2          # 운영에선 qstep = 10**-vp 로 항상 짝이다
        cy.acct = dict(equity=720.0, upl_all=0.0, avail=720.0)
        bk.sp = {**bk.sp, "unit_frac": 0.75, "cap_frac": 0.075, "wallet_frac": 0.5, "unit_qty": 35.0}
        bk.feat.f = {"mid": 841.0}; bk.dyn = {}; bk.pos["lots"] = []
        bk.resize()
        self.assertAlmostEqual(bk.dyn["unit_qty"], 0.32, 2)                       # 720 x 0.5 x 0.75 / 841 = 0.321
        prev = bk.dyn["unit_qty"]; bk.feat.f = {"mid": 420.0}; bk.sized_t = 0
        bk.resize()
        self.assertAlmostEqual(bk.dyn["unit_qty"], prev * 1.25, 2)                # 이전 동적 값이 있으면 한 번에 25% 까지만

    def test_an_empty_wallet_sizes_nothing_and_a_floor_unit_is_no_damping_reference(self):
        """2026-09-03: the EGLD engine's first SIZING ran at wallet 0 -> unit pinned to the qstep floor 0.1; every later resize was damped to
        0.1 x 1.25 = 0.125, which quantizes back to 0.1 — stuck forever while the target was 43. Rules: wallet <= 0 writes nothing; a unit
        at the floor is not a reference (no damp); the damp band is at least one qstep wide."""
        cy, bk = book(); cy.qstep, cy.vp = 0.1, 1
        cy.acct = dict(equity=0.0, upl_all=0.0, avail=0.0)
        bk.sp = {**bk.sp, "unit_frac": 4.0, "cap_frac": 0.77, "wallet_frac": 1.0}; bk.feat.f = {"mid": 5.3}; bk.dyn = {}; bk.pos["lots"] = []
        bk.resize(); self.assertEqual(bk.dyn, {})                                          # nothing sized, nothing remembered
        bk.dyn = {"unit_qty": 0.1}; cy.acct = dict(equity=57.55, upl_all=0.0, avail=57.55); bk.sized_t = 0
        bk.resize(); self.assertAlmostEqual(bk.dyn["unit_qty"], round(57.55 * 4.0 / 5.3, 1), 1)   # from the floor straight to the target (43.4): no damp from a floor value
        cy.qstep, cy.vp = 0.01, 2; bk.dyn = {"unit_qty": 0.02}; bk.sp = {**bk.sp, "unit_frac": 1.0, "wallet_frac": 0.01}; bk.feat.f = {"mid": 5.3}; bk.sized_t = 0
        bk.resize(); self.assertAlmostEqual(bk.dyn["unit_qty"], 0.03, 2)                   # a real small unit still moves a whole step (x1.25 of 0.02 would quantize back to 0.02)

    def test_the_money_cap_keeps_a_minimum_atr_distance_by_shrinking_the_unit(self):
        """cap_min_atr(2026-09-03): 돈 한도(cap)는 그대로 두고 유닛을 줄여 1유닛 진입에서 한도까지 >= k ATR 이 되게 한다 — σ 23%/일 코인에선
        4유닛 사다리 아래 2.5% 가 시간당 σ 의 반이라 잡음 손절이었다. 늘리지는 않고, 0 이면 무변화."""
        cy, bk = book(); cy.qstep, cy.vp = 1, 0
        cy.acct = dict(equity=52.0, upl_all=0.0, avail=52.0)
        atr = 0.52 * 0.0059                                                            # UAIUSDT 2026-09-03: mid 0.52, ATR(1m) 0.59%
        bk.sp = {**bk.sp, "unit_frac": 1.5, "cap_frac": 0.15, "wallet_frac": 1.0, "cap_min_atr": 30}
        bk.feat.f = {"mid": 0.52, "atr": atr}; bk.dyn = {}; bk.pos["lots"] = []
        bk.resize()
        self.assertEqual(bk.dyn["unit_qty"], round(7.8 / (30 * atr)))                # 명목 78$ → 한도 7.8$ 가 30 ATR 아래 = 명목 44$ (85계약)
        self.assertEqual(bk.dyn["cap_usdt"], 7.8)                                      # 한도는 돈 그대로
        self.assertEqual(round(7.8 / (85 * atr), 1), cy.events[-1][1]["cap_atr"])      # SIZING 이 1유닛 기준 한도의 ATR 거리를 적는다
        bk.sp["cap_min_atr"] = 0; bk.dyn = {}; bk.sized_t = 0; bk.resize()
        self.assertEqual(bk.dyn["unit_qty"], 150)                                      # 끄면 78 / 0.52
        bk.sp["cap_min_atr"] = 30; bk.feat.f = {"mid": 0.52, "atr": 0.52 * 0.001}; bk.sized_t = 0; bk.resize()
        self.assertEqual(bk.dyn["unit_qty"], 150)                                      # 한도가 이미 멀면(ATR 0.1% → 100 ATR) 늘리지 않는다

    def test_a_lowered_profile_shrinks_the_unit_and_the_limits_under_an_open_position_but_never_the_cap(self):
        """2026-09-03 20:08: hunt.strat was cut 4/0.77/1.0/16 -> 2/0.4/0.6/8 under a 2-unit EGLD long that never went flat, so the 4x unit
        kept adding into the slide. Positioned: reductions of the unit (damped 25% a step), the notional cap and the daily limit apply at
        once; the money cap stays the campaign's; increases wait for flat."""
        cy, bk = book(lots=[[53.6, 5.139, "a"], [26.8, 5.062, "b"]]); cy.qstep, cy.vp = 0.1, 1
        cy.acct = dict(equity=61.03, upl_all=-8.0, avail=40.0)                              # wallet = equity - upl = 69.03
        old = dict(unit_qty=55.2, cap_usdt=53.15, daily_loss_limit=69.03, max_notional=1104.48)
        bk.dyn = dict(old); bk.sp = {**bk.sp, **old, "unit_frac": 2.0, "cap_frac": 0.4, "daily_loss_frac": 0.6, "notional_frac": 8.0, "wallet_frac": 1.0}
        bk.feat.f = {"mid": 5.0}; bk.sized_t = 0
        bk.resize()
        self.assertAlmostEqual(bk.sp["unit_qty"], 41.4, 1)                                   # toward 27.6, one 25% step at a time
        self.assertEqual((bk.sp["daily_loss_limit"], bk.sp["max_notional"]), (41.42, 552.24))
        self.assertEqual(bk.sp["cap_usdt"], 53.15)                                            # the stop distance is not re-derived under a position
        bk.sp.update(unit_frac=4.0, cap_frac=0.77, daily_loss_frac=1.0, notional_frac=16.0); bk.sized_t = 0
        bk.resize()
        self.assertEqual((bk.sp["unit_qty"], bk.sp["cap_usdt"], bk.sp["daily_loss_limit"], bk.sp["max_notional"]), (41.4, 53.15, 41.42, 552.24))   # increases wait for flat
        bk.pos["lots"] = []; bk.pos["avg"] = None; bk.sized_t = 0
        bk.resize()
        self.assertGreater(bk.sp["unit_qty"], 41.4); self.assertEqual((bk.sp["daily_loss_limit"], bk.sp["max_notional"]), (69.03, 1104.48))

    def test_a_volatility_spike_does_not_shrink_the_ladder_under_an_open_position(self):
        """MAGMA 2026-09-04 16:03-16:12: ATR1m x3.5 inside the spike, cap_min_atr cut the unit 494 -> 206 minute by minute, and every stall at
        the top (16:06:16, 16:10:01, 16:12:01) was SKIP max_units. The ladder is the campaign's (user decision): the ATR at the first fill
        measures it until flat; a smaller wallet (or a lowered profile) still shrinks it at once; a restart carries it."""
        cy, bk = book(lots=[[494, 0.2559, "a"]]); cy.qstep, cy.vp = 1, 0
        cy.acct = dict(equity=204.0, upl_all=0.0, avail=200.0)
        atr0 = 20.4 / (15 * 494)                                                                      # the ATR that sized the 494 unit under cap 20.4 / 15 ATR
        bk.dyn = dict(unit_qty=494, cap_usdt=20.4); bk.sp = {**bk.sp, "unit_qty": 494, "cap_usdt": 20.4, "unit_frac": 1.5, "cap_frac": 0.1, "cap_min_atr": 15, "wallet_frac": 1.0}
        bk.campaign_atr = atr0; bk.feat.f = {"mid": 0.281, "atr": atr0 * 3.5}; bk.sized_t = 0
        bk.resize(); self.assertEqual(bk.sp["unit_qty"], 494)                                          # the spike's ATR does not touch the ladder
        cy.acct = dict(equity=150.0, upl_all=0.0, avail=150.0); bk.sized_t = 0
        bk.resize(); self.assertLess(bk.sp["unit_qty"], 494)                                           # a smaller wallet still does (cap 15 over 15 x atr0)
        self.assertEqual(bk.snapshot()["campaign_atr"], atr0)                                          # and a restart reads it back
        cy2, bk2 = book(); bk2.load(dict(mode="live", day=cy2.day, books=dict(long=dict(pos=dict(lots=[[494, 0.2559, "a"]], avg=0.2559, last="buy"), campaign_atr=atr0, sizing={}))))
        self.assertEqual(bk2.campaign_atr, atr0)
        d = dict(buy=None, trim=None, stop=None, no_stop=False, events=[]); bk2.pos["lots"] = []; bk2.pos["avg"] = None
        asyncio.run(bk2.reconcile(d)); self.assertIsNone(bk2.campaign_atr)                             # flat: the next campaign is measured afresh
        bk2.feat.f = {"mid": 0.25, "atr": atr0}
        asyncio.run(bk2.on_fill("buy", 100, 0.25, 0.01, "cycL-b1", "maker")); self.assertEqual(bk2.campaign_atr, atr0)   # frozen at the first fill

    def test_wrong_types_of_file_only_and_optional_keys_are_rejected(self):
        bad = valid_params({**STRAT, "daily_loss_limit": "40", "stop_structural": "oops", "adopt": "false", "symbol": "TRUMPUSDT"}, {})
        self.assertEqual(bad, ["adopt", "daily_loss_limit", "stop_structural"])
        self.assertEqual(valid_params({**STRAT, "symbol": "trump", "mode": "paper", "typo_key": "x"}, {}), ["mode", "symbol", "typo_key"])

    def test_mode_switch_discards_the_other_modes_ledger(self):
        cy, bk = book(mode="live")
        bk.load(dict(mode="dry", day=cy.day, books=dict(long=dict(pos=dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", halt="DAILY_LOSS"), realized=-50, stops_today=2, sizing=dict(unit_qty=90)))))
        self.assertEqual(bk.pos["lots"], []); self.assertEqual(bk.realized, 0.0); self.assertEqual(bk.stops_today, 0); self.assertIsNone(bk.pos["halt"]); self.assertEqual(bk.dyn, {})
        self.assertEqual(kinds(cy)[-1], "STATE_DISCARDED")


class RemovedBook(unittest.TestCase):
    def test_a_book_that_left_params_keeps_its_share_and_only_winds_down(self):
        """select drops a book only when it saw it flat, but a fill can land in between: strat_for then falls back to the common defaults
        (wallet_frac 1.0, wind_down off) and the deferred engine would add again on the whole wallet until flat."""
        cy, bk = book(lots=[[70, 3.0, "a"]]); bk.sp["wallet_frac"] = 0.25
        bk.apply_params({**STRAT, "symbol": "TESTUSDT"}, gone=True)
        self.assertTrue(bk.sp["wind_down"]); self.assertEqual(bk.sp["wallet_frac"], 0.25); self.assertIs(bk.strat.p, bk.sp)
        asyncio.run(bk.tick([])); self.assertTrue(bk.pos["pause"])                                   # no new entry while it waits for flat
        bk.apply_params({**STRAT, "symbol": "TESTUSDT", "wallet_frac": 0.5}); self.assertFalse(bk.sp["wind_down"]); self.assertEqual(bk.sp["wallet_frac"], 0.5)

class LotRouting(unittest.TestCase):
    def test_a_core_cut_reduces_the_core_lot_and_the_ledger_says_so(self):
        cy, bk = book(lots=[[70, 3.0, "a"], [70, 2.95, "b"]]); bk.pos["avg"] = 2.975
        d = dict(buy=None, trim=(3.02, 35, "maker", 0), stop=None, no_stop=False, events=[])
        asyncio.run(bk.reconcile(d)); oid = bk.work["trim"]["oid"]
        self.assertEqual(bk.trim_lot[oid], 0)                                                          # the order remembers the lot its fills reduce
        asyncio.run(bk.on_private_fill(dict(clientOid=oid, tradeSide="close", side="sell", tradeScope="maker"), 35, 3.02, 0.02))
        self.assertEqual(bk.pos["lots"], [[35, 3.0, "a"], [70, 2.95, "b"]])
        fill = [kw for k, kw in cy.events if k == "FILL"][-1]; self.assertEqual(fill["lot"], "core")
        cy2, bk2 = book(lots=[[70, 3.0, "a"], [70, 2.95, "b"]]); bk2.pos["avg"] = 2.975
        asyncio.run(bk2.on_fill("trim", 35, 3.02, 0.02, "cycL-t7", "taker", lot=None))                 # LIFO when no lot is named
        self.assertEqual(bk2.pos["lots"], [[70, 3.0, "a"], [35, 2.95, "b"]])

class CampaignHighWater(unittest.TestCase):
    """The blow-off target sells blowoff_frac of the campaign's LARGEST position. That high-water is Strategy soft state, so a restart
    (this repo restarts positioned engines when the running build has a live defect) would take the REMAINDER as the campaign and rest
    another frac of it at the same price — the defect of f740edd, re-entered through the restart. It is snapshotted and restored."""
    def test_the_high_water_survives_a_restart_while_the_position_does(self):
        cy, bk = book(lots=[[70, 3.0, "a"], [70, 2.9, "b"]]); bk.pos["avg"] = 2.95
        bk.strat.blow_base = 140.0
        self.assertEqual(bk.snapshot()["blow_base"], 140.0)
        snap = bk.snapshot()
        def state(lots): return dict(mode="live", day=cy.day, books={"long": {**snap, "pos": {**snap["pos"], "lots": lots}}})
        cy2, bk2 = book(); bk2.load(state([[70, 3.0, "a"]]))                                 # the target sold half; the engine restarts here
        self.assertEqual(bk2.strat.blow_base, 140.0)                                        # not 70: the campaign's share is already sold
        cy3, bk3 = book(); bk3.load(state([]))
        self.assertIsNone(bk3.strat.blow_base)                                              # a flat book starts a fresh campaign

class StopLock(unittest.TestCase):
    def test_two_concurrent_stop_requests_place_one_pos_loss(self):
        cy, bk = book(lots=[[70, 3.0, "a"]])
        async def rest(fn, *a, **kw): await asyncio.sleep(0); return fn(*a, **kw)                    # a REST call yields, as the executor does
        cy.rest = rest
        async def both(): await asyncio.gather(bk.set_stop(2.9), bk.set_stop(2.9))                 # a fill's reconcile and housekeeping's ensure_stop at once
        asyncio.run(both())
        self.assertEqual(sum(1 for c in cy.b.calls if c[0] == "pos_tpsl"), 1); self.assertEqual(bk.stop["order_id"], "P-new")

class Sizing(unittest.TestCase):
    def test_a_coarse_qstep_unit_does_not_flap_at_the_rounding_boundary(self):
        # ETHUSDT 2026-09-01: 목표 0.0547 에 qstep 0.01 = 5.5 단계. 순수 반올림은 mid 가 조금만 움직여도
        # 0.05 / 0.06 을 매분 오갔다(유닛 20% 진동 + SIZING 알림 폭주).
        self.assertAlmostEqual(quantize_unit(0.0547, None, 0.01), 0.05)          # 첫 사이징: 그냥 반올림
        for tgt in (0.0547, 0.0551, 0.0574):
            self.assertAlmostEqual(quantize_unit(tgt, 0.05, 0.01), 0.05)         # 죽은 구간 안이면 안 움직인다
        self.assertAlmostEqual(quantize_unit(0.0576, 0.05, 0.01), 0.06)          # 3/4 단계를 넘으면 움직인다
        self.assertAlmostEqual(quantize_unit(0.0424, 0.05, 0.01), 0.04)          # 아래쪽도 대칭

    def test_a_fine_qstep_symbol_keeps_its_old_behaviour(self):
        # ZECUSDT: 유닛 0.156 에 qstep 0.001 = 156 단계. 죽은 구간이 유닛의 0.5% 라 기존 2% 기록 문턱보다 촘촘하다.
        self.assertAlmostEqual(quantize_unit(0.1565, 0.156, 0.001), 0.156)
        self.assertAlmostEqual(quantize_unit(0.1571, 0.156, 0.001), 0.157)
        self.assertAlmostEqual(quantize_unit(0.1580, 0.156, 0.001), 0.158)

    def test_the_unit_is_never_smaller_than_one_step(self):
        self.assertAlmostEqual(quantize_unit(0.0004, None, 0.01), 0.01)

if __name__ == "__main__":
    unittest.main()


class CapitalPool(unittest.TestCase):
    """cycle.Pool (2026-09-04): the hunt basket's first-come capital pool — at most `cap` books hold a campaign at once, a claim is a lease,
    a positioned book adopts, the day's loss across every book refuses new campaigns."""
    def setUp(self): self.d = tempfile.mkdtemp(); self.path = os.path.join(self.d, "pool.json")
    def tearDown(self): shutil.rmtree(self.d, ignore_errors=True)
    def pool(self, sym, cap=1, states=None, ev=None):
        p = cycle.Pool(sym, ev=ev, path=self.path, states=states or (lambda: {})); p.cap = cap; return p
    def claims(self):
        with open(self.path, encoding="utf-8") as f: return json.load(f)["claims"]

    def test_the_first_claim_wins_and_the_next_book_waits_for_the_release(self):
        a, b = self.pool("AUSDT"), self.pool("BUSDT")
        self.assertEqual((a.claim(), b.claim(), a.claim()), ("", "pool", ""))            # ours stays ours; the second waits
        a.tend(positioned=True, armed=False); self.assertTrue(a.mine)                      # positioned: kept
        a.tend(positioned=False, armed=True); self.assertTrue(a.mine)                      # an armed entry still holds it
        a.tend(positioned=False, armed=False); self.assertFalse(a.mine)                    # nothing behind it: released
        self.assertEqual(b.claim(), ""); self.assertEqual(set(self.claims()), {"BUSDT"})
        c, d = self.pool("CUSDT", cap=2), self.pool("DUSDT", cap=2)
        self.assertEqual((c.claim(), d.claim()), ("", "pool"))                             # cap 2: room for one more, not two

    def test_a_dead_engines_lease_is_swept_and_a_positioned_book_adopts_whatever_the_cap(self):
        a = self.pool("AUSDT"); a.claim()
        with open(self.path, encoding="utf-8") as f: d = json.load(f)
        d["claims"]["AUSDT"]["t"] -= 400                                                   # older than TTL: its engine is gone
        with open(self.path, "w", encoding="utf-8") as f: json.dump(d, f)
        evs = []; b = self.pool("BUSDT", ev=lambda k, **kw: evs.append(k))
        self.assertEqual(b.claim(), "pool")  # a dead process does not prove a flat exchange position
        b.states = lambda: {"AUSDT": dict(mode="live", ws=dict(prv=True), t=time.strftime("%Y-%m-%d %H:%M:%S"),
                                             books=dict(long=dict(pos=dict(qty=0, lots=[]), exch=dict(total=0),
                                                                  working=dict(buy=None, trim=None), arm=None, market_pending=False)))}
        self.assertEqual(b.claim(), ""); self.assertIn("POOL_STALE", evs); self.assertEqual(set(self.claims()), {"BUSDT"})
        evs2 = []; c = self.pool("CUSDT", ev=lambda k, **kw: evs2.append(k)); c.tend(positioned=True, armed=False)
        self.assertTrue(c.mine); self.assertIn("POOL_ADOPT", evs2); self.assertEqual(set(self.claims()), {"BUSDT", "CUSDT"})   # the position is the fact
        b.refreshed = 0.0; b.tend(positioned=True, armed=False)
        self.assertGreater(self.claims()["BUSDT"]["t"], time.time() - 5)                   # a held lease is refreshed

    def test_the_live_construction_reads_the_engines_state_files_by_default(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder, patch.object(cycle, 'ROOT', folder):
            os.mkdir(os.path.join(folder, 'logs'))
            with open(os.path.join(folder, 'logs', 'events.jsonl'), 'w'): pass
            with patch.object(cycle, 'EVENTS', os.path.join(folder, 'logs', 'events.jsonl')):
                p = cycle.Pool("XUSDT", path=self.path)
            self.assertIs(p.states, cycle.load_states)
            self.assertIsInstance(p.day_loss(), float)

    def test_the_days_loss_across_every_book_refuses_new_campaigns(self):
        day = time.strftime("%Y-%m-%d", time.gmtime())
        states = lambda: {"XUSDT": dict(day=day, books=dict(short=dict(realized=-40.0))), "YUSDT": dict(day=day, books=dict(long=dict(realized=-25.0))),
                          "OLD": dict(day="2000-01-01", books=dict(long=dict(realized=-999.0)))}
        a = self.pool("AUSDT", states=states); a.limit = 60.0
        self.assertEqual(a.day_loss(), -65.0); self.assertEqual(a.claim(), "pool_daily")
        a.limit = 100.0; self.assertEqual(a.claim(), "")

    def test_a_lock_that_cannot_be_taken_refuses_rather_than_trades(self):
        a = self.pool("AUSDT"); a.LOCK_TTL, a.LOCK_WAIT = 999, 0.2
        open(self.path + ".lock", "w").close()                                             # somebody holds the lock and does not let go
        evs = []; a.ev = lambda k, **kw: evs.append(k)
        self.assertEqual(a.claim(), "pool_error"); self.assertIn("POOL_ERROR", evs); self.assertFalse(a.mine)
        os.remove(self.path + ".lock"); self.assertEqual(a.claim(), "")

    def test_the_days_loss_can_leave_the_asking_book_out(self):
        day = time.strftime("%Y-%m-%d", time.gmtime())
        states = lambda: {"XUSDT": dict(day=day, books=dict(short=dict(realized=-40.0))), "AUSDT": dict(day=day, books=dict(long=dict(realized=-5.0)))}
        a = self.pool("AUSDT", states=states)
        self.assertEqual(a.day_loss(), -45.0); self.assertEqual(a.day_loss(exclude="AUSDT"), -40.0)   # the asker adds its own live figure instead of its snapshot
        a._dl["AUSDT"] = (time.time(), -999.0, "2000-01-01")                                          # a fresh cache entry from another UTC day is not reused
        self.assertEqual(a.day_loss(exclude="AUSDT"), -40.0)                                          # (09:00:01 KST 2026-09-05: yesterday's -21 re-halted two books a second after day_close)
        a._dl["AUSDT"] = (time.time(), -999.0, day); self.assertEqual(a.day_loss(exclude="AUSDT"), -999.0)   # same day, under 5 s: the cache answers

class ResumeFile(unittest.TestCase):
    """RESUME lifts every engine's HALT once per file and the file outlives the first reader by RESUME_GRACE_S: with one engine per book, the
    first engine to poll used to delete it before the halted ones saw it (2026-09-05 09:31: FLOCK ate six RESUMEs, DASH / MARSCOIN stayed halted)."""
    def test_every_engine_honours_a_resume_once_and_the_file_survives_the_grace(self):
        from common.cycle import Cycle
        d = tempfile.mkdtemp(); rp = os.path.join(d, "RESUME")
        cy1, bk1 = book(); cy1.books = {"long": bk1}; bk1.pos["halt"] = "DAILY_LOSS"
        cy2, bk2 = book(); cy2.books = {"long": bk2}; bk2.pos["halt"] = "DAILY_LOSS"
        open(rp, "w").close()
        Cycle.consume_resume(cy1, rp)                                                     # the first engine: lifted, file kept (inside the grace)
        self.assertIsNone(bk1.pos["halt"]); self.assertTrue(os.path.exists(rp)); self.assertIn("RESUME", kinds(cy1))
        Cycle.consume_resume(cy1, rp); self.assertEqual(kinds(cy1).count("RESUME"), 2)   # the same file again: not honoured twice (the book-level and the engine-level event once each)
        Cycle.consume_resume(cy2, rp); self.assertIsNone(bk2.pos["halt"])                # the second engine still finds it
        os.utime(rp, (time.time() - 10, time.time() - 10))                                # past the grace: whoever polls next removes it
        cy3, bk3 = book(); cy3.books = {"long": bk3}; bk3.pos["halt"] = "STOP_FAILED"
        Cycle.consume_resume(cy3, rp); self.assertIsNone(bk3.pos["halt"]); self.assertFalse(os.path.exists(rp))   # honoured once by the remover too
        Cycle.consume_resume(cy3, rp); self.assertEqual(kinds(cy3).count("RESUME"), 2)   # no file: nothing
        shutil.rmtree(d, ignore_errors=True)

class BasketDailyBrake(unittest.TestCase):
    """With the pool on, a campaign in flight is braked by the day's realized losses of the OTHER books too: the pool's shield only refuses
    new campaigns, so a book starting the day at 0 could otherwise add a whole ladder after the basket had already lost its daily limit."""
    def test_a_campaign_in_flight_is_halted_by_the_days_losses_of_the_other_books(self):
        cy, bk = book(lots=[[70, 3.0, "a"]]); d = tempfile.mkdtemp(); day = time.strftime("%Y-%m-%d", time.gmtime())
        cy.pool = cycle.Pool("TESTUSDT", path=os.path.join(d, "pool.json"),
                             states=lambda: {"OTHER": dict(day=day, books=dict(short=dict(realized=-50.0))), "TESTUSDT": dict(day=day, books=dict(long=dict(realized=-999.0)))})
        cy.pool.cap = 1; bk.sp["daily_loss_limit"] = 60.0; bk.exch["upl"] = -5.0
        bk.check_daily(cy.feat.f); self.assertIsNone(bk.pos["halt"])                      # -50 + 0 - 5 > -60; its own stale snapshot is not counted twice
        bk.exch["upl"] = -12.0; bk.check_daily(cy.feat.f); self.assertEqual(bk.pos["halt"], "DAILY_LOSS")
        self.assertEqual(next(kw for k, kw in cy.events if k == "DAILY_LOSS")["others"], -50.0)
        cy2, bk2 = book(lots=[[70, 3.0, "a"]]); bk2.sp["daily_loss_limit"] = 60.0; bk2.exch["upl"] = -12.0
        bk2.check_daily(cy2.feat.f); self.assertIsNone(bk2.pos["halt"])                   # no pool: the book alone, as before
        shutil.rmtree(d, ignore_errors=True)

class EmptySide(unittest.TestCase):
    """The exchange answered "no position" (43023 on a stop, 22002 on a reduce): the book's lots are stale. One probe a minute instead of an
    order or a stop at every tick, and the capital pool does not count the phantom as a campaign — CP 2026-09-04 09:41: 280 STOP_NO_POSITION
    in 3.5 minutes until the STOP file, while the phantom lots would have held the pool for every other coin."""
    def test_a_no_position_answer_throttles_stops_and_orders_to_one_probe_a_minute_until_the_exchange_shows_a_position(self):
        cy, bk = book(lots=[[70, 3.0, "a"]]); calls = []
        def refuse(symbol, hold_side, sl=None, tp=None): calls.append(sl); raise BitgetError("43023", "Insufficient position, can not set stop")
        cy.b.place_pos_tpsl = refuse
        asyncio.run(bk.ensure_stop()); self.assertEqual(len(calls), 1); self.assertEqual(kinds(cy).count("STOP_NO_POSITION"), 1); self.assertTrue(bk.phantom)
        bk.stop_try_t = 0.0; asyncio.run(bk.ensure_stop()); self.assertEqual(len(calls), 1)                  # the latch, not the 2-s clock, holds it
        d = dict(buy=None, trim=(3.1, 70.0, "maker", None), stop=2.9, no_stop=False, events=[])
        asyncio.run(bk.reconcile(d)); self.assertEqual(len(calls), 1); self.assertFalse(any(c[0] == "limit" for c in cy.b.calls))   # no stop and no order per tick either
        bk.no_pos_t -= cycle.NO_POS_RETRY_S; bk.stop_try_t = 0.0
        asyncio.run(bk.ensure_stop()); self.assertEqual(len(calls), 2); self.assertEqual(kinds(cy).count("STOP_NO_POSITION"), 2)   # a minute later: one probe, refused again
        bk.on_position(dict(total="70", openPriceAvg="3.0", unrealizedPL="0", markPrice="3.0")); self.assertFalse(bk.phantom)      # the exchange shows the position: normal again
        del cy.b.place_pos_tpsl; bk.stop_try_t = 0.0
        asyncio.run(bk.ensure_stop()); self.assertIsNotNone(bk.stop)
        bk.no_pos_t = time.time(); asyncio.run(bk.on_fill("buy", 70, 3.0, 0.01, "cycL-b2", "maker")); self.assertIsNone(bk.no_pos_t)   # an entry fill clears it too
        cy3, bk3 = book(lots=[[70, 3.0, "a"]]); self.assertFalse(bk3.phantom)                                    # never told otherwise: a campaign as before
