"""Non-destructive tape capacity accounting and a forward recording budget."""
import datetime as dt
from pathlib import Path
import shutil
import statistics
import time

GIB = 1024**3


def capacity(directory, public_limit, *, now=None, horizon_hours=240, reserve=GIB):
    now = time.time() if now is None else now
    directory = Path(directory)
    groups = {}
    for name in ('public', 'leaders')+ (('observations',) if (directory/'observations').exists() else ()):
        sizes, complete = [], []
        for p in (directory/name).glob('*.jsonl.gz'):
            size = p.stat().st_size
            sizes.append(size)
            try:
                start = dt.datetime.strptime(p.name[:11], '%Y%m%d-%H').replace(tzinfo=dt.timezone.utc).timestamp()
            except ValueError:
                continue
            if start+3600 <= now and start >= now-7*3600:
                complete.append((start, size))
        recent = [size for _,size in sorted(complete)[-6:]]
        groups[name] = dict(bytes=sum(sizes), completed_hours=len(recent),
                            bytes_per_hour=statistics.mean(recent) if recent else None)
    disk = shutil.disk_usage(directory)
    public_rate = groups['public']['bytes_per_hour']
    rates = [groups[k]['bytes_per_hour'] for k in groups]
    total_rate = sum(rates) if all(v is not None for v in rates) else None
    free_hours = max(0, disk.free-reserve)/total_rate if total_rate else None
    cap_hours = max(0, public_limit-groups['public']['bytes'])/public_rate if public_rate else None
    known = [h for h in (free_hours,cap_hours) if h is not None]
    remaining = min(known) if known else None
    return dict(ok=groups['public']['bytes'] < public_limit and disk.free > reserve,
                free_bytes=disk.free, total_bytes=disk.total, reserve_bytes=reserve,
                public_limit_bytes=public_limit, tapes=groups, estimated_remaining_hours=remaining,
                forecast_horizon_hours=horizon_hours, forecast_shortfall=remaining is not None and remaining < horizon_hours,
                forecast_note='recent completed-hour rate; excludes other applications, ledger and backups')
