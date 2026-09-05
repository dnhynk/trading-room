import json
import os
import tempfile
import unittest
from unittest.mock import patch

from bot import notify


class SummaryFieldsTests(unittest.TestCase):
    def test_uses_engine_events_and_sizing_wallet_only(self):
        with tempfile.TemporaryDirectory() as root:
            logs = os.path.join(root, "logs"); os.makedirs(logs)
            events = [
                {"t": "2026-09-04 00:01:00", "ev": "SIZING", "wallet": 100.0},
                {"t": "2026-09-04 01:00:00", "ev": "FILL", "symbol": "X", "side": "short", "role": "buy", "qty": 10, "pos_qty": 10, "pnl": -0.1},
                {"t": "2026-09-04 01:00:01", "ev": "FILL", "symbol": "X", "side": "short", "role": "buy", "qty": 5, "pos_qty": 15, "pnl": -0.05},
                {"t": "2026-09-04 01:01:00", "ev": "FILL", "symbol": "X", "side": "short", "role": "trim", "qty": 15, "pos_qty": 0, "pnl": 1.15},
                {"t": "2026-09-04 02:00:00", "ev": "STOP_HIT", "pnl": -0.4},
                {"t": "2026-09-04 03:00:00", "ev": "SIZING", "wallet": 200.0},
                {"t": "2026-09-03 23:59:59", "ev": "FILL", "pnl": 99.0},
            ]
            with open(os.path.join(logs, "events.jsonl"), "w", encoding="utf-8") as fh:
                fh.write("\n".join(json.dumps(x) for x in events))
            with open(os.path.join(logs, "manual.jsonl"), "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"t1": "2026-09-04 03:00:00", "net": 200.0}))
            with open(os.path.join(logs, "state-X.json"), "w", encoding="utf-8") as fh:
                json.dump({"books": {"short": {"realized": 2.5}},
                           "acct": {"equity": 103.5, "avail": 100, "upl_all": 7}}, fh)
            with patch.object(notify, "LOGS", logs):
                self.assertEqual(notify.summary_fields("2026-09-04"), [
                    ("잔고", "$103.50"), ("가용", "$100.00"),
                    ("누적 손익", "+0.60 USDT"), ("엔진 수익률", "+0.30%"),
                    ("캠페인 횟수", "1회"), ("승률", "100.0% · 1승/1회")])

    def test_win_rate_excludes_open_campaign(self):
        with tempfile.TemporaryDirectory() as root:
            logs = os.path.join(root, "logs"); os.makedirs(logs)
            events = [
                {"t": "2026-09-04 01:00:00", "ev": "FILL", "symbol": "X", "side": "long", "role": "buy", "qty": 1, "pos_qty": 1, "pnl": -0.1},
                {"t": "2026-09-04 01:01:00", "ev": "FILL", "symbol": "X", "side": "long", "role": "trim", "qty": 1, "pos_qty": 0, "pnl": -0.2},
                {"t": "2026-09-04 02:00:00", "ev": "FILL", "symbol": "Y", "side": "short", "role": "buy", "qty": 2, "pos_qty": 2, "pnl": -0.1},
            ]
            with open(os.path.join(logs, "events.jsonl"), "w", encoding="utf-8") as fh:
                fh.write("\n".join(json.dumps(x) for x in events))
            with patch.object(notify, "LOGS", logs):
                fields = dict(notify.summary_fields("2026-09-04", balance=False))
            self.assertEqual(fields["캠페인 횟수"], "2회")
            self.assertEqual(fields["승률"], "0.0% · 0승/1회")

    def test_runtime_baseline_resets_all_campaign_metrics(self):
        with tempfile.TemporaryDirectory() as root:
            logs = os.path.join(root, "logs"); os.makedirs(logs)
            events = [
                {"t": "2026-09-04 01:00:00", "ev": "SIZING", "wallet": 100.0},
                {"t": "2026-09-04 01:01:00", "ev": "FILL", "symbol": "X", "side": "long", "role": "buy", "qty": 1, "pos_qty": 1, "pnl": -0.1},
                {"t": "2026-09-04 01:02:00", "ev": "FILL", "symbol": "X", "side": "long", "role": "trim", "qty": 1, "pos_qty": 0, "pnl": 1.1},
                {"t": "2026-09-04 02:01:00", "ev": "SIZING", "wallet": 55.0},
            ]
            with open(os.path.join(logs, "events.jsonl"), "w", encoding="utf-8") as fh:
                fh.write("\n".join(json.dumps(x) for x in events))
            with open(os.path.join(logs, "notify-baseline.json"), "w", encoding="utf-8") as fh:
                json.dump({"day": "2026-09-04", "start": "2026-09-04 02:00:00", "wallet": 55.0}, fh)
            with patch.object(notify, "LOGS", logs):
                fields = dict(notify.summary_fields("2026-09-04", balance=False))
            self.assertEqual(fields["누적 손익"], "+0.00 USDT")
            self.assertEqual(fields["엔진 수익률"], "+0.00%")
            self.assertEqual(fields["캠페인 횟수"], "0회")
            self.assertEqual(fields["승률"], "계산 전 · 0회 종료")

    def test_detail_is_above_dashboard_and_context_stays_last(self):
        with patch.object(notify, "summary_fields", return_value=[("잔고", "$100.00")]):
            blocks = notify._blocks("상태", "test", fields=[["포지션", "flat"]], ctx="KST")
        self.assertEqual(blocks[1]["type"], "section")
        self.assertEqual(blocks[1]["fields"][0]["text"], "*포지션*\nflat")
        self.assertEqual(blocks[2], {"type": "divider"})
        self.assertEqual(blocks[3]["fields"][0]["text"], "*잔고*\n$100.00")
        self.assertEqual(blocks[-1], {"type": "context", "elements": [{"type": "mrkdwn", "text": "KST"}]})


if __name__ == "__main__":
    unittest.main()
