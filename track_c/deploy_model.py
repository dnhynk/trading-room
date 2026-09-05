"""Replace the read-only model worker without restarting the trading process."""
import json
import shlex
from .deploy import BASE,DEFAULT_AWS,DEFAULT_KEY,ROOT,remote,stage


def install():
    r=stage(DEFAULT_AWS,DEFAULT_KEY); release=r['release']
    script=f'''
import json,pathlib,re,subprocess
base=pathlib.Path({BASE!r}); release=pathlib.Path({release!r})
def pid(): return subprocess.run(['systemctl','show','trading-room-c.service','--value','-p','MainPID'],capture_output=True,text=True,check=True).stdout.strip()
before=pid(); assert before!='0'
tests=subprocess.run([str(base/'.venv/bin/python'),'-m','unittest','track_c.test_quantitative','track_c.test_portfolio','track_c.test_research'],cwd=release,capture_output=True,text=True)
if tests.returncode: print(tests.stdout); print(tests.stderr); raise SystemExit(1)
unit=pathlib.Path('/etc/systemd/system/trading-room-c-model.service')
old=unit.read_text(); assert '-m track_c.model_worker --config '+str(base/'config.json') in old
(release/'worker-unit-before.service').write_text(old)
subprocess.run(['systemctl','stop','trading-room-c-model.service'],check=True)
unit.write_text(re.sub(r'^WorkingDirectory=.*$', 'WorkingDirectory='+str(release), old,flags=re.M))
subprocess.run(['systemctl','daemon-reload'],check=True)
subprocess.run(['systemctl','start','--no-block','trading-room-c-model.service'],check=True)
assert pid()==before
print(json.dumps(dict(engine_pid=before,engine_unchanged=True,worker_started=True,tests_passed=True)))
'''
    r.update(json.loads(remote(DEFAULT_KEY,'sudo -n python3 -',script.encode())))
    (ROOT/'logs/track-c/model-deployment-latest.json').write_text(json.dumps(r,indent=2)+'\n')
    return r


if __name__=='__main__': print(json.dumps(install()))
