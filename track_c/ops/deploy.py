"""Build and stage an isolated, verified C release without switching any service."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import shlex
import tarfile
import time
import tomllib

from common.paths import PROJECT_ROOT
from track_c.ops.aws import BASE, DEFAULT_AWS, DEFAULT_KEY, remote, verify
from track_c.replay.evidence import read_model


def bundle(model):
    document, _, _ = read_model(model)
    contents = {}
    for package in ('common', 'track_a', 'track_b', 'track_c', 'tests'):
        for path in (PROJECT_ROOT/package).rglob('*'):
            if path.is_file() and path.suffix in ('.py', '.json'):
                contents[path.relative_to(PROJECT_ROOT).as_posix()] = path.read_bytes()
    for name in ('pyproject.toml', 'config/tracks.json', 'config/bitget.json'):
        contents[name] = (PROJECT_ROOT/name).read_bytes()
    contents['model.json'] = Path(model).read_bytes()
    dependencies = tomllib.loads(contents['pyproject.toml'].decode())['project']['dependencies']
    contents['requirements.txt'] = ('\n'.join(dependencies)+'\n').encode()
    manifest = {name:hashlib.sha256(body).hexdigest() for name,body in contents.items()}
    identity = hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest()[:12]
    contents['manifest.json'] = json.dumps(manifest,sort_keys=True).encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output,mode='w:gz') as archive:
        for name,body in sorted(contents.items()):
            member=tarfile.TarInfo(name)
            member.size=len(body); member.mode=0o600
            archive.addfile(member,io.BytesIO(body))
    return output.getvalue(), identity, document['digest']


def stage(model, key=DEFAULT_KEY, aws=DEFAULT_AWS):
    instance=verify(aws)
    payload,identity,model_digest=bundle(model)
    release=BASE+'/c4-live/releases/'+time.strftime('%Y%m%d-%H%M%S')+'-'+identity
    remote(key,'umask 077; mkdir '+shlex.quote(release)+' && tar -xzf - -C '+shlex.quote(release),payload)
    script=f'''
import hashlib,json,pathlib,subprocess,sys
root=pathlib.Path({release!r})
manifest=json.loads((root/'manifest.json').read_text())
assert all(hashlib.sha256((root/name).read_bytes()).hexdigest()==value for name,value in manifest.items())
from track_c.replay.evidence import read_model
doc,_,_=read_model(root/'model.json')
assert doc['digest']=={model_digest!r}
run=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-t','.'],cwd=root,capture_output=True,text=True)
(root/'test-output.txt').write_text(run.stdout+run.stderr)
print(json.dumps(dict(tests_passed=run.returncode==0,test_summary=run.stderr[-600:],files=len(manifest))))
'''
    checked=json.loads(remote(key,'cd '+shlex.quote(release)+' && '+BASE+'/.venv/bin/python -',script.encode()))
    if not checked['tests_passed']:
        raise RuntimeError('staged tests failed; inspect the isolated release test-output.txt')
    return dict(instance=instance,release=release,model=model_digest,digest=identity,
                service_started=False,secrets_packaged=False,**checked)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--stage',action='store_true',help='Upload and test only; never switches a service')
    args=parser.parse_args()
    path=Path(args.output)
    if path.exists():raise FileExistsError('output already exists')
    if args.stage:
        result=stage(args.model)
        path.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    else:
        payload,identity,model=bundle(args.model)
        path.write_bytes(payload)
        result=dict(bundle=str(path),digest=identity,model=model,service_started=False)
    print(json.dumps(result))


if __name__=='__main__':main()
