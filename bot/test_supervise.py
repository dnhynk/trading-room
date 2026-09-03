"""Invariants of the watchdog plumbing (bot/supervise.py). The Monitor reads what this writes, so a silent encoding change here
stops supervision.  python -m unittest bot.test_supervise"""
import os, types, unittest
from bot import supervise

class ChildLogEncoding(unittest.TestCase):
    """logs/<job>.log has two writers — this module (utf-8) and the child on the inherited handle, which picks its own encoding
    (cp949 from a clean shell). One Korean traceback line then made the file undecodable and killed bot.watch_cycle."""
    def test_the_child_is_told_to_write_utf8(self):
        seen = {}
        real = supervise.subprocess.Popen
        supervise.subprocess.Popen = lambda cmd, **kw: seen.update(kw) or types.SimpleNamespace(pid=1)
        real_assign, supervise.assign = supervise.assign, lambda job, p: True
        try: supervise.spawn(["python", "-u", "-m", "bot.cycle"], "test-encoding")
        finally:
            supervise.subprocess.Popen = real; supervise.assign = real_assign
            try: os.remove(os.path.join(supervise.ROOT, "logs", "test-encoding.log"))
            except OSError: pass
        self.assertEqual(seen["env"]["PYTHONIOENCODING"], "utf-8")
        self.assertIn("PATH", seen["env"])                      # the rest of the environment is passed through, not replaced

if __name__ == "__main__":
    unittest.main()
