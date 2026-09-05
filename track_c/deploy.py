"""AWS CLI target verification and narrowly scoped SSH staging. Never packages secrets."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import shlex
import subprocess
import tarfile
import time

ROOT = Path(__file__).resolve().parents[1]
INSTANCE = "i-0db0329527b7b3533"
HOST = "52.78.144.101"
BASE = "/home/ubuntu/trading-room-c"
DEFAULT_AWS = ROOT / "logs/tools/aws-cli-v2/extracted/Amazon/AWSCLIV2/aws.exe"
DEFAULT_KEY = Path("D:/repos/arbitrage/data/reports/aws_probe/alphaverdict-probe.pem")


def ssh_args(key):
    return ["ssh", "-i", str(key), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10", "ubuntu@"+HOST]


def remote(key, command, data=None):
    result = subprocess.run(ssh_args(key)+[command], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
    if result.returncode:
        # Commands never contain secrets, but do not relay arbitrary server errors.
        raise RuntimeError(f"remote operation failed ({result.returncode})")
    return result.stdout


def verify(aws):
    result = subprocess.run([str(aws), "ec2", "describe-instances", "--region", "ap-northeast-2", "--instance-ids", INSTANCE,
                             "--query", "Reservations[].Instances[].{Id:InstanceId,IP:PublicIpAddress,State:State.Name}", "--output", "json", "--no-cli-pager"],
                            capture_output=True, check=True, timeout=30)
    rows = json.loads(result.stdout)
    if len(rows) != 1 or rows[0] != dict(Id=INSTANCE, IP=HOST, State="running"):
        raise RuntimeError("AWS instance identity/IP/state mismatch")
    return rows[0]


def bundle():
    paths = sorted((ROOT / "track_c").glob("*.py")) + [ROOT / "track_c/config.json", ROOT / "track_c/config-c2.json", ROOT / "track_c/config-c3.json", ROOT/'track_c/requirements.txt',
                                                     ROOT / "bot/signal.py", ROOT / "bot/risk.py", ROOT/'bot/notify.py', ROOT/'bot/test_notify.py']
    manifest = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:12]
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as tar:
        for p in paths:
            tar.add(p, arcname=p.relative_to(ROOT).as_posix(), recursive=False)
        raw = json.dumps(manifest, sort_keys=True).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(raw)
        tar.addfile(info, io.BytesIO(raw))
    return payload.getvalue(), digest, manifest


def stage(aws, key):
    identity = verify(aws)
    payload, digest, manifest = bundle()
    release = BASE+"/releases/"+time.strftime("%Y%m%d-%H%M%S")+"-"+digest
    remote(key, "umask 077; mkdir -p "+shlex.quote(release)+" && tar -xzf - -C "+shlex.quote(release), payload)
    script = "import hashlib,json,pathlib; p=pathlib.Path("+repr(release)+"); m=json.loads((p/'manifest.json').read_text()); assert all(hashlib.sha256((p/k).read_bytes()).hexdigest()==v for k,v in m.items()); print(json.dumps({'verified_files':len(m)}))"
    checked = json.loads(remote(key, "python3 -c "+shlex.quote(script)))
    result = dict(instance=identity, release=release, digest=digest, verified_files=checked["verified_files"], secrets_packaged=False, service_started=False)
    path = ROOT / "logs/track-c/deployment-latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    return result


def install_observe(aws, key):
    """Stage, test and install the observer; refuses existing live/exposed state."""
    result = stage(aws, key)
    release = result["release"]
    remote(key, "python3 -m venv "+BASE+"/.venv && "+BASE+"/.venv/bin/python -m pip install --disable-pip-version-check --no-input websockets==17.1")
    remote(key, "cd "+shlex.quote(release)+" && "+BASE+"/.venv/bin/python -m unittest track_c.test_coinone track_c.test_runtime")
    unit = f"""[Unit]
Description=Track C Coinone scalping
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory={release}
ExecStart={BASE}/.venv/bin/python -u -m track_c.runner --config {BASE}/config.json
Restart=on-failure
RestartSec=5
TimeoutStopSec=120
KillSignal=SIGTERM
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths={BASE}/data
ReadOnlyPaths=/home/ubuntu/arbitrage/.env
MemoryMax=384M
CPUQuota=50%

[Install]
WantedBy=multi-user.target
"""
    script = f"""
import json,pathlib,sqlite3,subprocess
base=pathlib.Path({BASE!r}).resolve()
assert str(base)=='/home/ubuntu/trading-room-c'
target=base/'config.json'
if target.exists():
    previous=json.loads(target.read_text())
    assert previous['mode']=='observe' and previous['funding_confirmed'] is False, 'existing live configuration must be reconciled'
ledger=base/'data/ledger.sqlite'
if ledger.exists():
    with sqlite3.connect('file:'+str(ledger)+'?mode=ro',uri=True) as db:
        row=db.execute('select body from state where id=1').fetchone()
    if row:
        state=json.loads(row[0])
        assert state['campaign'] is None and not state['orders'], 'existing Track C exposure'
cfg=json.loads(pathlib.Path({release!r},'track_c/config.json').read_text())
assert cfg['mode']=='observe' and cfg['funding_confirmed'] is False
assert cfg['capital_mode']=='account_equity' and 'capital_krw' not in cfg and cfg['env_path']=='/home/ubuntu/arbitrage/.env'
target.write_text(json.dumps(cfg,indent=2)+'\\n')
(base/'data').mkdir(exist_ok=True)
subprocess.run(['chown','ubuntu:ubuntu',str(target),str(base/'data')],check=True)
service=pathlib.Path('/etc/systemd/system/trading-room-c.service')
if service.exists():
    service.with_suffix('.service.previous').write_text(service.read_text())
service.write_text({unit!r})
subprocess.run(['systemctl','daemon-reload'],check=True)
subprocess.run(['systemctl','enable','trading-room-c.service'],check=True)
# Only this flat, observe-only service is restarted to load the changed code.
subprocess.run(['systemctl','restart','trading-room-c.service'],check=True)
subprocess.run(['systemctl','is-active','trading-room-c.service'],check=True)
"""
    remote(key, "sudo -n python3 -", script.encode())
    result.update(service_started=True, mode="observe", funding_confirmed=False, continuous_health_verified=False)
    (ROOT/"logs/track-c/deployment-latest.json").write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify", "stage", "install-observe"))
    parser.add_argument("--aws", type=Path, default=DEFAULT_AWS)
    parser.add_argument("--ssh-key", type=Path, default=DEFAULT_KEY)
    args = parser.parse_args()
    result = (install_observe(args.aws, args.ssh_key) if args.command == "install-observe"
              else stage(args.aws, args.ssh_key) if args.command == "stage" else verify(args.aws))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
