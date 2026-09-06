"""Read recorded Coinone messages without changing receive-time ordering."""
import gzip
import json

def coinone_rows(paths, quality=None):
    """Yields (recv_ms, message) from recorder files (object or string message)."""
    for path in paths:
        opener = gzip.open if str(path).endswith('.gz') else open
        with opener(path, 'rt', encoding='utf-8') as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    recv = int(row.get('received_ms', row.get('recv_ms')))
                    msg = row['message']
                    msg = json.loads(msg) if isinstance(msg, str) else msg
                except (ValueError, KeyError, TypeError):
                    if quality is not None: quality['malformed_coinone_rows'] += 1
                    continue
                if msg.get('response_type') == 'DATA':
                    yield recv, msg
