"""Independent, durable Slack relay. Reads the C engine; never calls an exchange."""
import argparse
import json
from pathlib import Path
import signal
import sqlite3
import time

from bot.notify import NotificationError, send
from .notices import DataUnavailable, REPLAY_VERSION, Replay, Source, TERMINAL, fact_payload, fill_payload, krw, number, payload, qty


def operating_fields(state, status):
    campaigns=list(state['campaigns'].values()) if state['version']==3 else [state['campaign']] if state['campaign'] else []
    positions=[]
    for c in campaigns:
        if number(c['qty']) <= 0:
            positions.append(c['coin']+' 매수 대기 · 미체결')
            continue
        text=c['coin']+' '+qty(c['qty'])+'개'
        if number(c['qty'])>0:
            text+=' · 평단 '+krw(number(c['cost'])/number(c['qty']))+' · 미실현 '+krw(number(c['qty'])*number(c['mark'])-number(c['cost']),True,decimals=1)
        positions.append(text)
    from .accounting import residual_value
    for coin,r in sorted(state.get('residuals',{}).items()):
        if number(r['qty']) > 0:
            positions.append(coin+' '+qty(r['qty'])+'개 · 이월 잔량 · 평가액 '+krw(residual_value(r),decimals=1)+
                             ' · 미실현 '+krw(residual_value(r)-number(r['cost']),True,decimals=1))
    position='\n'.join(positions) or '없음'
    stops = [o for o in state['orders'].values() if o['role']=='protect' and o['status'] not in TERMINAL]
    stop = ', '.join((o.get('coin','')+' ')+qty(o['qty'])+'개 @ '+krw(o['trigger_price']) for o in stops) or '없음'
    mode='진입 일시정지' if status.get('entry_paused') else '실거래 학습' if (status.get('model') or {}).get('state')=='research' else str(status['mode']).upper()
    return [['상태', ('HALT' if state['halt'] else mode)+' · '+('시세 연결' if status.get('connected') else '시세 연결 확인 필요')],
            ['포지션',position],['거래소 보호 주문',stop]]


def heartbeat(state, status, now, *, boot=False):
    counts = status.get('counts',{})
    fields = operating_fields(state,status)
    trading_coins = (status.get('rule') or {}).get('coins')
    fields += [['매매 종목' if trading_coins is not None else '감시 종목', ', '.join(trading_coins if trading_coins is not None else status.get('markets',{})) or '준비 중'],
               ['WS / 계정 오류',f"{int(counts.get('ws_errors',0))} / {int(counts.get('account_errors',0))}"]]
    return payload('부팅' if boot else '상태', '알림 연결 · 현재 운용 상태' if boot else '60분 운용 현황', fields,
                   ['C 원화 장부를 읽어 알림을 전송합니다.'] if boot else None, t_ms=now*1000)


class Relay:
    def __init__(self, directory, env_path, *, sender=send, clock=time.time):
        self.directory = Path(directory).resolve()
        self.folder = self.directory/'notifications'
        self.folder.mkdir(parents=True,exist_ok=True)
        self.source, self.env_path, self.sender, self.clock = Source(directory), str(env_path), sender, clock
        self.guard = sqlite3.connect(self.folder/'writer.lock.sqlite',timeout=0)
        try:
            self.guard.execute('BEGIN EXCLUSIVE')
        except sqlite3.OperationalError:
            self.guard.close()
            raise RuntimeError('C notification relay already running') from None
        self.db = sqlite3.connect(self.folder/'relay.sqlite')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE NOT NULL,
                created REAL NOT NULL, ready REAL NOT NULL, payload TEXT NOT NULL, fact TEXT,
                state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_at REAL NOT NULL DEFAULT 0, sent_at REAL, last_error TEXT);
        ''')
        with self.db:
            # A crash after Slack accepted a request but before our commit is ambiguous.
            # Retry durably; incoming webhooks cannot promise exactly-once delivery.
            self.db.execute("UPDATE queue SET state='pending',last_error='delivery uncertain after restart' WHERE state='sending'")
            # User preference: trade notices on exits only, including already queued
            # entries from the previous release. Keep the outbox audit trail.
            for row in self.db.execute("SELECT id,payload FROM queue WHERE state='pending'").fetchall():
                if json.loads(row['payload']).get('kind') in ('진입','추가'):
                    self.db.execute("UPDATE queue SET state='suppressed',last_error=NULL WHERE id=?", (row['id'],))

    def close(self):
        self.db.close()
        self.guard.close()

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',(key,json.dumps(value,ensure_ascii=False)))

    def enqueue(self, key, card, now, *, fact=None, ready=None):
        self.db.execute('INSERT OR IGNORE INTO queue(key,created,ready,payload,fact) VALUES (?,?,?,?,?)',
                        (key,now,now if ready is None else ready,json.dumps(card,ensure_ascii=False),json.dumps(fact) if fact else None))

    def combine_close(self, fact):
        """Replace unsent fill cards; do not announce already-delivered sales again."""
        cids = set(fact.get('order_cids',[]))
        covered_qty, covered_gross = number(0),number(0)
        rows = self.db.execute("SELECT id,fact,state,attempts FROM queue WHERE key LIKE 'fill:%' AND fact IS NOT NULL AND state!='suppressed'").fetchall()
        for row in rows:
            part = json.loads(row['fact'])
            if part.get('cid') not in cids:
                continue
            if row['state']=='pending' and row['attempts']==0:
                self.db.execute("UPDATE queue SET state='suppressed',last_error=NULL WHERE id=?",(row['id'],))
            else:
                # An attempted delivery may have reached Slack. Preserve its retry
                # record, and do not create a second card covering that quantity.
                covered_qty += number(part['qty'])
                covered_gross += number(part['gross'])
        fact = dict(fact,notice_sold=str(number(fact['campaign']['sold'])-covered_qty))
        if fact.get('sell_gross') is not None:
            fact['notice_gross'] = str(number(fact['sell_gross'])-covered_gross)
        return fact

    def ingest(self, rows, context, now, state, status):
        replay = Replay(context)
        for row in rows:
            fact = replay.apply(row)
            if fact:
                if fact['type'] == 'fill':
                    # The CLOSE card includes the final sell; avoid two full-exit notices.
                    if fact['buy'] or number(fact['remaining']) == 0:
                        pass
                    else:
                        key = 'fill:'+fact['cid']
                        pending = self.db.execute("SELECT * FROM queue WHERE key LIKE ? AND state='pending' AND attempts=0 ORDER BY id DESC LIMIT 1",(key+':%',)).fetchone()
                        if pending:
                            merged = json.loads(pending['fact'])
                            for field in ('qty','gross','fee','pnl'):
                                merged[field] = str(number(merged[field])+number(fact[field]))
                            merged.update(remaining=fact['remaining'],t_ms=fact['t_ms'])
                            self.db.execute('UPDATE queue SET payload=?,fact=?,ready=? WHERE id=?',
                                            (json.dumps(fill_payload(merged),ensure_ascii=False),json.dumps(merged),min(now+2,pending['created']+5),pending['id']))
                        else:
                            self.enqueue(key+':'+str(fact['seq']),fill_payload(fact),now,fact=fact,ready=now+2)
                else:
                    if fact['type']=='close':
                        fact = self.combine_close(fact)
                    card = fact_payload(fact)
                    if card:
                        key = 'close:'+fact['campaign']['id'] if fact['type']=='close' else 'event:'+str(fact['seq'])
                        if card['kind']=='이상':
                            reason = fact['body'].get('reason','') if fact['type']=='HALT' else ''
                            throttle = 'alert:'+fact['type']+':'+reason
                            last = self.get(throttle)
                            if last is not None and now-last < 60:
                                card = None
                            else:
                                self.put(throttle,now)
                                card['fields'] += operating_fields(state,status)
                        if card:
                            self.enqueue(key,card,now)
            self.put('cursor',row[0])
        self.put('context',replay.context)

    def health(self, issue, state, status, now):
        prior = self.get('health_issue')
        candidate = self.get('health_candidate')
        if issue and issue != candidate:
            self.put('health_candidate',issue)
            self.put('health_since',now)
        elif not issue:
            self.put('health_candidate',None)
        # A disconnected socket gets a short reconnect grace; old/missing status does not.
        if issue == '시세 연결 끊김' and now-self.get('health_since',now) < 30:
            return
        if issue != prior:
            self.put('health_issue',issue)
            if issue:
                card = payload('이상','감시 상태 확인',operating_fields(state,status) if state else None,
                               [issue+' · 엔진·계정 상태 확인이 필요합니다.'],t_ms=now*1000)
            else:
                card = payload('복구','감시 데이터 정상화',lines=['최신 C 장부와 시세 연결을 다시 확인했습니다.'],t_ms=now*1000)
            sequence = self.get('health_sequence',0)+1
            self.put('health_sequence',sequence)
            self.enqueue('health:'+str(sequence),card,now)

    def poll(self):
        now = self.clock()
        cursor = self.get('cursor')
        try:
            state,status,rows,maximum = self.source.read(cursor)
            if cursor is not None and maximum < cursor:
                raise DataUnavailable('C ledger sequence moved backwards')
            with self.db:
                if cursor is None:
                    replay = Replay()
                    for row in rows:
                        replay.apply(row)
                    self.put('cursor',maximum)
                    self.put('context',replay.context)
                    self.put('context_version',REPLAY_VERSION)
                    self.put('last_hb',now)
                    self.enqueue('initial-connection',heartbeat(state,status,now,boot=True),now)
                else:
                    if self.get('context_version') != REPLAY_VERSION:
                        # Rebuild only processed context. Preserve cursor/outbox so
                        # upgrading during a merged campaign neither skips nor replays sales.
                        _,_,history,_ = self.source.read()
                        replay = Replay()
                        for row in history:
                            if row[0] <= cursor:
                                replay.apply(row)
                        self.put('context',replay.context)
                        self.put('context_version',REPLAY_VERSION)
                    self.ingest(rows,self.get('context'),now,state,status)
                age = now-status['t_ms']/1000
                issue = 'C 상태 보고가 2분 이상 지연됨' if age>120 else '시세 연결 끊김' if not status.get('connected') else '저장 공간 상태 확인 필요' if not status.get('storage_ok',True) else None
                self.health(issue,state,status,now)
                if now-self.get('last_hb',now)>=3600:
                    self.enqueue('heartbeat:'+str(int(now)),heartbeat(state,status,now),now)
                    self.put('last_hb',now)
                self.put('last_poll',now)
                self.put('source_error',None)
        except (DataUnavailable,ValueError,TypeError,KeyError,ArithmeticError):
            with self.db:
                self.put('source_error','C ledger/status unavailable')
                self.health('C 장부·상태 파일을 읽을 수 없음',None,None,now)
        self.deliver(now)
        self.write_status()

    def deliver(self, now):
        row = self.db.execute("SELECT * FROM queue WHERE state IN ('pending','sending') ORDER BY id LIMIT 1").fetchone()
        if not row or max(row['ready'],row['next_at']) > now:
            return
        with self.db:
            self.db.execute("UPDATE queue SET state='sending',attempts=attempts+1 WHERE id=?",(row['id'],))
        try:
            result = self.sender(**json.loads(row['payload']),data_dir=str(self.directory),env_path=self.env_path)
            if result != (200,'ok'):
                raise NotificationError('notify: delivery acknowledgement unavailable',uncertain=True)
        except NotificationError as exc:
            error = 'Slack HTTP '+str(exc.status) if exc.status else 'Slack delivery unavailable'
            if exc.uncertain:
                error += ' (delivery uncertain)'
            with self.db:
                delay = min(300,5*2**min(row['attempts'],6))
                self.db.execute("UPDATE queue SET state='pending',next_at=?,last_error=? WHERE id=?",(now+delay,error,row['id']))
                self.put('last_failure',dict(t_ms=int(now*1000),error=error))
            print(json.dumps(dict(kind='NOTIFY_ERROR',error=error,retry_seconds=delay)),flush=True)
        else:
            with self.db:
                self.db.execute("UPDATE queue SET state='sent',sent_at=?,last_error=NULL WHERE id=?",(now,row['id']))
                self.put('last_sent',dict(t_ms=int(now*1000),http_status=200,ack='ok',kind=json.loads(row['payload'])['kind']))
                self.put('last_failure',None)
            print(json.dumps(dict(kind='NOTIFY_SENT',http_status=200,ack='ok',notice=json.loads(row['payload'])['kind'])),flush=True)

    def write_status(self):
        report = dict(t_ms=int(self.clock()*1000),cursor=self.get('cursor'),last_poll=self.get('last_poll'),
                      last_sent=self.get('last_sent'),last_failure=self.get('last_failure'),source_error=self.get('source_error'),
                      health_issue=self.get('health_issue'),trade_notifications='exits_only',
                      pending=self.db.execute("SELECT COUNT(*) FROM queue WHERE state IN ('pending','sending')").fetchone()[0],
                      suppressed=self.db.execute("SELECT COUNT(*) FROM queue WHERE state='suppressed'").fetchone()[0])
        temp = self.folder/'status.json.tmp'
        temp.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        temp.replace(self.folder/'status.json')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',required=True,type=Path)
    parser.add_argument('--env',required=True,type=Path)
    parser.add_argument('--once',action='store_true',help='Poll and deliver once, then exit.')
    args = parser.parse_args()
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    relay = Relay(args.data_dir,args.env)
    try:
        while not stopping:
            relay.poll()
            if args.once:
                break
            time.sleep(1)
    finally:
        relay.close()


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        # Never print exception text that might contain a URL or local secret.
        print(json.dumps(dict(kind='NOTIFY_FATAL',error_type=type(exc).__name__)),flush=True)
        raise SystemExit(1) from None
