"""Read-only KRW notification accounting and sanitized human-readable events."""
import datetime as dt
from contextlib import closing
from decimal import Decimal as D, InvalidOperation, ROUND_HALF_UP
import json
from pathlib import Path
import sqlite3
import time

KST = dt.timezone(dt.timedelta(hours=9))
TERMINAL = {'FILLED','CANCELED','REJECTED','NOT_TRIGGERED_CANCELED','CANCELED_NO_ORDER','CANCELED_LIMIT_PRICE_EXCEED','CANCELED_UNDER_PRODUCT_UNIT'}
HISTORY = ('CAMPAIGN_INTENT','ORDER_INTENT','FILL','CLOSE','NO_FILL','FLAT','SCAN')


class DataUnavailable(RuntimeError):
    pass


def number(value):
    value = D(str(value))
    if not value.is_finite():
        raise ValueError('nonfinite amount')
    return value


def krw(value, signed=False, *, decimals=0):
    rounded = number(value).quantize(D(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    if not rounded: rounded = D(0)
    return format(rounded, ('+' if signed else '')+f',.{decimals}f')+'원'


def qty(value):
    return format(number(value), 'f').rstrip('0').rstrip('.') if '.' in format(number(value),'f') else format(number(value),'f')


def stamp(milliseconds):
    return dt.datetime.fromtimestamp(milliseconds/1000, KST).strftime('%m-%d %H:%M:%S KST')


class Source:
    def __init__(self, directory):
        if not directory:
            raise DataUnavailable('C data directory is required')
        self.directory = Path(directory).resolve()

    def connect(self):
        # mode=ro never creates a missing engine DB or changes its schema/state.
        return sqlite3.connect((self.directory/'ledger.sqlite').as_uri()+'?mode=ro', uri=True, timeout=3)

    def read(self, after=None):
        try:
            with closing(self.connect()) as db:
                db.execute('BEGIN')
                state = json.loads(db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])
                maximum = db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
                if after is None:
                    sql = 'SELECT seq,t_ms,kind,body FROM events WHERE kind IN ('+','.join('?' for _ in HISTORY)+') ORDER BY seq'
                    rows = db.execute(sql,HISTORY).fetchall()
                else:
                    rows = db.execute('SELECT seq,t_ms,kind,body FROM events WHERE seq>? AND seq<=? ORDER BY seq LIMIT 1000',(after,maximum)).fetchall()
            status = json.loads((self.directory/'status.json').read_text(encoding='utf-8'))
            if state['version'] not in (2,3) or not state['capital_initialized'] or status.get('capital_mode') != 'account_equity':
                raise ValueError('C capital state unavailable')
            return state, status, [(s,t,k,json.loads(b)) for s,t,k,b in rows], maximum
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, IndexError):
            raise DataUnavailable('C ledger/status unavailable') from None


class Replay:
    """Interleaved campaigns; ids stay internal and are never placed in Slack text."""
    def __init__(self, context=None):
        self.context = context or dict(campaign=None, symbols=[])
        if 'campaigns' not in self.context:
            old=self.context.get('campaign')
            self.context['campaigns']={old['coin']:old} if old else {}

    def apply(self, row):
        seq, t, kind, body = row
        books=self.context['campaigns']
        coin=body.get('coin') or body.get('order',{}).get('coin') or body.get('campaign',{}).get('coin')
        if not coin and body.get('cid'):
            coin=next((k for k,c in books.items() if body['cid'] in c['orders']),None)
        if not coin and len(books)==1: coin=next(iter(books))
        c=books.get(coin)
        if kind == 'CAMPAIGN_INTENT':
            books[body['coin']] = dict(coin=body['coin'], started_ms=None, qty='0', orders={}, buy_gross='0', sell_gross='0', fees='0',
                                      entry_dev_ticks=(body.get('plan') or {}).get('dev_ticks'))
        elif kind == 'ORDER_INTENT':
            if c is None:
                raise DataUnavailable('C campaign context missing')
            c['orders'][body['order']['cid']] = body['order']
        elif kind == 'FILL':
            if c is None or body['cid'] not in c['orders']:
                raise DataUnavailable('C fill context missing')
            q, gross, fee = (number(body[k]) for k in ('qty','gross','fee'))
            if q <= 0:
                raise DataUnavailable('C fill quantity invalid')
            buy = c['orders'][body['cid']]['side'] == 'BUY'
            first = buy and c['started_ms'] is None
            if first:
                c['started_ms'] = t
            c['qty'] = str(number(c['qty'])+(q if buy else -q))
            field = 'buy_gross' if buy else 'sell_gross'
            c[field] = str(number(c[field])+gross)
            c['fees'] = str(number(c['fees'])+fee)
            return dict(body,type='fill',seq=seq,t_ms=t,coin=c['coin'],first=first,started_ms=c['started_ms'],
                        remaining=c['qty'],buy=buy,entry_dev_ticks=c.get('entry_dev_ticks'))
        elif kind == 'CLOSE':
            campaign = body['campaign']
            started = c['started_ms'] if c else int(float(campaign['first_fill'])*1000) if campaign['first_fill'] is not None else None
            return dict(type='close',seq=seq,t_ms=t,coin=campaign['coin'],campaign=campaign,started_ms=started,
                        sell_gross=c['sell_gross'] if c else None,fees=c['fees'] if c else None,
                        entry_dev_ticks=c.get('entry_dev_ticks') if c else (campaign.get('plan') or {}).get('dev_ticks'))
        elif kind in ('NO_FILL','FLAT'):
            books.pop(coin,None)
            self.context['campaign'] = None
        elif kind == 'SCAN':
            old = self.context.get('symbols',[])
            self.context['symbols'] = body['symbols']
            if old and set(old) != set(body['symbols']):
                return dict(type='scan',seq=seq,t_ms=t,old=old,new=body['symbols'])
        elif kind in ('START','HALT','API_ERROR','ORDER_UNCERTAIN','ORDER_REJECTED','EXTERNAL_CAPITAL'):
            return dict(type=kind,seq=seq,t_ms=t,coin=c['coin'] if c else None,body=body)


def summary_fields(directory, *, day=None, balance=True, now=None):
    now = time.time() if now is None else now
    try:
        state,status,rows,_ = Source(directory).read()
        date = dt.date.fromisoformat(day) if day else dt.datetime.fromtimestamp(now,KST).date()
        start = int(dt.datetime.combine(date,dt.time(),KST).timestamp()*1000)
        end = start+86400000
        coins = (status.get('rule') or {}).get('coins')
        entry_floor = None
        baseline = Path(directory)/'notifications/baseline.json'
        if baseline.exists():
            b = json.loads(baseline.read_text())
            # Display scopes persist across midnight; the daily time cutoff does not.
            coins = b.get('coins', coins)
            if b.get('entry_dev_min_ticks') is not None:
                entry_floor = number(b['entry_dev_min_ticks'])
            if b.get('day') == date.isoformat():
                start = max(start,int(b['start_ms']))
        if coins is not None and (not isinstance(coins,list) or not coins or
                                  any(not isinstance(c,str) or not c for c in coins)):
            raise ValueError('invalid notification coin scope')
        allowed = set(coins) if coins is not None else None
        pnl, campaigns, wins, closed = D(0),0,0,0
        seen = set()
        replay = Replay()
        for row in rows:
            fact = replay.apply(row)
            # Replay every coin first so interleaved order/fill context remains intact.
            if not fact or (allowed is not None and fact.get('coin') not in allowed):
                continue
            if entry_floor is not None and fact['type'] in ('fill','close'):
                # Classify the whole campaign by its original entry intent, including
                # partial fills/fees. Missing historical deviations cannot qualify.
                try:
                    if number(fact.get('entry_dev_ticks')) < entry_floor:
                        continue
                except (InvalidOperation,ValueError,TypeError):
                    continue
            if fact['type'] == 'fill':
                if start <= fact['t_ms'] < end:
                    pnl += number(fact['pnl'])
                    if fact['first']:
                        campaigns += 1
            elif fact['type'] == 'close':
                c = fact['campaign']
                if not number(c.get('qty', '0')) and fact['started_ms'] is not None and start <= fact['started_ms'] < end and fact['t_ms'] < end and c['id'] not in seen:
                    seen.add(c['id']); closed += 1
                    wins += number(c['net']) > 0
        cash = number(state['cash_krw'])
        positions=list(state['campaigns'].values()) if state['version']==3 else [state['campaign']] if state['campaign'] else []
        from .accounting import marked_equity
        equity = marked_equity(state)
        reserved = sum((max(D(0),number(o['qty'])-number(o['filled']))*number(o['price'])
                        for o in state['orders'].values() if o['side']=='BUY' and o['status'] not in TERMINAL),D(0))
        available = max(D(0),min(number(status['account_krw_available']),cash-reserved))
        age = now-status['t_ms']/1000
        stale = f' · {int(max(0,age)//60)}분 전' if age > 120 else ''
        fields = [('잔고',krw(equity)+stale),('가용',krw(available)+stale)] if balance else []
        fields += [('누적 손익',krw(pnl,True,decimals=1)),('엔진 수익률',f'{pnl/equity*100:+.2f}%' if equity>0 else '계산 불가'),
                   ('캠페인 횟수',f'{campaigns:,}회'),('승률',f'{wins/closed*100:.1f}% · {wins}승/{closed}회' if closed else '계산 전 · 0회 종료')]
        return fields
    except (DataUnavailable,OSError,ValueError,TypeError,KeyError,InvalidOperation,ZeroDivisionError):
        labels = (['잔고','가용'] if balance else [])+['누적 손익','엔진 수익률','캠페인 횟수','승률']
        return [(label,'확인 불가 · C 데이터 미수신') for label in labels]


REASONS = {'time':'보유 기한 도달','premise':'진입 전제 무효화','exchange_stop':'거래소 보호 주문 체결',
           'daily_loss':'일일 손실 제한','operator_stop':'운영 정지','market_data_unavailable':'시세 확인 불가',
           'opposite_stall':'반등 정체','protection_rejected':'보호 주문 거절','halt':'엔진 중단','account_error':'계정 조회 장애',
           'one_tick_profit':'짧은 목표 수익 실현','continuation_value':'보유 기대값 소멸',
           'take_profit':'지정가 익절 체결','defend':'공정가 하회 방어 청산','stop':'손절 거리 도달','brake':'선행거래소 하락 지속으로 조기 청산',
           'dust':'최소 주문액 미만 잔량 이월','take_rejected':'익절 주문 거절 후 시장가 청산'}
HALTS = {'DAILY_LOSS':'일일 손실 제한','ORDER_RECONCILIATION':'주문 접수·체결 결과 미확정',
         'UNTRADEABLE_PARTIAL':'최소 주문액 미만 잔량','INVENTORY_UNAVAILABLE':'C 잔량과 계좌 가용수량 불일치',
         'EXIT_REJECTED':'청산 주문 거절','UNJOURNALED_TRACK_C_ORDER':'장부에 없는 C 주문 발견'}


def payload(kind, head, fields=None, lines=None, *, t_ms=None):
    return dict(track='C',kind=kind,head='C · '+head,fields=fields or [],lines=lines or [],
                ctx='코인원 현물 · '+stamp(t_ms or time.time()*1000)+' · 계기판 KST 당일')


def fill_payload(fact):
    q,gross = number(fact['qty']),number(fact['gross'])
    kind = '진입' if fact['buy'] else '손절' if fact['role']=='protect' else '부분청산'
    return payload(kind,fact['coin']+' 현물',[
        ['체결','매수 · 지정가' if fact['buy'] else '매도 · 보호 주문' if fact['role']=='protect' else '매도 · 지정가 익절' if fact['role']=='take' else '매도 · 시장가'],
        ['수량 · 평균가',qty(q)+'개 @ '+krw(gross/q)],['수수료',krw(fact['fee'])],
        ['체결 순손익',krw(fact['pnl'],True,decimals=1)],['남은 수량',qty(fact['remaining'])+'개']],t_ms=fact['t_ms'])


def fact_payload(fact):
    kind = fact['type']
    if kind == 'close':
        c = fact['campaign']; reason = c['exit_reason']
        if not number(c['sold']): return None  # Pure dust carry has no sale to notify.
        label = '손절' if reason in ('premise','stop','exchange_stop','daily_loss') else '부분청산' if number(c['qty']) else '전량청산'
        fields = [['청산 원인',REASONS.get(reason,'청산 조건 충족')],['매도 수량',qty(c['sold'])+'개'],
                  ['캠페인 순손익',krw(c['net'],True,decimals=1)],['남은 수량',qty(c['qty'])+'개']]
        if fact.get('sell_gross') is not None and number(c['sold'])>0:
            fields.append(['평균 매도가',krw(number(fact['sell_gross'])/number(c['sold']))])
        return payload(label,c['coin']+' 현물',fields,t_ms=fact['t_ms'])
    if kind == 'scan':
        return payload('종목변경','감시 종목 갱신',lines=['현재: '+', '.join(fact['new'])],t_ms=fact['t_ms'])
    if kind == 'START':
        return payload('복구','매매 엔진 재기동',lines=['저장된 주문과 재고를 확인하며 시세 신호를 다시 준비합니다.'],t_ms=fact['t_ms'])
    if kind == 'EXTERNAL_CAPITAL':
        return payload('정산','외부 자본 변동',[['변동',krw(fact['body']['external_delta'],True)],['원화 잔액',krw(fact['body']['balance'])]],
                       lines=['다음 주문의 복리 자본에 반영합니다. 매매 손익에는 포함하지 않습니다.'],t_ms=fact['t_ms'])
    if kind == 'HALT':
        message = HALTS.get(fact['body'].get('reason'),'엔진 상태 확인 필요')
    elif kind == 'ORDER_UNCERTAIN':
        message = '주문 응답 미확정 · 기존 주문을 재조회합니다.'
    elif kind == 'ORDER_REJECTED':
        message = '거래소가 주문을 거절했습니다.'
    elif kind == 'API_ERROR':
        message = '계정 API 조회 장애 · 신규 진입과 보유 재고 상태를 확인합니다.'
    else:
        return None
    return payload('이상',(fact.get('coin')+' · ' if fact.get('coin') else '')+'운영 확인',lines=[message],t_ms=fact['t_ms'])
