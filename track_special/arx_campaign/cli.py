from __future__ import annotations
import argparse, hashlib, json
from datetime import datetime, timezone
from pathlib import Path
from .config import load
from .execution.engine import CampaignEngine
from .reporting.korean import daily_report
from .contracts import RiskState

def build_parser():
 p=argparse.ArgumentParser(prog="track_special"); p.add_argument("--config",default="track_special/configs/observe.yaml"); p.add_argument("--state")
 s=p.add_subparsers(dest="command",required=True)
 for n in ("doctor","collect","observe","paper","replay","status","report","pause-entries","cancel-entry-orders","request-exit","emergency-halt","propose-risk-change","validate-live"): s.add_parser(n)
 return p
def _state(a,cfg):
 p=Path(a.state) if a.state else cfg.state_directory/"campaign.sqlite"
 if not p.is_absolute(): raise ValueError("state must be an absolute external path")
 return p
def main(argv=None):
 a=build_parser().parse_args(argv); cfg=load(a.config)
 if a.command=="collect":
  try:
   from .marketdata.collector import collect_public_snapshot
  except ImportError: raise SystemExit("collect unavailable: public collector integration is not installed; no private API fallback is used")
  print(json.dumps(collect_public_snapshot(config=cfg.raw),default=str)); return 0
 if a.command=="validate-live":
  print(json.dumps({"valid_config":cfg.mode.value=="live","live_operational":False,"reason":"live adapter intentionally absent; validation has no private writes"})); return 0
 if a.command in {"doctor","observe","paper","replay"}:
  print(json.dumps({"command":a.command,"mode":cfg.mode.value,"exchange_writes":0,"live_operational":False,"state_directory":str(cfg.state_directory)})); return 0
 if a.command=="propose-risk-change":
  proposal={"kind":"risk_change_proposal","config_hash":cfg.config_hash,"worst_loss":"UNKNOWN: requires independently approved scenario","effective_after":"approval + cooling delay","approval":"pending","created_at":datetime.now(timezone.utc).isoformat()}
  target=cfg.state_directory/"risk-change-proposal.json"; target.parent.mkdir(parents=True,exist_ok=True); target.write_text(json.dumps(proposal,sort_keys=True),encoding="utf-8")
  print(json.dumps(proposal)); return 0
 with CampaignEngine(_state(a,cfg)) as e:
  if a.command=="pause-entries": e.set_control(RiskState.PAUSE_ENTRIES,"operator")
  elif a.command=="cancel-entry-orders": print(json.dumps({"canceled":e.cancel_entry_orders()})); return 0
  elif a.command=="request-exit": e.request_exit()
  elif a.command=="emergency-halt": e.halt("operator_emergency_halt")
  elif a.command=="report": print(daily_report({"risk_state":e.status()["risk_state"]})); return 0
  print(json.dumps(e.status(),default=str)); return 0
