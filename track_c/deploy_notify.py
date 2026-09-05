"""Deploy only the C Slack reader. Never restart or reconfigure the trading engine."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import shlex
import tarfile
import time

from .deploy import BASE, DEFAULT_AWS, DEFAULT_KEY, ROOT, remote, verify

UNIT = 'trading-room-c-notify.service'


def bundle():
    names = ('bot/notify.py','bot/test_notify.py','track_c/notices.py','track_c/notify_relay.py','track_c/test_notices.py')
    manifest = {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in names}
    digest = hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest()[:12]
    data = io.BytesIO()
    with tarfile.open(fileobj=data,mode='w:gz') as archive:
        for name in names:
            archive.add(ROOT/name,arcname=name,recursive=False)
        body=json.dumps(manifest,sort_keys=True).encode()
        info=tarfile.TarInfo('manifest.json'); info.size=len(body)
        archive.addfile(info,io.BytesIO(body))
    return data.getvalue(),digest,manifest


def inspect(key, release=None):
    script = f'''
import json,pathlib,sqlite3,subprocess,time
base=pathlib.Path({BASE!r})
def service(unit):
    result=subprocess.run(['systemctl','show',unit,'-p','ActiveState','-p','UnitFileState','-p','MainPID','-p','NRestarts','-p','WorkingDirectory'],capture_output=True,text=True,check=True)
    return dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
status=base/'data/notifications/status.json'
report=dict(t_ms=int(time.time()*1000),engine=service('trading-room-c.service'),notifier=service({UNIT!r}),
            notification_status=json.loads(status.read_text()) if status.exists() else None)
ledger=base/'data/notifications/relay.sqlite'
if ledger.exists():
    db=sqlite3.connect(ledger.as_uri()+'?mode=ro',uri=True)
    report['queue_counts']=dict(db.execute('SELECT state,COUNT(*) FROM queue GROUP BY state').fetchall())
    report['sent_notices']=[dict(kind=json.loads(p)['kind'],sent_at=s) for p,s in db.execute("SELECT payload,sent_at FROM queue WHERE state='sent' ORDER BY id DESC LIMIT 5")]
    db.close()
print(json.dumps(report))
'''
    report=json.loads(remote(key,'python3 -',script.encode()))
    if release:
        preview = f'''
import json
from bot.notify import _blocks
from track_c.notices import Source
from track_c.notify_relay import heartbeat
import time
directory={BASE+'/data'!r}
state,status,_,_=Source(directory).read()
card=heartbeat(state,status,time.time(),boot=True)
print(json.dumps(dict(card=card,blocks=_blocks(**card,data_dir=directory)),ensure_ascii=False))
'''
        report['preview']=json.loads(remote(key,'cd '+shlex.quote(release)+' && '+BASE+'/.venv/bin/python -',preview.encode()))
    return report


def stage(aws, key):
    identity=verify(aws)
    before=inspect(key)
    if before['engine']['ActiveState']!='active':
        raise RuntimeError('C engine is not active; inspect before deploying notifications')
    data,digest,manifest=bundle()
    release=BASE+'/notifications/releases/'+time.strftime('%Y%m%d-%H%M%S')+'-'+digest
    remote(key,'umask 077; mkdir -p '+shlex.quote(release)+' && tar -xzf - -C '+shlex.quote(release),data)
    check=f'''
import hashlib,json,pathlib,subprocess
p=pathlib.Path({release!r}); manifest=json.loads((p/'manifest.json').read_text())
assert all(hashlib.sha256((p/k).read_bytes()).hexdigest()==v for k,v in manifest.items())
result=subprocess.run([{BASE+'/.venv/bin/python'!r},'-m','unittest','bot.test_notify','track_c.test_notices'],cwd=p,capture_output=True,text=True)
if result.returncode:
    print(result.stdout); print(result.stderr); raise SystemExit(1)
from bot.notify import _hook
_hook('/home/ubuntu/arbitrage/.env')
print(json.dumps(dict(verified_files=len(manifest),remote_tests_passed=True,existing_webhook_valid=True)))
'''
    checks=json.loads(remote(key,'cd '+shlex.quote(release)+' && '+BASE+'/.venv/bin/python -',check.encode()))
    result=dict(instance=identity,release=release,digest=digest,secrets_packaged=False,engine_before=before['engine'],**checks,service_started=False)
    path=ROOT/'logs/track-c/notification-deployment-latest.json'; path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    return result


def install(aws, key):
    result=stage(aws,key)
    release=result['release']
    unit=f'''[Unit]
Description=Track C Coinone Slack notifications (read-only engine ledger)
After=network-online.target trading-room-c.service
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory={release}
ExecStart={BASE}/.venv/bin/python -u -m track_c.notify_relay --data-dir {BASE}/data --env /home/ubuntu/arbitrage/.env
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
KillSignal=SIGTERM
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths={BASE}/data/notifications
ReadOnlyPaths=/home/ubuntu/arbitrage/.env
MemoryMax=128M
CPUQuota=25%

[Install]
WantedBy=multi-user.target
'''
    script=f'''
import pathlib,subprocess,json
base=pathlib.Path({BASE!r}).resolve()
assert str(base)=='/home/ubuntu/trading-room-c'
def pid():
    return subprocess.run(['systemctl','show','trading-room-c.service','-p','MainPID','--value'],check=True,capture_output=True,text=True).stdout.strip()
assert pid()=={result['engine_before']['MainPID']!r}, 'engine changed during staging'
target=base/'data/notifications'; target.mkdir(exist_ok=True)
subprocess.run(['chown','ubuntu:ubuntu',str(target)],check=True)
service=pathlib.Path('/etc/systemd/system/{UNIT}')
if service.exists():
    assert 'track_c.notify_relay' in service.read_text(), 'notification service ownership mismatch'
    service.with_suffix('.service.previous').write_text(service.read_text())
service.write_text({unit!r})
subprocess.run(['systemctl','daemon-reload'],check=True)
subprocess.run(['systemctl','enable',{UNIT!r}],check=True,capture_output=True)
subprocess.run(['systemctl','restart',{UNIT!r}],check=True)
assert pid()=={result['engine_before']['MainPID']!r}, 'engine PID changed unexpectedly'
print(json.dumps(dict(notifier_started=True,engine_pid_unchanged=True)))
'''
    result.update(json.loads(remote(key,'sudo -n python3 -',script.encode())))
    result['service_started']=True
    (ROOT/'logs/track-c/notification-deployment-latest.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('stage','install','status'))
    parser.add_argument('--aws',type=Path,default=DEFAULT_AWS)
    parser.add_argument('--ssh-key',type=Path,default=DEFAULT_KEY)
    args=parser.parse_args()
    if args.command=='status':
        verify(args.aws)
        meta=json.loads((ROOT/'logs/track-c/notification-deployment-latest.json').read_text())
        result=inspect(args.ssh_key,meta['release'])
        (ROOT/'logs/track-c/notification-health-latest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    else:
        result=(install if args.command=='install' else stage)(args.aws,args.ssh_key)
    print(json.dumps(result,ensure_ascii=True))


if __name__=='__main__':
    main()
