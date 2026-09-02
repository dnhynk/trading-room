"""OMS invariants of bot/cycle.py (Book against a stub exchange).  python -m unittest bot.test_cycle -v"""
import asyncio, time, unittest
from types import SimpleNamespace
from bot.signal import Features, STRAT, book_params
from bot.ws import load_params
from bot import cycle
from bot.cycle import Book, valid_params, quantize_unit

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
    def _cy(self, lots=(), lever=10, acct_lever=20.0, mode="crossed"):
        from bot.cycle import Cycle
        cy, bk = book(lots=lots); cy.books = {"long": bk}; cy.sp["lever"] = lever; cy.lever_t = 0.0
        cy.b.margin_mode = mode
        cy.b.account = lambda symbol: dict(marginMode=mode, crossedMarginLeverage=str(acct_lever), isolatedLongLever=str(acct_lever), isolatedShortLever=str(acct_lever), available="100")
        cy.b.set_leverage = lambda symbol, lev, hold_side=None: cy.b.calls.append(("lever", lev, hold_side)) or {}
        return cy, bk, Cycle.refresh_lever

    def test_a_flat_book_is_brought_to_the_configured_leverage(self):
        cy, bk, refresh = self._cy()
        asyncio.run(refresh(cy))
        self.assertEqual([c for c in cy.b.calls if c[0] == "lever"], [("lever", 10, None)]); self.assertIn("LEVER_SET", kinds(cy)); self.assertEqual(bk.lever, 10)

    def test_a_positioned_book_is_left_alone_and_lever_0_means_hands_off(self):
        cy, bk, refresh = self._cy(lots=[[70, 3.0, "a"]])
        asyncio.run(refresh(cy))
        self.assertEqual([c for c in cy.b.calls if c[0] == "lever"], []); self.assertNotIn("LEVER_SET", kinds(cy)); self.assertEqual(bk.lever, 20.0)   # read, not set
        cy, bk, refresh = self._cy(lever=0)
        asyncio.run(refresh(cy)); self.assertEqual([c for c in cy.b.calls if c[0] == "lever"], [])
        cy, bk, refresh = self._cy(mode="isolated")
        asyncio.run(refresh(cy)); self.assertEqual([c for c in cy.b.calls if c[0] == "lever"], [("lever", 10, "long")])   # isolated: per side

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
        from bot.ws import outside_books, strat_for
        from bot.supervise import gone
        p = dict(strat=dict(symbol="AUSDT"), books={"AUSDT": dict(wallet_frac=0.5), "BUSDT": dict(wallet_frac=0.5)})
        self.assertFalse(outside_books(p, "AUSDT")); self.assertTrue(outside_books(p, "CUSDT"))
        self.assertFalse(outside_books(dict(strat=dict(symbol="AUSDT")), "CUSDT"))     # books 가 없으면 예전 단일 엔진: 판정하지 않는다
        self.assertEqual(strat_for(p, "CUSDT").get("wallet_frac"), 1.0)                # 거부하지 않으면 이 값으로 주문이 나간다
        self.assertTrue(gone("CUSDT") if (load_params() or {}).get("books") and "CUSDT" not in load_params()["books"] else True)

    def test_a_portfolio_gives_each_engine_its_own_strat_and_wallet_share(self):
        """params["books"] 가 있으면 심볼마다 엔진 하나. 공통 strat 위에 그 심볼의 몫만 덮고, 지갑은 wallet_frac 으로 나눈다 —
        안 나누면 두 엔진이 각자 계좌 전액으로 사이징해 노출이 심볼 수만큼 배가 된다 (NEXT 8)."""
        from bot.ws import strat_for, portfolio
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
