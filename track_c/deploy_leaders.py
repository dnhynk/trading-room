"""Install/inspect the leader-venue recorder service on the authorized AWS instance."""
import argparse
import json
from pathlib import Path
import shlex
import time

from .deploy import BASE, DEFAULT_AWS, DEFAULT_KEY, ROOT, remote, stage, verify

UNIT = 'trading-room-c-leaders.service'


def status(key):
    code = f'''
import json,pathlib,subprocess,time
base=pathlib.Path({BASE!r})
r=subprocess.run(['systemctl','show',{UNIT!r},'-p','ActiveState','-p','MainPID','-p','NRestarts','-p','WorkingDirectory','-p','UnitFileState'],capture_output=True,text=True,check=True)
unit=dict(line.split('=',1) for line in r.stdout.splitlines() if '=' in line)
folder=base/'data/leaders'; status=json.loads((folder/'status.json').read_text()) if (folder/'status.json').exists() else None
files=sorted((p.name,p.stat().st_size) for p in folder.glob('*.gz')) if folder.exists() else []
engine=json.loads((base/'data/status.json').read_text())
print(json.dumps(dict(t_ms=int(time.time()*1000),unit=unit,status=status,files=files[-6:],engine_entry_paused=engine.get('entry_paused'),engine_status_age_s=(time.time()*1000-engine['t_ms'])/1000,pause=(base/'data/PAUSE').read_text() if (base/'data/PAUSE').exists() else None)))
'''
    return json.loads(remote(key, 'python3 -', code.encode()))


def install(aws=DEFAULT_AWS, key=DEFAULT_KEY):
    result = stage(aws, key)
    release = result['release']
    unit = f'''[Unit]
Description=Track C leader venues recorder (Upbit/Bithumb top-of-book, public only)
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=ubuntu
WorkingDirectory={release}
ExecStart={BASE}/.venv/bin/python -u -m track_c.leaders --data-dir {BASE}/data
Restart=always
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths={BASE}/data/leaders
MemoryMax=256M
CPUQuota=50%
[Install]
WantedBy=multi-user.target
'''
    script = f'''
import pathlib,subprocess,json,time
base=pathlib.Path({BASE!r}); folder=base/'data/leaders'; folder.mkdir(exist_ok=True)
subprocess.run(['chown','ubuntu:ubuntu',str(folder)],check=True)
pathlib.Path('/etc/systemd/system',{UNIT!r}).write_text({unit!r})
subprocess.run(['systemctl','daemon-reload'],check=True)
subprocess.run(['systemctl','enable',{UNIT!r}],check=True,capture_output=True)
subprocess.run(['systemctl','restart',{UNIT!r}],check=True)
time.sleep(35)
print(json.dumps(dict(installed=True,release={release!r})))
'''
    result.update(json.loads(remote(key, 'sudo -n python3 -', script.encode())))
    result['status'] = status(key)
    path = ROOT / 'logs/track-c/leaders-deployment-latest.json'
    path.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['install', 'status'])
    args = p.parse_args()
    verify(DEFAULT_AWS)
    result = install() if args.command == 'install' else status(DEFAULT_KEY)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
