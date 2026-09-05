"""Stage/test -> pause and switch at flat -> verify/warm up -> resume the same C3 config.

Each command is bounded. An existing operator PAUSE is preserved. State is never
rolled back, credentials never copied, and the notification service is independent.
"""
import argparse
import hashlib
import json
from pathlib import Path

from .deploy import BASE, ROOT, DEFAULT_AWS, DEFAULT_KEY, remote, stage, verify
from .deploy_c3 import GUARD, ENGINE, status

RECORD = ROOT / 'logs/track-c/c3-upgrade-staged.json'


def prepare():
    result = stage(DEFAULT_AWS, DEFAULT_KEY)
    release = result['release']
    code = f'''
import json,pathlib,subprocess,hashlib
release=pathlib.Path({release!r})
manifest=json.loads((release/'manifest.json').read_text())
assert all(hashlib.sha256((release/k).read_bytes()).hexdigest()==v for k,v in manifest.items())
p=subprocess.run([{BASE+'/.venv/bin/python'!r},'-m','unittest','discover','-s','track_c','-t','.'],cwd=release,capture_output=True,text=True)
if p.returncode:
    print(p.stdout); print(p.stderr); raise SystemExit(1)
print(json.dumps(dict(remote_tests_passed=True)))
'''
    result.update(json.loads(remote(DEFAULT_KEY, 'python3 -', code.encode())))
    before = status(DEFAULT_KEY)
    result.update(previous_release=before['engine']['WorkingDirectory'], pause_owner='c3-audit-upgrade-'+result['digest']+'\n')
    RECORD.write_text(json.dumps(result, indent=2)+'\n')
    return result


def switch():
    result = json.loads(RECORD.read_text())
    assert result['remote_tests_passed'] and not result.get('switched')
    code = GUARD + f'''
import hashlib
release=pathlib.Path({result['release']!r}).resolve()
assert release.is_relative_to(base/'releases') and release!=base/'releases'
assert unit({ENGINE!r})['WorkingDirectory']=={result['previous_release']!r}, 'another rollout changed the engine'
manifest=json.loads((release/'manifest.json').read_text())
assert all(hashlib.sha256((release/k).read_bytes()).hexdigest()==v for k,v in manifest.items())
sys.path.insert(0,str(release))
from track_c.coinone import CoinoneReadOnly,Credentials
from track_c.rule import VERSION
cfg_bytes=(base/'config.json').read_bytes(); cfg=json.loads(cfg_bytes)
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
unit_path.write_text(old.replace('WorkingDirectory='+{result['previous_release']!r},'WorkingDirectory='+str(release)))
subprocess.run(['systemctl','daemon-reload'],check=True)
subprocess.run(['systemctl','start',{ENGINE!r}],check=True)
assert (base/'config.json').read_bytes()==cfg_bytes
print(json.dumps(dict(switched=True,rule=VERSION,pause_owned=owned,config_sha256=hashlib.sha256(cfg_bytes).hexdigest(),backup=str(backup))))
'''
    out = json.loads(remote(DEFAULT_KEY, 'sudo -n '+BASE+'/.venv/bin/python -', code.encode()))
    result.update(out)
    RECORD.write_text(json.dumps(result, indent=2)+'\n')
    return result


def resume():
    result = json.loads(RECORD.read_text())
    assert result.get('switched')
    code = GUARD + f'''
import hashlib
assert unit({ENGINE!r})['WorkingDirectory']=={result['release']!r}
assert hashlib.sha256((base/'config.json').read_bytes()).hexdigest()=={result['config_sha256']!r}
sys.path.insert(0,{result['release']!r})
from track_c.rule import VERSION
s=json.loads((base/'data/status.json').read_text())
assert s.get('rule',{{}}).get('version')==VERSION and time.time()*1000-s['t_ms']<60000
assert s['connected'] and s['private_connected'] and not s['halt'] and s['storage_ok']
assert s['leaders']['storage_ok'] and all(v['connected'] for v in s['leaders']['venues'].values())
assert all(s.get('fair',{{}}).get(c) for c in s['rule']['coins']), 'fair values still warming up'
flat()
pause=base/'data/PAUSE'
assert pause.exists(), 'pause changed during rollout'
removed=False
if {result.get('pause_owned',False)!r}:
    assert pause.read_text()=={result['pause_owner']!r}, 'pause owner changed'
    pause.unlink(); removed=True
print(json.dumps(dict(verified=True,resumed=removed,operator_pause_preserved=not removed,rule=VERSION)))
'''
    result.update(json.loads(remote(DEFAULT_KEY, 'sudo -n '+BASE+'/.venv/bin/python -', code.encode())))
    RECORD.write_text(json.dumps(result, indent=2)+'\n')
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=('prepare','switch','resume','status'))
    args = p.parse_args()
    verify(DEFAULT_AWS)
    result = dict(status=status(DEFAULT_KEY)['summary']) if args.command=='status' else globals()[args.command]()
    print(json.dumps(result))


if __name__ == '__main__':
    main()
