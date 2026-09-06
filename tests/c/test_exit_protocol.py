"""Public replay hypotheses include the live protection cancellation sequence."""
import unittest
from tests.c.test_learning import action, state
from tests.c.test_market import cfg, book, trade
from track_c.replay.execution import Attempt, ExitPair


class ProtectiveExitTests(unittest.TestCase):
    def held(self, **config):
        attempt=Attempt(action(),cfg(**config),state())
        attempt.event(trade(1000300))
        attempt.event(book(1000600,100.))
        return attempt

    def test_protection_settlement_changes_arrival_value_with_the_same_entry(self):
        direct=self.held(exit_protocol='direct');protected=self.held()
        for a in (direct,protected):
            a.request_exit(1000700,'recovery',99.)
            a.event(book(1000900,104.))
            a.advance(1001000)
        self.assertEqual(direct.gross,104.)
        self.assertEqual(protected.gross,0.)
        protected.event(book(1001300,99.));protected.advance(1001450)
        self.assertEqual(protected.gross,99.)
        self.assertEqual(protected.result()['exit_phases'],[dict(request_ms=1000700,protection_settled_ms=1001200,
                                                                 market_arrival_ms=1001450,protection_present=True)])
        self.assertFalse(protected.result()['live_execution_verified'])

    def test_protective_fill_during_cancel_removes_quantity_before_market_sale(self):
        a=self.held();a.request_exit(1000700,'recovery',103.)
        a.event(book(1000800,96.,qty=.8))
        a.event(trade(1000850,price=97.))
        self.assertAlmostEqual(a.protect_sold,.4)
        self.assertAlmostEqual(a.qty,.6)
        a.event(book(1001300,95.));a.advance(1001450)
        r=a.result()
        self.assertAlmostEqual(r['sold_qty'],1.)
        self.assertAlmostEqual(r['gross_exit_krw'],.4*96+.6*95)
        self.assertEqual(r['reason'],'exchange_stop')
        self.assertEqual(r['cause'],'collapse')

    def test_fully_filled_protection_cannot_be_sold_again(self):
        a=self.held();a.request_exit(1000700,'recovery',103.)
        a.event(book(1000800,96.));a.event(trade(1000850,price=97.))
        a.advance(1001500)
        self.assertEqual(a.sold,1.);self.assertEqual(a.gross,96.)
        self.assertIsNone(a.pending_exit)
        self.assertEqual(a.result()['reason'],'exchange_stop')

    def test_triggered_stop_limit_waits_for_buyers_above_its_limit(self):
        a=self.held()
        a.event(book(1000800,94.));a.event(trade(1000850,price=95.))
        self.assertEqual(a.sold,0.)
        a.event(trade(1000900,qty=.4,price=96.,buy=True))
        self.assertAlmostEqual(a.sold,.4)
        a.event(trade(1000950,qty=1.,price=96.,buy=True))
        self.assertAlmostEqual(a.sold,1.)
        self.assertAlmostEqual(a.gross,96.)

    def test_cancelled_protection_cannot_fill_from_later_trades(self):
        a=self.held();a.request_exit(1000700,'stop')
        a.event(book(1001100,94.));a.event(trade(1001200,price=97.))
        a.event(trade(1001300,price=96.,buy=True));a.advance(1001450)
        self.assertEqual(a.protect_sold,0.)
        self.assertEqual(a.gross,94.)

    def test_cancel_race_can_leave_dust_but_does_not_fabricate_liquidation(self):
        a=self.held();a.a['minimum']=80.
        a.request_exit(1000700,'stop')
        a.event(book(1000800,96.,qty=.8));a.event(trade(1000850,price=97.))
        a.advance(1001000)
        self.assertFalse(a.done)
        a.advance(1001250)
        r=a.result()
        self.assertAlmostEqual(r['residual_qty'],.6)
        self.assertAlmostEqual(r['cash_net_krw'],.4*96-100)
        self.assertEqual(r['reason'],'residual')

    def test_both_landmark_arms_keep_protection_and_failed_paths(self):
        hold=self.held();pair=ExitPair(hold,1000700,state(1000700))
        for a in (hold,pair.sell):
            a.event(book(1000800,96.));a.event(trade(1000850,price=97.))
        r=pair.result(1000850)
        self.assertFalse(r['censored'])
        self.assertEqual(r['hold_reason'],'exchange_stop')
        self.assertEqual(r['sell_reason'],'exchange_stop')
        self.assertEqual(r['delta_bp'],0.)

    def test_missing_observations_during_cancellation_remain_unknown(self):
        a=self.held();a.request_exit(1000700,'recovery')
        r=a.result(1001000)
        self.assertTrue(r['censored']);self.assertIsNone(r['cause'])
        a.advance(1004000)
        self.assertTrue(a.result(1004000)['censored'])
        self.assertTrue(a.observation_gap)
