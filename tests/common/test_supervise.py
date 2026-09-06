"""Invariants of the watchdog plumbing (common/supervise.py). The Monitor reads what this writes, so a silent encoding change here
stops supervision.  python -m unittest tests.common.test_supervise"""
import os, types, unittest, tempfile
from pathlib import Path
from unittest.mock import patch
from common import supervise

class ChildLogEncoding(unittest.TestCase):
    """logs/<job>.log has two writers — this module (utf-8) and the child on the inherited handle, which picks its own encoding
    (cp949 from a clean shell). One Korean traceback line then made the file undecodable and killed common.watch_cycle."""
    def test_the_child_is_told_to_write_utf8(self):
        seen = {}
        real = supervise.subprocess.Popen
        supervise.subprocess.Popen = lambda cmd, **kw: seen.update(kw) or types.SimpleNamespace(pid=1)
        real_assign, supervise.assign = supervise.assign, lambda job, p: True
        try:
            with tempfile.TemporaryDirectory() as folder, patch.object(supervise, 'ROOT', folder):
                (Path(folder)/'logs').mkdir()
                supervise.spawn(["python", "-u", "-m", "common.cycle"], "test-encoding")
        finally:
            supervise.subprocess.Popen = real; supervise.assign = real_assign
        self.assertEqual(seen["env"]["PYTHONIOENCODING"], "utf-8")
        self.assertIn("PATH", seen["env"])                      # the rest of the environment is passed through, not replaced

if __name__ == "__main__":
    unittest.main()
