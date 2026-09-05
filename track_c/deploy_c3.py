"""Flat-guarded C3 rollout on the authorized AWS instance. No key packaging.

install : stage code, stop the standalone leader recorder and the C2 engine only while
          flat (same command as the guard), back up ledger/config/unit, install
          config-c3.json (observe mode) and start track_c.c3_runner.
go-live : flat guard, switch config to live/funding_confirmed, remove the audit PAUSE, restart.
status  : engine unit, status.json (fair values, selection, campaigns, leaders), notifier.
"""
import argparse
import json
from pathlib import Path
import shlex
import time

from .deploy import BASE, DEFAULT_AWS, DEFAULT_KEY, ROOT, remote, stage, verify

PAUSE_OWNER = 'c3-audit-20260905\n'
ENGINE = 'trading-room-c.service'
LEADERS = 'trading-room-c-leaders.service'

GUARD = f'''
import json,pathlib,sqlite3,subprocess,sys,time
base=pathlib.Path({BASE!r}).resolve(); assert str(base)=={BASE!r}
def unit(name):
    r=subprocess.run(['systemctl','show',name,'-p','ActiveState','-p','MainPID','-p','WorkingDirectory','-p','NRestarts','-p','UnitFileState'],capture_output=True,text=True,check=True)
    return dict(line.split('=',1) for line in r.stdout.splitlines() if '=' in line)
def flat():
    with sqlite3.connect((base/'data/ledger.sqlite').as_uri()+'?mode=ro',uri=True) as db: state=json.loads(db.execute('select body from state where id=1').fetchone()[0])
    assert not state.get('campaign') and not state.get('campaigns') and not [o for o in state['orders'].values() if o['status'] not in ('FILLED','CANCELED','NOT_TRIGGERED_CANCELED','CANCELED_NO_ORDER','CANCELED_LIMIT_PRICE_EXCEED','CANCELED_UNDER_PRODUCT_UNIT','REJECTED')], 'C exposure exists'
    return state
'''


def status(key):
    code = GUARD + f'''
report=dict(t_ms=int(time.time()*1000),engine=unit({ENGINE!r}),leaders_service=unit({LEADERS!r}),notifier=unit('trading-room-c-notify.service'))
for name,path in [('status',base/'data/status.json'),('notifications',base/'data/notifications/status.json'),('leaders',base/'data/leaders/status.json')]:
    report[name]=json.loads(path.read_text()) if path.exists() else None
report['entry_paused']=(base/'data/PAUSE').exists()
report['config']={{k:v for k,v in json.loads((base/'config.json').read_text()).items() if k in ('policy','mode','funding_confirmed','coins','notional_krw')}}
s=report['status'] or {{}}
report['summary']=dict(policy=s.get('policy'),mode=s.get('mode'),connected=s.get('connected'),private=s.get('private_connected'),halt=s.get('halt'),
    capital=s.get('capital_krw'),positions=list((s.get('positions') or {{}}).keys()),fair=s.get('fair'),selection={{c:v.get('reason') for c,v in (s.get('selection') or {{}}).items()}},
    residuals=s.get('residuals'),counts=s.get('counts'),leaders={{v:dict(connected=x.get('connected'),coins=len(x.get('coins',[]))) for v,x in ((s.get('leaders') or {{}}).get('venues') or {{}}).items()}})
print(json.dumps(report))
'''
    return json.loads(remote(key, 'python3 -', code.encode()))


def install(aws=DEFAULT_AWS, key=DEFAULT_KEY):
    result = stage(aws, key)
    release = result['release']
    tests = 'track_c.test_c3 track_c.test_runtime track_c.test_portfolio track_c.test_notices track_c.test_coinone'
    prep = f'''
import pathlib,subprocess,json
base=pathlib.Path({BASE!r}); release=pathlib.Path({release!r})
subprocess.run([str(base/'.venv/bin/python'),'-m','pip','install','--disable-pip-version-check','--no-input','-r',str(release/'track_c/requirements.txt')],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
r=subprocess.run([str(base/'.venv/bin/python'),'-m','unittest']+{tests.split()!r},cwd=release,capture_output=True,text=True)
if r.returncode:
    print(r.stdout); print(r.stderr); raise SystemExit(1)
print(json.dumps(dict(tests_passed=True)))
'''
    result.update(json.loads(remote(key, 'python3 -', prep.encode())))
    unit = f'''[Unit]
Description=Track C3 Coinone fair-value rule portfolio
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=ubuntu
WorkingDirectory={release}
ExecStart={BASE}/.venv/bin/python -u -m track_c.c3_runner --config {BASE}/config.json
Restart=on-failure
RestartSec=5
TimeoutStopSec=120
KillSignal=SIGTERM
Environment=OPENBLAS_NUM_THREADS=1
Environment=OMP_NUM_THREADS=1
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths={BASE}/data
ReadOnlyPaths=/home/ubuntu/arbitrage/.env
MemoryMax=512M
CPUQuota=100%
[Install]
WantedBy=multi-user.target
'''
    script = GUARD + f'''
release=pathlib.Path({release!r})
state=flat()
backup=base/'data'/'migration-c3'/release.name; backup.mkdir(parents=True,exist_ok=False)
(backup/'config-before.json').write_text((base/'config.json').read_text())
(backup/'unit-before.service').write_text(pathlib.Path('/etc/systemd/system',{ENGINE!r}).read_text())
with sqlite3.connect(base/'data/ledger.sqlite') as source, sqlite3.connect(backup/'ledger-before.sqlite') as target: source.backup(target)
# Guard and stop share this command; a failed guard cannot fall through to a stop.
flat(); subprocess.run(['systemctl','disable','--now',{LEADERS!r}],check=True,capture_output=True)
subprocess.run(['systemctl','stop',{ENGINE!r}],check=True)
flat()
newcfg=json.loads((release/'track_c/config-c3.json').read_text()); assert newcfg['policy']=='rule' and newcfg['mode']=='observe' and not newcfg['funding_confirmed']
(base/'config.json').write_text(json.dumps(newcfg,indent=2)+'\\n')
pathlib.Path('/etc/systemd/system',{ENGINE!r}).write_text({unit!r})
for p in [base/'config.json']: subprocess.run(['chown','ubuntu:ubuntu',str(p)],check=True)
subprocess.run(['systemctl','daemon-reload'],check=True)
subprocess.run(['systemctl','enable',{ENGINE!r}],check=True,capture_output=True)
subprocess.run(['systemctl','start',{ENGINE!r}],check=True)
time.sleep(40)
print(json.dumps(dict(service_started=True,mode='observe',previous_version=state['version'],leaders_service_stopped=True)))
'''
    result.update(json.loads(remote(key, 'cd ' + shlex.quote(release) + ' && sudo -n ' + BASE + '/.venv/bin/python -', script.encode())))
    result['status'] = status(key)
    path = ROOT / 'logs/track-c/c3-deployment-latest.json'
    path.write_text(json.dumps(result, indent=2) + '\n')
    return result


def go_live(key=DEFAULT_KEY):
    script = GUARD + f'''
pause=base/'data/PAUSE'
cfg=json.loads((base/'config.json').read_text()); assert cfg['policy']=='rule'
s=json.loads((base/'data/status.json').read_text())
assert time.time()*1000-s['t_ms']<60000 and s['connected'] and s['private_connected'] and not s['halt'] and s['storage_ok'], 'engine not healthy'
assert any(v for v in (s.get('fair') or {{}}).values()), 'no fair value yet'
leaders=(s.get('leaders') or {{}}).get('venues') or {{}}
assert leaders and all(v.get('connected') for v in leaders.values()), 'leader feeds not connected'
state=flat()
sys.path.insert(0,subprocess.run(['systemctl','show',{ENGINE!r},'--value','-p','WorkingDirectory'],capture_output=True,text=True,check=True).stdout.strip())
from track_c.coinone import CoinoneReadOnly,Credentials
client=CoinoneReadOnly(Credentials.read(cfg['env_path'],profile=cfg['credential_profile']))
assert not client.active_orders(), 'account has outstanding orders'
for coin in cfg['coins']: assert not any(float(v) for v in client.fees(coin).values()), 'nonzero fee: '+coin
cfg.update(mode='live',funding_confirmed=True)
(base/'config.json').write_text(json.dumps(cfg,indent=2)+'\\n')
if pause.exists():
    assert pause.read_text()=={PAUSE_OWNER!r}, 'pause not owned by this rollout'
    pause.unlink()
subprocess.run(['systemctl','restart',{ENGINE!r}],check=True)
time.sleep(40)
s=json.loads((base/'data/status.json').read_text())
print(json.dumps(dict(live=True,mode=s.get('mode'),connected=s.get('connected'),private=s.get('private_connected'),capital=s.get('capital_krw'),paused=pause.exists())))
'''
    out = json.loads(remote(key, 'sudo -n ' + BASE + '/.venv/bin/python -', script.encode()))
    out['status'] = status(key)
    path = ROOT / 'logs/track-c/c3-golive-latest.json'
    path.write_text(json.dumps(out, indent=2) + '\n')
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['install', 'go-live', 'status'])
    args = p.parse_args()
    verify(DEFAULT_AWS)
    result = install() if args.command == 'install' else go_live() if args.command == 'go-live' else status(DEFAULT_KEY)
    (ROOT / 'logs/track-c/c3-health-latest.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False)[:3000])


if __name__ == '__main__':
    main()
