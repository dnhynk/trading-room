"""OMS invariants of bot/cycle.py (Book against a stub exchange).  python -m unittest bot.test_cycle -v"""
import asyncio, time, unittest
from types import SimpleNamespace
from bot.signal import Features, STRAT
from bot import cycle
from bot.cycle import Book, valid_params

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
    def place_pos_tpsl(self, symbol, hold_side, sl=None, tp=None): self.calls.append(("pos_tpsl", sl)); return dict(pos_loss=dict(orderId="P-new"))
    def pending_orders(self, symbol): return dict(entrustedList=list(self.pend))
    def positions(self): return list(self.pos)

class StubCy:
    def __init__(self, mode="live"):
        self.sp = {**STRAT, "symbol": "TESTUSDT"}; self.px_tick, self.qstep, self.pp, self.vp = 0.001, 0.1, 3, 1
        self.sides, self.symbol, self.mode = ["long"], "TESTUSDT", mode
        self.feat = Features(); self.feat.f = dict(t=100, mid=3.0, bid=2.999, ask=3.001, atr=0.02, mark=3.0)
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

    def test_wrong_types_of_file_only_and_optional_keys_are_rejected(self):
        bad = valid_params({**STRAT, "daily_loss_limit": "40", "stop_structural": "oops", "adopt": "false", "symbol": "TRUMPUSDT"}, {})
        self.assertEqual(bad, ["adopt", "daily_loss_limit", "stop_structural"])
        self.assertEqual(valid_params({**STRAT, "symbol": "trump", "mode": "paper", "typo_key": "x"}, {}), ["mode", "symbol", "typo_key"])

    def test_mode_switch_discards_the_other_modes_ledger(self):
        cy, bk = book(mode="live")
        bk.load(dict(mode="dry", day=cy.day, books=dict(long=dict(pos=dict(lots=[[70, 3.0, "a"]], avg=3.0, last="buy", halt="DAILY_LOSS"), realized=-50, stops_today=2, sizing=dict(unit_qty=90)))))
        self.assertEqual(bk.pos["lots"], []); self.assertEqual(bk.realized, 0.0); self.assertEqual(bk.stops_today, 0); self.assertIsNone(bk.pos["halt"]); self.assertEqual(bk.dyn, {})
        self.assertEqual(kinds(cy)[-1], "STATE_DISCARDED")

if __name__ == "__main__":
    unittest.main()
