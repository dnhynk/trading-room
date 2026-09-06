"""Shared API-window budgets for simultaneous campaigns (below venue ceilings)."""
from collections import deque
import threading
import time
from urllib.parse import urlsplit
from track_c.execution.coinone import check_before_send


class Transport:
    def __init__(self,send,*,clock=time.monotonic,sleep=time.sleep):
        self.send,self.clock,self.sleep=send,clock,sleep
        self.lock=threading.Lock(); self.history={k:deque() for k in ('order','private','public')}
        self.wait_seconds=0.
    def __call__(self,request,timeout):
        path=urlsplit(request.full_url).path
        group='order' if path in ('/v2.1/order','/v2.1/order/cancel') else 'private' if path.startswith('/v2.1/') else 'public'
        limit,window={'order':(35,1.),'private':(70,1.),'public':(1100,60.)}[group]
        while True:
            with self.lock:
                now=self.clock(); history=self.history[group]
                while history and history[0]<=now-window: history.popleft()
                if len(history)<limit: history.append(now); break
                delay=max(.001,history[0]+window-now)
            self.sleep(delay); self.wait_seconds+=delay
        check_before_send(request)
        return self.send(request,timeout)
