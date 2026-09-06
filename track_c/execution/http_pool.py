"""Bounded persistent Coinone HTTPS connections; never retry a signed request."""
import http.client
import json
import queue
import re
import threading
import time
from urllib.parse import urlsplit

from track_c.execution.coinone import CoinoneError


class HTTPSPool:
    def __init__(self, size=4, *, factory=http.client.HTTPSConnection):
        self.factory = factory
        self.slots = queue.LifoQueue(size)
        for _ in range(size): self.slots.put(None)
        self.lock = threading.Lock()
        self.metrics = dict(requests=0, connections=0, failures=0, last_ms=0., max_ms=0.)

    def __call__(self, request, timeout):
        url = urlsplit(request.full_url)
        if url.scheme != 'https' or url.hostname != 'api.coinone.co.kr' or url.port not in (None,443) or url.username or url.password:
            raise CoinoneError('transport origin refused')
        started = time.monotonic()
        try: connection = self.slots.get(timeout=timeout)
        except queue.Empty: raise CoinoneError('transport pool deadline') from None
        keep = False
        try:
            if connection is None:
                connection = self.factory(url.hostname, timeout=timeout)
                with self.lock: self.metrics['connections'] += 1
            connection.timeout = timeout
            if connection.sock: connection.sock.settimeout(timeout)
            path = url.path + ('?'+url.query if url.query else '')
            # A closed pooled connection is discarded on error, never retried.
            connection.request(request.get_method(), path, body=request.data, headers=dict(request.header_items()))
            response = connection.getresponse()
            raw = response.read(2_000_001)
            if len(raw)>2_000_000: raise CoinoneError('response too large')
            keep = not response.will_close
            if 300 <= response.status < 400: raise CoinoneError('redirect refused')
            if response.status >= 400: raise CoinoneError('HTTP '+str(int(response.status)))
            value = json.loads(raw)
            if not isinstance(value,dict): raise CoinoneError('invalid response envelope')
            if value.get('result')!='success' or str(value.get('error_code'))!='0':
                code = str(value.get('error_code',''))
                code = code if re.fullmatch(r'[0-9]{1,6}',code) else 'unknown'
                raise CoinoneError('Coinone API error '+code, code=int(code) if code.isdigit() else None)
            return value
        except (OSError, ValueError, http.client.HTTPException):
            keep = False
            with self.lock: self.metrics['failures'] += 1
            raise CoinoneError('network or response decoding failure') from None
        finally:
            if not keep and connection is not None:
                connection.close()
                connection = None
            self.slots.put(connection)
            elapsed = (time.monotonic()-started)*1000
            with self.lock:
                self.metrics['requests'] += 1
                self.metrics['last_ms'] = elapsed
                self.metrics['max_ms'] = max(elapsed,self.metrics['max_ms'])

    def report(self):
        with self.lock: return dict(self.metrics)

    def close(self):
        while True:
            try: connection = self.slots.get_nowait()
            except queue.Empty: break
            if connection is not None: connection.close()
