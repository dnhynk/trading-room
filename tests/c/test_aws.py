"""Public source has no implicit deployment target; identity checks remain required."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from track_c.ops import aws


class RemoteTargetTests(unittest.TestCase):
    def test_unconfigured_remote_operations_fail_before_starting_a_process(self):
        with patch.multiple(aws, INSTANCE='', HOST=''), patch.object(aws.subprocess, 'run') as run:
            with self.assertRaises(RuntimeError):
                aws.remote('example-key', 'true')
            with self.assertRaises(RuntimeError):
                aws.verify('aws')
            run.assert_not_called()

    def test_configured_instance_must_match_host_and_be_running(self):
        identity = dict(Id='example-instance', IP='203.0.113.10', State='running')
        with patch.multiple(aws, INSTANCE=identity['Id'], HOST=identity['IP']):
            with patch.object(aws.subprocess, 'run', return_value=SimpleNamespace(stdout=json.dumps([identity]))):
                self.assertEqual(aws.verify('aws'), identity)
            for changed in ({'IP':'203.0.113.11'}, {'Id':'another-instance'}, {'State':'stopped'}):
                with self.subTest(changed=changed):
                    response = SimpleNamespace(stdout=json.dumps([{**identity, **changed}]))
                    with patch.object(aws.subprocess, 'run', return_value=response):
                        with self.assertRaises(RuntimeError):
                            aws.verify('aws')

