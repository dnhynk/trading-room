"""Read-only opportunity census of already-recorded coins; no universe changes."""
import argparse
from collections import Counter,defaultdict
import json

from track_c.replay.input import coinone_rows
from track_c.replay.dataset import public_contracts
from track_c.market.leaders import rows as leader_rows
from track_c.replay.stream import EventSpool
from track_c.learning.config import load
from track_c.market.state import candidates
from track_c.replay.ledger import frames, atomic_json
from track_c.replay.engine import stage, embargo


def census(spec):
    contracts,units=public_contracts(spec['contracts'])
    coins=[c for c in ('BTC','ETH','XRP','SOL') if c in contracts]
    # Observation-only scope. The validated trading/model universe remains BTC.
    cfg={**load(),'coins':coins}
    counts=defaultdict(Counter);quality=Counter()
    start,end=spec['test_start_ms'],spec['end_ms']
    with EventSpool(coinone_rows(spec['coinone'],quality),(r for p in spec['leaders'] for r in leader_rows(p)),
                    coins,cfg['book_max_age_ms'],quality) as spool:
        for now,snaps,_ in frames(spool,cfg,contracts,units,end):
            if not start<now<=end:continue
            day=(now+9*3600000)//86400000
            for coin,s in snaps.items():
                n=counts[(day,coin)];n['observed_frames']+=1
                if s and s.get('entry_fresh'):n['fresh_local_frames']+=1
                if s and s['reference']['ready']:n['reference_ready_frames']+=1
                if not s or not s['new_episode']:continue
                n['sell_shock_episodes']+=1
                why=stage(s,cfg)
                if why:n['rejected_'+why]+=1
                actions=candidates(s,cfg,594574.,594574.*cfg['risk_fraction'])
                if now>=end-embargo(cfg):n['boundary_excluded_episodes']+=bool(actions);continue
                n['candidate_actions']+=len(actions)
                n['candidate_episodes']+=bool(actions)
                if not actions and not why:n['rejected_price_size_or_capacity']+=1
    return dict(start_ms=start,end_ms=end,mode='observation_only',trading_coins_unchanged=['BTC'],
                days=[dict(kst_day_index=day,coin=coin,**dict(n),
                           observed_hours=n['observed_frames']*cfg['decision_ms']/3600000,
                           complete_day=n['observed_frames']*cfg['decision_ms']>=86400000)
                      for (day,coin),n in sorted(counts.items())],quality=dict(quality),
                actual_daily_trade_frequency='unverified; candidates are not fills; partial days are not extrapolated')


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--spec',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();result=census(json.loads(open(a.spec,encoding='utf-8').read()));atomic_json(a.output,result)
    print(json.dumps(result))


if __name__=='__main__':main()
