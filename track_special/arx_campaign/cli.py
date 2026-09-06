from __future__ import annotations
import argparse, json
from pathlib import Path
from .config import load
from .execution.engine import CampaignEngine
from .reporting.korean import daily_report, severe_report

def build_parser():
 p=argparse.ArgumentParser(prog="track_special"); p.add_argument("--config",default="track_special/configs/observe.yaml"); p.add_argument("--state",default="track-special-arx.sqlite")
 s=p.add_subparsers(dest="command",required=True)
 for n in ("doctor","observe","paper","replay","status","report","pause-entries","cancel-entry-orders","request-exit","propose-risk-change","validate-live","collect"): s.add_parser(n)
 return p
def main(argv=None):
 a=build_parser().parse_args(argv); cfg=load(a.config)
 if a.command == "collect": raise SystemExit("collect unavailable: install/configure the later ARX marketdata module; no fallback private API is used")
 if a.command == "validate-live":
  print(json.dumps({"valid_config":True,"live_operational":False,"reason":"live adapter intentionally absent; read-only capability validation cannot promote this build"})); return 0
 if a.command in {"doctor","observe","paper","replay","propose-risk-change"}:
  print(json.dumps({"command":a.command,"mode":cfg.mode.value,"exchange_writes":0,"live_operational":False})); return 0
 e=CampaignEngine(Path(a.state))
 if a.command=="pause-entries": e.set_control(__import__("track_special.arx_campaign.contracts",fromlist=["RiskState"]).RiskState.PAUSE_ENTRIES,"operator")
 elif a.command=="cancel-entry-orders": print(json.dumps({"canceled":e.cancel_entry_orders()})); return 0
 elif a.command=="request-exit": e.request_exit()
 elif a.command=="report": print(daily_report({"risk_state":e.status()["risk_state"]})); return 0
 print(json.dumps(e.status(),default=str)); return 0
