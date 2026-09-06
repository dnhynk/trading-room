"""Inspect the deployed owner and its actual data path. Never submits orders."""
import argparse
import json
from track_c.ops.aws import BASE, DEFAULT_KEY, remote
ENGINE='trading-room-c.service'
NOTIFY='trading-room-c-notify.service'
COMMON=f'''
import hashlib,json,pathlib,sqlite3,subprocess,sys,time,shutil
base=pathlib.Path({BASE!r})
def props(u):
 r=subprocess.run(['systemctl','show',u,'-p','MainPID','-p','ActiveState','-p','WorkingDirectory','-p','NRestarts'],capture_output=True,text=True,check=True)
 return dict(x.split('=',1) for x in r.stdout.splitlines() if '=' in x)
'''


def status():
    code=COMMON+f'''
engine=props({ENGINE!r})
if '/c4-live/releases/' in engine['WorkingDirectory']:
 line=subprocess.run(['systemctl','show',{ENGINE!r},'--value','-p','ExecStart'],capture_output=True,text=True,check=True).stdout
 import re
 match=re.search(r'--config ([^ ;]+)',line)
 directory=pathlib.Path(json.loads(pathlib.Path(match.group(1)).read_text())['data_directory'])
else:directory=base/'data'
s=json.loads((directory/'status.json').read_text()) if (directory/'status.json').exists() else {{}}
notification=directory/'notifications/status.json'
report=dict(engine=engine,notifier=props({NOTIFY!r}),shadow=props('trading-room-c4-shadow.service'),
 directory=str(directory),entry_paused=(directory/'PAUSE').exists(),status=s,
 notifications=json.loads(notification.read_text()) if notification.exists() else None,
 summary={{k:s.get(k) for k in ['t_ms','policy','mode','c4_live_mode','model','connected','private_connected',
 'halt','storage_ok','capital_krw','positions','residuals','counts']}})
print(json.dumps(report))
'''
    return json.loads(remote(DEFAULT_KEY,'python3 -',code.encode()))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--full',action='store_true')
    args=parser.parse_args()
    result=status()
    print(json.dumps(result if args.full else {k:result[k] for k in ('engine','notifier','shadow','directory','entry_paused','summary','notifications')},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
