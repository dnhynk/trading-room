"""Reproducible B regression/stress panel against the pre-edit git revision.

python -m track_b.replay --scorecard logs/research-b-20260905.json --output logs/replay-b-20260905.json

The panel uses recorded campaign symbol/side windows rounded OUT to whole hours.
It is conditional on historical selection and already observed data. It is NOT
a replay of the full hunt/AI selection process and is NOT an OOS alpha test.
No exchange orders, params writes, or production process control.
"""
from common.paths import runtime_root
import argparse
import copy
import datetime as dt
import hashlib
import json
import math
import os
import subprocess
import sys
import time
import types
from collections import defaultdict
from unittest.mock import patch

from track_a import backtest
from common import signal
from track_b.hunt import HUNT
from common.ws import load_params

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def panel(campaigns):
    groups = defaultdict(list)
    for c in campaigns:
        if c.get("issues") or not c.get("t1"): continue
        groups[(c["symbol"], c["side"])].append(c)
    spec = []
    for (sym, side), cs in sorted(groups.items()):
        start = dt.datetime.fromisoformat(min(c["t0"] for c in cs)).astimezone(dt.timezone.utc).replace(minute=0, second=0)
        end = dt.datetime.fromisoformat(max(c["t1"] for c in cs)).astimezone(dt.timezone.utc)
        files = []
        while start <= end:
            name = os.path.join(runtime_root(ROOT), "data", "ws", start.strftime("pub-%Y%m%d-%H.jsonl.gz"))
            if not os.path.isfile(name): raise FileNotFoundError(name)
            files.append(name)
            start += dt.timedelta(hours=1)
        spec.append(dict(sym=sym, side=side, files=files))
    return spec


def reference(revision):
    """Load the old pure signal/replay code in memory; do not edit a checkout."""
    modules = []
    old_signal = sys.modules["common.signal"]
    try:
        for name in ("signal", "backtest"):
            source = subprocess.check_output(["git", "show", f"{revision}:bot/{name}.py"], cwd=ROOT).decode("utf-8")
            # Historical source stays in Git. Translate imports only, leaving its
            # strategy and execution math intact in the isolated comparison.
            for dependency in ('signal', 'risk', 'ws', 'bitget'):
                source = source.replace('bot.'+dependency, 'common.'+dependency)
            package = 'common' if name == 'signal' else 'track_a'
            module = types.ModuleType(f"{package}.{name}")
            module.__file__ = os.path.join(ROOT, package, f"{name}.py")
            module.runtime_root = runtime_root
            source = source.replace('os.path.join(ROOT, "data",', 'os.path.join(runtime_root(ROOT), "data",')
            exec(compile(source, module.__file__, "exec"), module.__dict__)
            modules.append(module)
            if name == "signal": sys.modules["common.signal"] = module
        return modules
    finally: sys.modules["common.signal"] = old_signal


class FrozenCache:
    """Identical, offline warm-up/contract inputs for every variant; never fetch REST."""
    def __init__(self, cache):
        self.cache, self.data, self.manifest = cache, {}, {}

    def read(self, name, optional=False):
        if name not in self.data:
            path = os.path.join(self.cache, name)
            if optional and not os.path.isfile(path):
                self.data[name], self.manifest[name] = None, "missing; identical tape-only warm-up"
            else:
                with open(path, "rb") as fh: raw = fh.read()
                self.data[name] = json.loads(raw)
                self.manifest[name] = hashlib.sha256(raw).hexdigest()
        return copy.deepcopy(self.data[name])

    def contract(self, sym):
        d = self.read(f"contract-{sym}.json")
        if any(not math.isfinite(float(d[k])) or float(d[k]) <= 0 for k in ("qstep", "tick")):
            raise ValueError(f"invalid cached contract: {sym}")
        return d

    def seed(self, sym, sec):
        d = self.read(f"seed-{sym}-{int(sec)}.json", optional=True)
        if d is None: return None, None, None
        for key, duration in (("c1", 60), ("c15", 900), ("daily", 86400)):
            if any(r["ts"] + duration * 1000 > sec * 1000 for r in d.get(key) or []):
                raise ValueError(f"look-ahead in cached seed: {sym} {key}")
        return d.get("c1"), d["c15"], d["daily"]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scorecard", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--reference", default="HEAD")
    ap.add_argument("--equity", type=float, default=100)
    args = ap.parse_args()
    with open(args.scorecard, encoding="utf-8") as fh: score = json.load(fh)
    p = load_params()
    if not p or not (p.get("hunt") or {}).get("on"): raise ValueError("a B profile snapshot is required")
    profile = {**signal.STRAT, **p["strat"], **p["hunt"]["strat"], "hunt": 1, "wallet_frac": 1.0}
    spec = panel(score["campaigns"])
    hunt = {**HUNT, **p["hunt"]}
    for b in spec:
        b["strat"] = dict(blowoff_atr=hunt["blowoff_atr"] if b["side"] == "long" else 0,
                          blowoff_frac=hunt["blowoff_frac"])
    revision = subprocess.check_output(["git", "rev-parse", args.reference], cwd=ROOT, text=True).strip()
    old_signal, old_bt = reference(revision)
    hashes = {}
    for name in ("signal", "backtest", "risk", "cycle", "hunt"):
        package = 'track_a' if name == 'backtest' else 'track_b' if name == 'hunt' else 'common'
        with open(os.path.join(ROOT, package, name + ".py"), "rb") as fh: hashes[name] = hashlib.sha256(fh.read()).hexdigest()
    result = dict(reference=revision, source_hashes=hashes, equity=args.equity, profile=profile, sig=p.get("sig"), spec=spec,
                  selection="historical completed-campaign windows, expanded to whole hours; development regression only", runs={})
    cache = FrozenCache(backtest.CACHE)
    result["cache_inputs"] = cache.manifest
    result["offline"] = True
    variants = [("reference", old_bt, None), ("candidate", backtest, None),
                ("cost_stress", backtest, dict(maker=.0004, taker=.0012, taker_slip=.0025, stop_slip=.0025))]
    for name, module, execution in variants:
        print(f"{name}: {len(spec)} symbol/side windows, pool 1, equity {args.equity:g}", flush=True)
        t0 = time.monotonic()
        kwargs = dict(strat=profile, sig=p.get("sig"), pool_cap=1, equity=args.equity)
        if execution is not None: kwargs["execution"] = execution
        with patch.object(module, "load_params", return_value=p), patch.object(module, "contract_meta", side_effect=cache.contract), \
                patch.object(module, "seed_history", side_effect=cache.seed), \
                patch.dict(sys.modules, {"common.signal": old_signal if name == "reference" else signal}):
            result["runs"][name] = module.run_multi(spec, **kwargs)
        print(json.dumps(result["runs"][name]["pool"]), f"{time.monotonic()-t0:.1f}s", flush=True)
        with open(args.output, "w", encoding="utf-8") as fh: json.dump(result, fh, ensure_ascii=False, indent=2, allow_nan=False)
    print("No alpha promotion: this checks code changes and execution-cost sensitivity on already selected windows.", flush=True)


if __name__ == "__main__": main()
