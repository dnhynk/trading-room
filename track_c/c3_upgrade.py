"""Stage/test -> pause and switch at flat -> verify/warm up -> resume C3.

Each command is bounded. An existing operator PAUSE is preserved. State is never
rolled back. The v3 profile updates only the five v3 settings; btc-only updates
only the trading/recording universe and restarts its evaluation/display window.
All other live settings are preserved. Credentials are never copied.
"""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path

from .deploy import BASE, ROOT, DEFAULT_AWS, DEFAULT_KEY, remote, stage, verify
from .deploy_c3 import GUARD, ENGINE, status

RECORD = ROOT / 'logs/track-c/c3-upgrade-staged.json'
V3_CONFIG_KEYS = ('momentum_window_s','momentum_recent_s','momentum_veto_ticks','momentum_decel_share','flow_gate')
CONFIG_PROFILES = {'v3': V3_CONFIG_KEYS, 'btc-only': ('coins','record_coins')}
EVALUATION = ROOT / 'track_c/evaluation-c3.json'


def upgraded_config(current, desired, profile='v3'):
    if current.get('policy') != 'rule' or desired.get('policy') != 'rule':
        raise ValueError('C3 rule configuration required')
    if profile not in CONFIG_PROFILES:
        raise ValueError('unknown C3 upgrade profile')
    if profile == 'btc-only':
        records = desired.get('record_coins')
        if (desired.get('coins') != ['BTC'] or not isinstance(records,list) or
                len(records) != len(set(records)) or 'BTC' in records or
                not {'ETH','XRP','SOL'} <= set(records) or
                set(current['coins']+current.get('record_coins',[])) != {'BTC',*records}):
            raise ValueError('BTC-only must retain the full recorded universe')
    return dict(current, **{k:desired[k] for k in CONFIG_PROFILES[profile]})


def activation_records(previous, cfg, start_ms, release):
    """Pure reset of the prospective clock and display scope; no risk/accounting reset."""
    if previous.get('schema') != 2 or cfg.get('coins') != ['BTC'] or start_ms <= previous['start_ms']:
        raise ValueError('a new BTC-only activation is required')
    protocol = json.loads(json.dumps(previous))
    when = dt.datetime.fromtimestamp(start_ms/1000, dt.timezone(dt.timedelta(hours=9)))
    end_ms = start_ms + int(previous['days'])*86400000
    protocol.update(name='C3 v3 BTC-only activation-anchored prospective checkpoint',
                    registered_at_utc=when.astimezone(dt.timezone.utc).isoformat(),
                    start_ms=start_ms,end_ms=end_ms,start_kst=when.isoformat(),
                    end_kst_exclusive=dt.datetime.fromtimestamp(end_ms/1000,when.tzinfo).isoformat(),
                    development_before_ms=start_ms,
                    config={k:cfg.get(k) for k in previous['config']},
                    previous_registration=dict(name=previous['name'],start_ms=previous['start_ms'],
                                               end_ms=previous['end_ms'],coins=previous['config']['coins']))
    baseline = dict(day=when.date().isoformat(),start_ms=start_ms,coins=['BTC'],rule=protocol['rule'],release=release)
    return protocol, baseline


def prepare(profile='v3'):
    if profile not in CONFIG_PROFILES:
        raise ValueError('unknown C3 upgrade profile')
    template = json.loads(EVALUATION.read_text(encoding='utf-8')) if profile == 'btc-only' else None
    if template:
        assert all(hashlib.sha256((ROOT/'track_c'/k).read_bytes()).hexdigest()==v for k,v in template['source'].items()), 'evaluation source changed'
    result = stage(DEFAULT_AWS, DEFAULT_KEY)
    release = result['release']
    code = f'''
import json,pathlib,subprocess,hashlib,sys,tempfile
release=pathlib.Path({release!r})
manifest=json.loads((release/'manifest.json').read_text())
assert all(hashlib.sha256((release/k).read_bytes()).hexdigest()==v for k,v in manifest.items())
p=subprocess.run([{BASE+'/.venv/bin/python'!r},'-m','unittest','discover','-s','track_c','-t','.'],cwd=release,capture_output=True,text=True)
if p.returncode:
    print(p.stdout); print(p.stderr); raise SystemExit(1)
sys.path.insert(0,str(release))
from track_c.c3_upgrade import upgraded_config,CONFIG_PROFILES
from track_c.settings import load
before=(pathlib.Path({BASE!r})/'config.json').read_bytes(); current=json.loads(before)
updated=upgraded_config(current,json.loads((release/'track_c/config-c3.json').read_text()),{profile!r})
with tempfile.TemporaryDirectory() as directory:
    path=pathlib.Path(directory)/'config.json'; path.write_text(json.dumps(updated)); load(path)
patch={{k:updated[k] for k in CONFIG_PROFILES[{profile!r}] if current.get(k)!=updated[k]}}
print(json.dumps(dict(remote_tests_passed=True,config_patch=patch,config_before_sha256=hashlib.sha256(before).hexdigest())))
'''
    result.update(json.loads(remote(DEFAULT_KEY, 'python3 -', code.encode())))
    before = status(DEFAULT_KEY)
    result.update(previous_release=before['engine']['WorkingDirectory'], pause_owner='c3-audit-upgrade-'+result['digest']+'\n',profile=profile)
    if template:
        archive = EVALUATION.parent/'evaluations'/('c3-v3-'+str(template['start_ms'])+'.json')
        archive.parent.mkdir(exist_ok=True)
        old = EVALUATION.read_bytes()
        if archive.exists():
            assert archive.read_bytes() == old, 'previous evaluation archive differs'
        else:
            archive.write_bytes(old)
        result.update(evaluation_template=template,previous_evaluation_sha256=hashlib.sha256(old).hexdigest())
    RECORD.write_text(json.dumps(result, indent=2)+'\n')
    return result


def switch():
    result = json.loads(RECORD.read_text())
    assert result['remote_tests_passed'] and not result.get('switched')
    code = GUARD + f'''
import hashlib,os
release=pathlib.Path({result['release']!r}).resolve()
assert release.is_relative_to(base/'releases') and release!=base/'releases'
assert unit({ENGINE!r})['WorkingDirectory']=={result['previous_release']!r}, 'another rollout changed the engine'
manifest=json.loads((release/'manifest.json').read_text())
assert all(hashlib.sha256((release/k).read_bytes()).hexdigest()==v for k,v in manifest.items())
sys.path.insert(0,str(release))
from track_c.coinone import CoinoneReadOnly,Credentials
from track_c.rule import VERSION
from track_c.c3_upgrade import upgraded_config,CONFIG_PROFILES
cfg_bytes=(base/'config.json').read_bytes(); cfg=json.loads(cfg_bytes)
assert hashlib.sha256(cfg_bytes).hexdigest()=={result['config_before_sha256']!r}, 'live config changed since prepare'
profile={result.get('profile','v3')!r}
updated=upgraded_config(cfg,json.loads((release/'track_c/config-c3.json').read_text()),profile)
patch={{k:updated[k] for k in CONFIG_PROFILES[profile] if cfg.get(k)!=updated[k]}}
assert patch=={result['config_patch']!r}, 'staged settings changed'
cfg_after=(json.dumps(updated,indent=2)+'\\n').encode() if patch else cfg_bytes
assert cfg['policy']=='rule' and cfg['mode'] in ('observe','live')
assert not (base/'data/STOP').exists(), 'operator STOP is present'
client=CoinoneReadOnly(Credentials.read(cfg['env_path'],profile=cfg['credential_profile']))
pause=base/'data/PAUSE'; owner={result['pause_owner']!r}
if not pause.exists():
    with pause.open('x') as f: f.write(owner)
owned=pause.read_text()==owner
# Let an already-authorized request finish before checking flat; PAUSE is checked
# again inside submission. Keep all guards and the stop in this same command.
time.sleep(2)
state=None
for _ in range(25):
    try: state=flat(); break
    except AssertionError: time.sleep(1)
if state is None:
    print(json.dumps(dict(waiting_for_flat=True,pause_owned=owned))); raise SystemExit(0)
def exchange_flat(state):
    assert not [o for o in client.active_orders() if str(o.get('user_order_id','')).startswith('tc-')], 'C exchange orders still active'
    rows={{r['currency']:r for r in client.balances()}}
    for coin,r in state.get('residuals',{{}}).items():
        from decimal import Decimal as D
        actual=rows.get(coin,{{'available':'0','limit':'0'}})
        assert D(actual['available'])+D(actual['limit'])==D(r['qty']), 'residual ownership mismatch'
    return True
exchange_flat(state)
assert pause.exists() and (base/'config.json').read_bytes()==cfg_bytes
assert unit({ENGINE!r})['WorkingDirectory']=={result['previous_release']!r}
backup=base/'data'/'upgrade-c3'/release.name; backup.mkdir(parents=True,exist_ok=True)
unit_path=pathlib.Path('/etc/systemd/system',{ENGINE!r}); old=unit_path.read_text()
assert 'track_c.c3_runner' in old and ('WorkingDirectory='+{result['previous_release']!r}) in old
(backup/'unit-before.service').write_text(old)
(backup/'config-before.json').write_bytes(cfg_bytes)
flat(); subprocess.run(['systemctl','stop',{ENGINE!r}],check=True)
state=flat(); exchange_flat(state)
with sqlite3.connect((base/'data/ledger.sqlite').as_uri()+'?mode=ro',uri=True) as src, sqlite3.connect(backup/'ledger-before.sqlite') as target: src.backup(target)
if cfg_after!=cfg_bytes:
    target=base/'config.json'; attributes=target.stat(); temp=base/'config.json.c3-upgrade.tmp'
    with temp.open('xb') as f: f.write(cfg_after)
    os.chmod(temp,attributes.st_mode & 0o777); os.chown(temp,attributes.st_uid,attributes.st_gid)
    temp.replace(target)
unit_path.write_text(old.replace('WorkingDirectory='+{result['previous_release']!r},'WorkingDirectory='+str(release)))
subprocess.run(['systemctl','daemon-reload'],check=True)
subprocess.run(['systemctl','start',{ENGINE!r}],check=True)
assert (base/'config.json').read_bytes()==cfg_after
print(json.dumps(dict(switched=True,service_started=True,rule=VERSION,pause_owned=owned,config_sha256=hashlib.sha256(cfg_after).hexdigest(),backup=str(backup))))
'''
    out = json.loads(remote(DEFAULT_KEY, 'sudo -n '+BASE+'/.venv/bin/python -', code.encode()))
    result.update(out)
    RECORD.write_text(json.dumps(result, indent=2)+'\n')
    return result


def resume():
    result = json.loads(RECORD.read_text())
    assert result.get('switched')
    if result.get('profile') == 'btc-only':
        assert hashlib.sha256(EVALUATION.read_bytes()).hexdigest() == result['previous_evaluation_sha256'], 'local evaluation changed during rollout'
    code = GUARD + f'''
import hashlib,os
assert unit({ENGINE!r})['WorkingDirectory']=={result['release']!r}
assert hashlib.sha256((base/'config.json').read_bytes()).hexdigest()=={result['config_sha256']!r}
sys.path.insert(0,{result['release']!r})
from track_c.rule import VERSION,momentum_available
s=json.loads((base/'data/status.json').read_text())
cfg=json.loads((base/'config.json').read_text())
assert s.get('rule',{{}}).get('version')==VERSION and time.time()*1000-s['t_ms']<60000
assert s['rule']['coins']==cfg['coins'], 'status universe differs from live config'
assert s['connected'] and s['private_connected'] and not s['halt'] and s['storage_ok']
assert s['leaders']['storage_ok'] and all(v['connected'] for v in s['leaders']['venues'].values())
assert all(s.get('fair',{{}}).get(c) for c in s['rule']['coins']), 'fair values still warming up'
assert all(momentum_available(s['fair'][c].get('m30'),s['fair'][c].get('m10')) for c in s['rule']['coins']), 'momentum still warming up'
flat()
pause=base/'data/PAUSE'
assert pause.exists(), 'pause changed during rollout'
removed=False
resumed_at_ms=None
activation={{}}
if {result.get('pause_owned',False)!r}:
    assert pause.read_text()=={result['pause_owner']!r}, 'pause owner changed'
    resumed_at_ms=int(time.time()*1000)
    if {result.get('profile','v3')!r}=='btc-only':
        from track_c.c3_upgrade import activation_records
        template={result.get('evaluation_template')!r}
        assert all(hashlib.sha256((pathlib.Path({result['release']!r})/'track_c'/k).read_bytes()).hexdigest()==v for k,v in template['source'].items())
        evaluation,baseline=activation_records(template,cfg,resumed_at_ms,{result['release']!r})
        owner=(base/'data').stat()
        def write_owned(target,body):
            temp=target.with_name(target.name+'.c3-upgrade.tmp')
            with temp.open('xb') as f:
                f.write((json.dumps(body,indent=2)+'\\n').encode()); f.flush(); os.fsync(f.fileno())
            os.chmod(temp,0o600); os.chown(temp,owner.st_uid,owner.st_gid); temp.replace(target)
        folder=base/'data/evaluation'; folder.mkdir(exist_ok=True)
        os.chown(folder,owner.st_uid,owner.st_gid)
        immutable=folder/('c3-v3-btc-'+str(resumed_at_ms)+'.json')
        assert not immutable.exists()
        write_owned(immutable,evaluation)
        write_owned(folder/'evaluation-c3.json',evaluation)
        target=base/'data/notifications/baseline.json'
        backup=target.with_name('baseline-before-'+pathlib.Path({result['release']!r}).name+'.json')
        if target.exists() and not backup.exists():
            write_owned(backup,json.loads(target.read_text()))
        write_owned(target,baseline)
        activation=dict(evaluation=evaluation,baseline=baseline,evaluation_path=str(immutable),
                        evaluation_sha256=hashlib.sha256(immutable.read_bytes()).hexdigest())
        write_owned(base/'data/upgrade-c3'/pathlib.Path({result['release']!r}).name/'activation.json',activation)
    # Both new clocks/scopes are durable before the first BTC-only entry is possible.
    pause.unlink(); removed=True
print(json.dumps(dict(verified=True,resumed=removed,resumed_at_ms=resumed_at_ms,operator_pause_preserved=not removed,rule=VERSION,**activation)))
'''
    result.update(json.loads(remote(DEFAULT_KEY, 'sudo -n '+BASE+'/.venv/bin/python -', code.encode())))
    RECORD.write_text(json.dumps(result, indent=2)+'\n')
    if result.get('resumed') and result.get('evaluation'):
        temp=EVALUATION.with_suffix('.json.tmp')
        temp.write_text(json.dumps(result['evaluation'],indent=2)+'\n',encoding='utf-8')
        temp.replace(EVALUATION)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=('prepare','switch','resume','status'))
    p.add_argument('--profile', choices=tuple(CONFIG_PROFILES), default='v3', help='configuration scope for prepare')
    args = p.parse_args()
    verify(DEFAULT_AWS)
    result = dict(status=status(DEFAULT_KEY)['summary']) if args.command=='status' else prepare(args.profile) if args.command=='prepare' else globals()[args.command]()
    print(json.dumps(result))


if __name__ == '__main__':
    main()
