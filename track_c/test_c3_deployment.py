import json
from pathlib import Path
import unittest
from .c3_upgrade import activation_records, upgraded_config, CONFIG_PROFILES
from .settings import load
from .service_health import upgrade_unit


class DeploymentTests(unittest.TestCase):
    def test_pending_rollout_requires_owned_never_resumed_pause(self):
        from .c3_upgrade import pending_activation
        good=dict(profile='execution',switched=True,resumed=False,pause_owned=True)
        self.assertEqual(pending_activation(good,'execution'),good)
        for record in (dict(good,resumed=True),dict(good,pause_owned=False),dict(good,switched=False)):
            with self.assertRaises(ValueError): pending_activation(record,'execution')
        with self.assertRaises(ValueError): pending_activation(good,'entry-2')

    def test_execution_changes_only_authorized_keys(self):
        desired=load(Path(__file__).with_name('config-c3.json'))
        current=dict(desired,mode='live',funding_confirmed=True,notional_krw=10000,stop_mode='fixed',value_exit=False)
        for key in ('http_timeout_s',): current.pop(key)
        changed=upgraded_config(current,desired,'execution')
        keys=CONFIG_PROFILES['execution']
        self.assertEqual({k:v for k,v in current.items() if k not in keys},{k:v for k,v in changed.items() if k not in keys})
        self.assertEqual(changed['notional_krw'],20000)
        for bad in (dict(desired,notional_krw=10000),dict(desired,value_exit=False)):
            with self.assertRaises(ValueError): upgraded_config(current,bad,'execution')

    def test_new_evaluation_preserves_slack_scope_across_midnight(self):
        cfg=load(Path(__file__).with_name('config-c3.json'))
        old=dict(schema=2,name='previous',start_ms=1788623847869,end_ms=1789487847869,days=10,rule='c3-rule-v3',source={},config=cfg)
        baseline=dict(day='2026-09-06',start_ms=1788620790977,coins=['BTC'],entry_dev_min_ticks=2.0,custom='keep')
        before=json.dumps(baseline,sort_keys=True)
        protocol,after=activation_records(old,cfg,old['start_ms']+86400000,'release','execution',baseline)
        self.assertEqual(after,baseline); self.assertEqual(json.dumps(baseline,sort_keys=True),before)
        self.assertEqual(protocol['rule'],'c3-rule-v4')
        self.assertEqual(protocol['config']['notional_krw'],20000)
        self.assertEqual(protocol['end_ms']-protocol['start_ms'],10*86400000)
        self.assertNotEqual(protocol['start_ms'],after['start_ms'])

    def test_main_loop_watchdog_and_serial_recovery_keep_unit_security(self):
        old='[Unit]\nDescription=C3\n[Service]\nType=simple\nWorkingDirectory=/base/releases/old\nExecStart=/base/.venv/bin/python -u -m track_c.c3_runner --config /base/config.json\nTimeoutStopSec=120\nUser=ubuntu\nProtectSystem=strict\nReadWritePaths=/base/data\n[Install]\nWantedBy=multi-user.target\n'
        result=upgrade_unit(old,'/base/releases/old','/base/releases/new','/base')
        self.assertEqual(result.count('Type='),1)
        self.assertIn('Type=notify',result); self.assertIn('WatchdogSec=20s',result)
        self.assertIn('ExecStopPost=/base/.venv/bin/python -m track_c.recovery',result)
        self.assertIn('ProtectSystem=strict',result); self.assertIn('TimeoutStopSec=120',result)
        self.assertEqual(upgrade_unit(result,'/base/releases/new','/base/releases/new','/base'),result)
        with self.assertRaises(ValueError): upgrade_unit(old,'/wrong','/new','/base')


if __name__=='__main__': unittest.main()
