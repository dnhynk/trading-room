"""AWS CLI target verification and narrowly scoped SSH staging. Never packages secrets."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import shlex
import subprocess
import tarfile
import time

from common.paths import PROJECT_ROOT as ROOT, STATE_ROOT
INSTANCE = "i-0db0329527b7b3533"
HOST = "52.78.144.101"
BASE = "/home/ubuntu/trading-room-c"
DEFAULT_AWS = STATE_ROOT / "logs/tools/aws-cli-v2/extracted/Amazon/AWSCLIV2/aws.exe"
DEFAULT_KEY = Path("D:/repos/arbitrage/data/reports/aws_probe/alphaverdict-probe.pem")


def ssh_args(key):
    return ["ssh", "-i", str(key), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10", "ubuntu@"+HOST]


def remote(key, command, data=None):
    result = subprocess.run(ssh_args(key)+[command], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
    if result.returncode:
        # Commands never contain secrets, but do not relay arbitrary server errors.
        raise RuntimeError(f"remote operation failed ({result.returncode})")
    return result.stdout


def verify(aws):
    result = subprocess.run([str(aws), "ec2", "describe-instances", "--region", "ap-northeast-2", "--instance-ids", INSTANCE,
                             "--query", "Reservations[].Instances[].{Id:InstanceId,IP:PublicIpAddress,State:State.Name}", "--output", "json", "--no-cli-pager"],
                            capture_output=True, check=True, timeout=30)
    rows = json.loads(result.stdout)
    if len(rows) != 1 or rows[0] != dict(Id=INSTANCE, IP=HOST, State="running"):
        raise RuntimeError("AWS instance identity/IP/state mismatch")
    return rows[0]


