"""Flat-guarded C2 rollout on the authorized AWS instance. No key packaging."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import time

from .deploy import BASE,DEFAULT_AWS,DEFAULT_KEY,ROOT,remote,stage,verify
from .estimation import read
from .dataset import public_contracts

PAUSE_OWNER='quantitative-rebuild-20260905\n'


def status(key):
    code=f'''
import json,pathlib,sqlite3,subprocess,time
base=pathlib.Path({BASE!r})
def unit(name):
    r=subprocess.run(['systemctl','show',name,'-p','ActiveState','-p','MainPID','-p','WorkingDirectory','-p','NRestarts','-p','MemoryCurrent','-p','UnitFileState'],capture_output=True,text=True,check=True)
    return dict(line.split('=',1) for line in r.stdout.splitlines() if '=' in line)
report=dict(t_ms=int(time.time()*1000),engine=unit('trading-room-c.service'),notifier=unit('trading-room-c-notify.service'),worker=unit('trading-room-c-model.service'),timer=unit('trading-room-c-model.timer'))
for name,path in [('status',base/'data/status.json'),('worker_status',base/'data/models/worker-status.json'),('notifications',base/'data/notifications/status.json')]:
    report[name]=json.loads(path.read_text()) if path.exists() else None
report['entry_paused']=(base/'data/PAUSE').exists()
print(json.dumps(report))
'''
    return json.loads(remote(key,'python3 -',code.encode()))


def install(model_path,aws=DEFAULT_AWS,key=DEFAULT_KEY):
    model=read(model_path,int(time.time()*1000)); result=stage(aws,key); release=result['release']
    # The immutable release contains code/config only; upload the explicit model.
    remote(key,'cat > '+shlex.quote(release+'/initial-model.json'),Path(model_path).read_bytes())
    meta=json.loads((Path(model_path).parent/'dataset.json').read_text())
    contracts,units=public_contracts(meta['spec']['contracts'])
    captures=[dict(coin=c,available_ms=max(r['available_ms'],units.get(c,{}).get('available_ms',0)),contract={k:v for k,v in r.items() if k not in ('history','available_ms')},
                   units=units.get(c,{}).get('rows',[dict(range_min='0',price_unit=r['price_unit'])]),tick_source='frozen_public_metadata') for c,r in contracts.items()]
    remote(key,'cat > '+shlex.quote(release+'/initial-contracts.json'),json.dumps(dict(captures=captures,markets=captures)).encode())
    tests='bot.test_notify track_c.test_coinone track_c.test_runtime track_c.test_notices track_c.test_quantitative track_c.test_portfolio track_c.test_research'
    prep=f'''
import pathlib,subprocess,json,hashlib
base=pathlib.Path({BASE!r}); release=pathlib.Path({release!r})
subprocess.run([str(base/'.venv/bin/python'),'-m','pip','install','--disable-pip-version-check','--no-input','-r',str(release/'track_c/requirements.txt')],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
r=subprocess.run([str(base/'.venv/bin/python'),'-m','unittest']+{tests.split()!r},cwd=release,capture_output=True,text=True)
if r.returncode:
    print(r.stdout); print(r.stderr); raise SystemExit(1)
assert hashlib.sha256((release/'initial-model.json').read_bytes()).hexdigest()=={hashlib.sha256(Path(model_path).read_bytes()).hexdigest()!r}
print(json.dumps(dict(tests_passed=True)))
'''
    result.update(json.loads(remote(key,'python3 -',prep.encode())))
    unit=f'''[Unit]
Description=Track C2 Coinone quantitative portfolio
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=ubuntu
WorkingDirectory={release}
ExecStart={BASE}/.venv/bin/python -u -m track_c.quant_runner --config {BASE}/config.json
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
    worker=f'''[Unit]
Description=Track C2 frozen-data model refresh
After=network-online.target trading-room-c.service
[Service]
Type=oneshot
User=ubuntu
WorkingDirectory={release}
ExecStart={BASE}/.venv/bin/python -u -m track_c.model_worker --config {BASE}/config.json
Environment=OPENBLAS_NUM_THREADS=1
Environment=OMP_NUM_THREADS=1
Nice=15
CPUQuota=50%
MemoryMax=1100M
TimeoutStartSec=1200
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths={BASE}/data/models {BASE}/data/research
ReadOnlyPaths={BASE}/data/public {BASE}/data/contracts {BASE}/data/ledger.sqlite
'''
    timer='''[Unit]
Description=Refresh C2 model from completed observations
[Timer]
OnBootSec=10min
OnUnitInactiveSec=30min
Unit=trading-room-c-model.service
[Install]
WantedBy=timers.target
'''
    script=f'''
import json,pathlib,sqlite3,subprocess,sys,time
base=pathlib.Path({BASE!r}).resolve(); release=pathlib.Path({release!r}); sys.path.insert(0,str(release))
from track_c.coinone import CoinoneReadOnly,Credentials
from track_c.estimation import read
assert str(base)=='/home/ubuntu/trading-room-c'
pause=base/'data/PAUSE'; assert pause.read_text()=={PAUSE_OWNER!r}, 'pause is not owned by this rebuild'
cfg=json.loads((base/'config.json').read_text()); client=CoinoneReadOnly(Credentials.read(cfg['env_path'],profile=cfg['credential_profile']))
def flat():
    with sqlite3.connect((base/'data/ledger.sqlite').as_uri()+'?mode=ro',uri=True) as db: state=json.loads(db.execute('select body from state where id=1').fetchone()[0])
    assert not state.get('campaign') and not state.get('campaigns') and not state['orders'], 'C exposure exists'
    assert not client.active_orders(), 'account has outstanding orders'
    return state
state=flat()
old_unit=pathlib.Path('/etc/systemd/system/trading-room-c.service').read_text()
backup=base/'data'/'migration-c2'/release.name; backup.mkdir(parents=True,exist_ok=False)
(backup/'config-before.json').write_text(json.dumps(cfg,indent=2))
(backup/'unit-before.service').write_text(old_unit)
if (base/'data/models/current.json').exists(): (backup/'model-before.json').write_text((base/'data/models/current.json').read_text())
with sqlite3.connect(base/'data/ledger.sqlite') as source, sqlite3.connect(backup/'ledger-before.sqlite') as target: source.backup(target)
# Guard and stop share this command; a failed guard cannot fall through to kill.
flat(); subprocess.run(['systemctl','stop','trading-room-c-model.timer','trading-room-c-model.service'],check=True)
subprocess.run(['systemctl','stop','trading-room-c.service'],check=True)
flat()
newcfg=json.loads((release/'track_c/config-c2.json').read_text()); assert newcfg['capital_mode']=='account_equity'
(base/'config.json').write_text(json.dumps(newcfg,indent=2)+'\\n')
models=base/'data/models'; models.mkdir(exist_ok=True); (base/'data/research').mkdir(exist_ok=True)
(base/'data/contracts').mkdir(exist_ok=True)
seed=base/'data/contracts/000-initial-frozen.json'; seed.write_text((release/'initial-contracts.json').read_text())
doc=read(release/'initial-model.json',int(time.time()*1000)); content=json.dumps(doc,indent=2)+'\\n'
version=models/(doc['digest']+'.json')
if version.exists(): assert json.loads(version.read_text())==doc
else: version.write_text(content)
tmp=models/'current.tmp'; tmp.write_text(content); tmp.replace(models/'current.json')
for p in [base/'config.json',models,base/'data/research',base/'data/contracts',seed,models/'current.json',version]: subprocess.run(['chown','ubuntu:ubuntu',str(p)],check=True)
for name,content in [('trading-room-c.service',{unit!r}),('trading-room-c-model.service',{worker!r}),('trading-room-c-model.timer',{timer!r})]:
    pathlib.Path('/etc/systemd/system',name).write_text(content)
subprocess.run(['systemctl','daemon-reload'],check=True)
subprocess.run(['systemctl','enable','trading-room-c.service','trading-room-c-model.timer'],check=True,capture_output=True)
subprocess.run(['systemctl','start','trading-room-c.service'],check=True)
assert pause.read_text()=={PAUSE_OWNER!r}
print(json.dumps(dict(service_started=True,entry_paused=True,model=doc['digest'],previous_version=state['version'])))
'''
    result.update(json.loads(remote(key,'cd '+shlex.quote(release)+' && sudo -n '+BASE+'/.venv/bin/python -',script.encode())))
    path=ROOT/'logs/track-c/quant-deployment-latest.json'; path.write_text(json.dumps(result,indent=2)+'\n')
    return result


def resume(key):
    script=f'''
import pathlib,json,sqlite3,time,sys,subprocess
base=pathlib.Path({BASE!r}); pause=base/'data/PAUSE'
release=subprocess.run(['systemctl','show','trading-room-c.service','--value','-p','WorkingDirectory'],capture_output=True,text=True,check=True).stdout.strip()
sys.path.insert(0,release)
from track_c.coinone import CoinoneReadOnly,Credentials
from track_c.estimation import read
cfg=json.loads((base/'config.json').read_text()); assert cfg['policy']=='quantitative' and cfg['mode']=='live'
s=json.loads((base/'data/status.json').read_text())
assert time.time()*1000-s['t_ms']<60000 and s['connected'] and s['private_connected'] and not s['halt'] and not s['model_issue'] and s['storage_ok']
assert s['model']['digest']==read(base/'data/models/current.json',int(time.time()*1000))['digest']
assert s['selection'] and any(v.get('reason')!='history_warmup' for v in s['selection'].values())
with sqlite3.connect((base/'data/ledger.sqlite').as_uri()+'?mode=ro',uri=True) as db: state=json.loads(db.execute('select body from state where id=1').fetchone()[0])
assert state['version']==3 and not state['campaigns'] and not state['orders']
client=CoinoneReadOnly(Credentials.read(cfg['env_path'],profile=cfg['credential_profile']))
assert not client.active_orders()
assert pause.read_text()=={PAUSE_OWNER!r}
pause.unlink()
subprocess.run(['systemctl','start','trading-room-c-model.timer'],check=True)
print(json.dumps(dict(resumed=True,model=s['model']['digest'],symbols=list(s['markets']),capital_krw=s['capital_krw'])))
'''
    return json.loads(remote(key,'sudo -n '+BASE+'/.venv/bin/python -',script.encode()))


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('command',choices=['install','status','resume']); p.add_argument('--model',type=Path); args=p.parse_args()
    verify(DEFAULT_AWS)
    result=install(args.model) if args.command=='install' else resume(DEFAULT_KEY) if args.command=='resume' else status(DEFAULT_KEY)
    (ROOT/'logs/track-c/quant-health-latest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result))


if __name__=='__main__': main()
