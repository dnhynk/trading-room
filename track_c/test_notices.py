import datetime as dt
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from bot import notify
from .notices import DataUnavailable, KST, Replay, Source, fill_payload, summary_fields
from .notify_relay import Relay


def at(text):
    return dt.datetime.fromisoformat(text).replace(tzinfo=KST).timestamp()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.now = at('2026-09-05T12:00:00')
        self.db = sqlite3.connect(self.path/'ledger.sqlite')
        self.addCleanup(self.db.close)
        self.db.executescript('CREATE TABLE state (id INTEGER PRIMARY KEY,body TEXT); CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT,t_ms INTEGER,kind TEXT,body TEXT);')
        self.state = dict(version=2,capital_initialized=True,cash_krw='500000',campaign=None,orders={},halt=None)
        self.status = dict(capital_mode='account_equity',t_ms=int(self.now*1000),account_krw_available='500000',mode='live',connected=True,counts={},markets={'BTC':{}},storage_ok=True)
        self.save()

    def save(self):
        self.db.execute('INSERT OR REPLACE INTO state VALUES (1,?)',(json.dumps(self.state),))
        self.db.commit()
        (self.path/'status.json').write_text(json.dumps(self.status),encoding='utf-8')

    def event(self, kind, body, t=None):
        self.db.execute('INSERT INTO events(t_ms,kind,body) VALUES (?,?,?)',(int((self.now if t is None else t)*1000),kind,json.dumps(body)))
        self.db.commit()

    def intent(self, *, cid='private-entry', coin='BTC', t=None, plan=None):
        self.event('CAMPAIGN_INTENT',dict(coin=coin,plan=plan or {}),t)
        self.event('ORDER_INTENT',dict(coin=coin,order=dict(cid=cid,side='BUY',role='entry')),t)

    def fill(self, q='1', gross='100', pnl='0', *, cid='private-entry', role='entry', fee='0', t=None):
        self.event('FILL',dict(cid=cid,role=role,qty=q,gross=gross,fee=fee,pnl=pnl,status='FILLED'),t)

    def closed(self, *, ident='campaign-private-id', coin='BTC', first=None, net='10', sold='1', t=None):
        self.event('CLOSE',dict(campaign=dict(id=ident,coin=coin,first_fill=self.now if first is None else first,net=net,sold=sold,qty='0',exit_reason='time')),t)

    def fields(self, **kw):
        return dict(summary_fields(self.path,now=self.now,**kw))


class AccountingTests(Fixture):
    def test_unsold_residual_is_capital_but_not_a_completed_win(self):
        self.state.update(version=3, campaigns={}, residuals=dict(BTC=dict(qty='2', cost='1980', mark='989')))
        self.save()
        self.assertEqual(self.fields()['잔고'], '501,978원')
        self.intent()
        self.fill(q='2', gross='1980')
        self.event('CLOSE', dict(campaign=dict(id='residual', coin='BTC', first_fill=self.now, net='10', sold='1', qty='2', exit_reason='dust')))
        self.assertEqual(self.fields()['승률'], '계산 전 · 0회 종료')

    def test_c_never_reads_ab_and_uses_krw(self):
        with patch.object(notify,'_latest_state',side_effect=AssertionError('A/B read')),patch.object(notify,'_engine_day',side_effect=AssertionError('A/B read')),patch('track_c.notices.time.time',return_value=self.now):
            fields = dict(notify.summary_fields(track='C',data_dir=self.path))
        self.assertEqual(fields['잔고'],'500,000원')
        self.assertNotIn('USDT',json.dumps(fields))
        self.assertNotIn('$',json.dumps(fields))

    def test_partial_entry_counted_once_close_not_double_pnl_and_deposit_only_changes_denominator(self):
        self.intent()
        self.fill(q='0.4',gross='40',fee='1',pnl='-1')
        self.fill(q='0.6',gross='60')
        self.event('ORDER_INTENT',dict(order=dict(cid='sell',side='SELL',role='exit')))
        self.fill(cid='sell',role='exit',gross='112',fee='1',pnl='11')
        self.closed(net='10')
        self.closed(net='10')  # Crash-replayed CLOSE cannot inflate wins.
        self.event('FLAT',{})
        self.assertEqual(self.fields()['누적 손익'],'+10.0원')
        self.assertEqual(self.fields()['캠페인 횟수'],'1회')
        self.assertEqual(self.fields()['승률'],'100.0% · 1승/1회')
        self.state['cash_krw']='1000'; self.status['account_krw_available']='1000'; self.save()
        self.assertEqual(self.fields()['엔진 수익률'],'+1.00%')
        self.event('EXTERNAL_CAPITAL',dict(balance='2000',external_delta='1000'))
        self.state['cash_krw']='2000'; self.status['account_krw_available']='2000'; self.save()
        self.assertEqual(self.fields()['엔진 수익률'],'+0.50%')
        self.assertEqual(self.fields()['누적 손익'],'+10.0원')

    def test_kst_midnight_previous_day_campaign_is_not_today_win(self):
        start = at('2026-09-04T23:59:59')
        self.intent(t=start); self.fill(t=start,pnl='-1',fee='1')
        self.event('ORDER_INTENT',dict(order=dict(cid='sell',side='SELL',role='exit')))
        self.fill(cid='sell',role='exit',pnl='11')
        self.closed(first=start,net='10')
        self.assertEqual(self.fields()['누적 손익'],'+11.0원')
        self.assertEqual(self.fields()['캠페인 횟수'],'0회')
        self.assertEqual(self.fields()['승률'],'계산 전 · 0회 종료')

    def test_open_campaign_and_unfilled_intent_do_not_count_as_closed(self):
        self.intent(); self.event('NO_FILL',dict(campaign={})); self.event('FLAT',{})
        self.intent(); self.fill()
        self.assertEqual(self.fields()['캠페인 횟수'],'1회')
        self.assertEqual(self.fields()['승률'],'계산 전 · 0회 종료')

    def test_inventory_mark_and_pending_buy_cash_reservation(self):
        self.state['campaign']=dict(qty='2',mark='110')
        self.state['orders']={'buy':dict(side='BUY',status='LIVE',qty='10',filled='2',price='100')}
        self.save()
        self.assertEqual(self.fields()['잔고'],'500,220원')
        self.assertEqual(self.fields()['가용'],'499,200원')
        self.status['account_krw_available']='490000'; self.save()
        self.assertEqual(self.fields()['가용'],'490,000원')

    def test_missing_or_stale_c_data_never_falls_back(self):
        self.status['t_ms']-=180000; self.save()
        self.assertIn('3분 전',self.fields()['잔고'])
        self.assertNotIn('잔고',self.fields(balance=False))
        (self.path/'status.json').unlink()
        with patch.object(notify,'_engine_day',side_effect=AssertionError('fallback')):
            fields = notify.summary_fields(track='C',data_dir=self.path)
        self.assertEqual(len(fields),6)
        self.assertTrue(all('확인 불가' in v for _,v in fields))

    def test_source_does_not_create_missing_db_or_allow_write(self):
        absent=self.path/'missing'; absent.mkdir()
        with self.assertRaises(DataUnavailable):
            Source(absent).read()
        self.assertFalse((absent/'ledger.sqlite').exists())
        db=Source(self.path).connect()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('DELETE FROM events')
        finally:
            db.close()

    def test_c_baseline_is_separate_and_ignores_older_entries(self):
        self.intent(t=self.now-60); self.fill(t=self.now-60,pnl='5')
        folder=self.path/'notifications'; folder.mkdir()
        (folder/'baseline.json').write_text(json.dumps(dict(day='2026-09-05',start_ms=int(self.now*1000))))
        self.assertEqual(self.fields()['누적 손익'],'+0.0원')
        self.assertEqual(self.fields()['캠페인 횟수'],'0회')

    def test_btc_scope_excludes_interleaved_alt_fills_closes_and_survives_midnight(self):
        self.state['cash_krw']='1000'; self.status['account_krw_available']='1000'
        self.status['rule']=dict(coins=['BTC','ETH'])
        self.save()
        folder=self.path/'notifications'; folder.mkdir()
        # Yesterday's cutoff no longer applies, but its explicit BTC-only scope does.
        (folder/'baseline.json').write_text(json.dumps(dict(day='2026-09-04',start_ms=int((self.now-86400)*1000),coins=['BTC'])))
        self.intent(cid='btc',coin='BTC'); self.intent(cid='eth',coin='ETH')
        self.fill(cid='eth',fee='1',pnl='-1'); self.fill(cid='btc',fee='1',pnl='-1')
        for coin,cid,pnl in [('ETH','eth-sell','-99'),('BTC','btc-sell','11')]:
            self.event('ORDER_INTENT',dict(coin=coin,order=dict(cid=cid,side='SELL',role='exit')))
            self.fill(cid=cid,role='exit',pnl=pnl)
            self.closed(coin=coin,ident=coin,net='10' if coin=='BTC' else '-100')
        count=self.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        fields=self.fields()
        self.assertEqual(fields['누적 손익'],'+10.0원')
        self.assertEqual(fields['엔진 수익률'],'+1.00%')
        self.assertEqual(fields['캠페인 횟수'],'1회')
        self.assertEqual(fields['승률'],'100.0% · 1승/1회')
        self.assertEqual(fields['잔고'],'1,000원')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],count)

    def test_c3_status_scopes_stats_without_a_baseline_and_heartbeat_lists_trading_only(self):
        from .notify_relay import heartbeat
        self.status.update(rule=dict(coins=['BTC']),markets={'BTC':{},'ETH':{},'SOL':{}})
        self.save()
        self.intent(cid='eth',coin='ETH'); self.fill(cid='eth',pnl='-10')
        self.closed(coin='ETH',net='-10')
        fields=self.fields()
        self.assertEqual(fields['누적 손익'],'+0.0원')
        self.assertEqual(fields['캠페인 횟수'],'0회')
        self.assertEqual(fields['승률'],'계산 전 · 0회 종료')
        self.assertEqual(dict(heartbeat(self.state,self.status,self.now)['fields'])['매매 종목'],'BTC')

    def test_invalid_scope_never_silently_includes_all_coins(self):
        self.status['rule']=dict(coins='BTC'); self.save()
        self.assertTrue(all('확인 불가' in value for value in self.fields().values()))

    def test_deviation_scope_filters_whole_campaign_including_fees_and_survives_midnight(self):
        folder=self.path/'notifications'; folder.mkdir()
        (folder/'baseline.json').write_text(json.dumps(dict(day='2026-09-04',start_ms=int((self.now-86400)*1000),
                                                            coins=['BTC'],entry_dev_min_ticks=2.0)))
        for i,(coin,dev,net) in enumerate([('BTC',1.999999,100),('ETH',3.0,100),('BTC',2.0,10),('BTC',2.5,-5)]):
            self.intent(cid='entry'+str(i),coin=coin,plan=dict(dev_ticks=dev))
            self.fill(cid='entry'+str(i),q='0.4',gross='40',fee='1',pnl='-1')
            self.fill(cid='entry'+str(i),q='0.6',gross='60')
            self.event('ORDER_INTENT',dict(coin=coin,order=dict(cid='sale'+str(i),side='SELL',role='exit')))
            self.fill(cid='sale'+str(i),role='exit',gross=str(101+net),pnl=str(1+net))
            self.closed(ident=str(i),coin=coin,net=str(net))
            self.event('FLAT',dict(coin=coin))
        before=self.db.execute('SELECT seq,t_ms,kind,body FROM events ORDER BY seq').fetchall()
        fields=self.fields()
        self.assertEqual(fields['누적 손익'],'+5.0원')
        self.assertEqual(fields['캠페인 횟수'],'2회')
        self.assertEqual(fields['승률'],'50.0% · 1승/2회')
        self.assertEqual(fields['잔고'],'500,000원')
        self.assertEqual(fields['가용'],'500,000원')
        self.assertEqual(self.db.execute('SELECT seq,t_ms,kind,body FROM events ORDER BY seq').fetchall(),before)

    def test_unknown_deviation_cannot_qualify_for_filtered_statistics(self):
        folder=self.path/'notifications'; folder.mkdir()
        (folder/'baseline.json').write_text(json.dumps(dict(coins=['BTC'],entry_dev_min_ticks=2.0)))
        for i,dev in enumerate([None,float('nan'),float('inf'),'bad',2.0]):
            self.intent(plan=dict(dev_ticks=dev)); self.fill(pnl='1')
            self.closed(ident=str(i),net='1'); self.event('FLAT',{})
        fields=self.fields()
        self.assertEqual(fields['누적 손익'],'+1.0원')
        self.assertEqual(fields['캠페인 횟수'],'1회')
        self.assertEqual(fields['승률'],'100.0% · 1승/1회')

    def test_detail_precedes_c_dashboard_and_private_ids_not_rendered(self):
        self.intent(); self.fill()
        replay=Replay(); facts=[replay.apply(row) for row in Source(self.path).read()[2]]
        card=fill_payload(next(f for f in facts if f))
        blocks=notify._blocks(**{k:v for k,v in card.items() if k!='track'},track='C',data_dir=self.path)
        encoded=json.dumps(blocks,ensure_ascii=False)
        self.assertLess(encoded.index('체결 순손익'),encoded.index('잔고'))
        self.assertNotIn('private-entry',encoded)


class RelayTests(Fixture):
    def setUp(self):
        super().setUp()
        self.sent=[]
        self.relay=Relay(self.path,self.path/'nonexistent.env',sender=self.send,clock=lambda:self.now)
        self.addCleanup(lambda:self.relay.close())

    def send(self, **card):
        self.assertEqual(card['track'],'C')
        self.assertEqual(card['data_dir'],str(self.path.resolve()))
        self.sent.append(card)
        return 200,'ok'

    def restart(self):
        self.relay.close()
        self.relay=Relay(self.path,self.path/'nonexistent.env',sender=self.send,clock=lambda:self.now)

    def test_first_start_tails_history_and_restart_does_not_repeat_boot(self):
        self.intent(); self.fill()
        self.relay.poll()
        self.assertEqual([p['kind'] for p in self.sent],['부팅'])
        self.restart(); self.relay.poll()
        self.assertEqual(len(self.sent),1)
        # Hydrated old campaign context allows the subsequent exit to be understood.
        self.event('ORDER_INTENT',dict(order=dict(cid='sell',side='SELL',role='exit')))
        self.fill(cid='sell',role='exit',pnl='10'); self.closed()
        self.relay.poll()
        self.assertEqual([p['kind'] for p in self.sent],['부팅','전량청산'])
        self.closed(); self.relay.poll()
        self.assertEqual(len(self.sent),2)

    def test_buy_fills_remain_silent_across_restart_and_close_retains_accounting(self):
        self.relay.poll(); self.intent(); self.fill(q='0.4',gross='40')
        self.relay.poll(); self.restart(); self.now+=1
        self.fill(q='0.6',gross='66'); self.relay.poll()
        self.event('ORDER_INTENT',dict(order=dict(cid='sell',side='SELL',role='exit')))
        self.fill(cid='sell',role='exit',gross='110',pnl='4'); self.closed(net='4')
        self.relay.poll(); self.assertEqual(len(self.sent),2)
        self.now+=2; self.relay.poll(); self.relay.poll()
        self.assertEqual([p['kind'] for p in self.sent],['부팅','전량청산'])
        self.assertEqual(dict(self.sent[1]['fields'])['캠페인 순손익'],'+4.0원')

    def test_upgrade_suppresses_queued_entry_without_blocking_exit(self):
        self.relay.poll()
        with self.relay.db:
            self.relay.enqueue('old-entry',dict(track='C',kind='진입',head='old'),self.now)
            self.relay.enqueue('exit',dict(track='C',kind='전량청산',head='exit'),self.now)
        self.restart(); self.relay.poll()
        self.assertEqual([p['kind'] for p in self.sent], ['부팅','전량청산'])
        report=json.loads((self.path/'notifications/status.json').read_text(encoding='utf-8'))
        self.assertEqual(report['trade_notifications'],'exits_only')
        self.assertEqual((report['suppressed'],report['pending']),(1,0))

    def test_failed_delivery_survives_restart_and_does_not_advance_sent(self):
        def failed(**_):
            raise notify.NotificationError('must not echo credential',status=429)
        self.relay.sender=failed; self.relay.poll()
        report=json.loads((self.path/'notifications/status.json').read_text())
        self.assertEqual(report['pending'],1)
        self.assertNotIn('credential',json.dumps(report))
        self.restart(); self.relay.poll(); self.assertEqual(self.sent,[])
        self.now+=5; self.relay.poll(); self.assertEqual(len(self.sent),1)
        self.assertEqual(self.relay.get('last_sent')['http_status'],200)

    def test_second_worker_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError,'already running'):
            Relay(self.path,'unused')

    def test_unchanged_scan_and_routine_events_are_silent(self):
        self.relay.poll()
        for symbols in (['BTC'],['BTC'],['ETH','BTC']):
            self.event('SCAN',dict(symbols=symbols))
        for kind in ('CAPITAL_SYNC','MARK','ORDER_STATUS','SIGNAL','DAY'):
            self.event(kind,{})
        self.relay.poll()
        self.assertEqual([p['kind'] for p in self.sent],['부팅','종목변경'])

    def test_burst_errors_throttled_and_source_failure_recovers(self):
        self.relay.poll()
        for _ in range(4):
            self.event('API_ERROR',dict(error='secret text must not be rendered'))
        self.relay.poll()
        self.assertEqual(len(self.sent),2)
        self.assertNotIn('secret text',json.dumps(self.sent))
        (self.path/'status.json').unlink(); self.relay.poll(); self.relay.poll()
        self.assertEqual(len(self.sent),3)
        self.save(); self.relay.poll()
        self.assertEqual([p['kind'] for p in self.sent][-2:],['이상','복구'])

    def test_disconnect_grace_and_hourly_heartbeat(self):
        self.relay.poll()
        self.status['connected']=False; self.save(); self.relay.poll()
        self.assertEqual(len(self.sent),1)
        self.now+=31; self.relay.poll(); self.assertEqual(self.sent[-1]['kind'],'이상')
        self.status['connected']=True; self.save(); self.relay.poll()
        self.assertEqual(self.sent[-1]['kind'],'복구')
        self.now+=3600; self.status['t_ms']=int(self.now*1000); self.save(); self.relay.poll()
        self.assertEqual(self.sent[-1]['head'],'C · 60분 운용 현황')

    def test_backward_cursor_is_reported_without_replaying(self):
        self.event('SCAN',dict(symbols=['BTC'])); self.relay.poll()
        self.db.execute('DELETE FROM events'); self.db.commit(); self.relay.poll()
        self.assertIsNotNone(self.relay.get('source_error'))
        self.assertEqual([p['kind'] for p in self.sent],['부팅','이상'])


class DeliveryTests(unittest.TestCase):
    def test_http_network_and_redirect_diagnostics_do_not_echo_secret(self):
        for error in (urllib.error.HTTPError('https://example.com/secret',403,'secret',{},None),urllib.error.URLError('secret')):
            with patch.object(notify,'_hook',return_value='https://hooks.slack.com/services/dummy'),patch.object(notify,'_blocks',return_value=[]),patch('urllib.request.build_opener') as opener:
                opener.return_value.open.side_effect=error
                with self.assertRaises(notify.NotificationError) as caught:
                    notify.send('상태','test')
                self.assertNotIn('secret',str(caught.exception))
        with self.assertRaisesRegex(notify.NotificationError,'redirect refused'):
            notify._NoRedirect().redirect_request(None,None,302,'secret',None,'https://example.com/secret')

    def test_acknowledgement_required(self):
        for body, accepted in ((b'ok',True),(b'error containing secret',False)):
            response=io.BytesIO(body); response.status=200
            with patch.object(notify,'_hook',return_value='https://hooks.slack.com/services/dummy'),patch.object(notify,'_blocks',return_value=[]),patch('urllib.request.build_opener') as opener:
                opener.return_value.open.return_value=response
                if accepted:
                    self.assertEqual(notify.send('상태','test'),(200,'ok'))
                else:
                    with self.assertRaises(notify.NotificationError) as caught:
                        notify.send('상태','test')
                    self.assertTrue(caught.exception.uncertain)
                    self.assertNotIn('secret',str(caught.exception))

    def test_hook_rejects_wrong_destination_and_conflicting_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'.env'
            for body in ('slack-webhook-url=https://example.com/secret','slack-webhook-url=https://[secret','slack-webhook-url=a\nslack_webhook_url=b'):
                path.write_text(body)
                with self.assertRaises(notify.NotificationError) as caught:
                    notify._hook(path)
                self.assertNotIn('secret',str(caught.exception))


if __name__=='__main__':
    unittest.main()
