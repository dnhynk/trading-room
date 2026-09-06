"""AWS CLI target verification and narrowly scoped SSH staging. Never packages secrets."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tarfile
import time

from common.paths import PROJECT_ROOT as ROOT, STATE_ROOT
INSTANCE = os.environ.get("TRADING_ROOM_AWS_INSTANCE", "")
HOST = os.environ.get("TRADING_ROOM_SSH_HOST", "")
BASE = os.environ.get("TRADING_ROOM_REMOTE_ROOT", "/opt/trading-room-c")
DEFAULT_AWS = os.environ.get("TRADING_ROOM_AWS_CLI") or shutil.which("aws") or "aws"
DEFAULT_KEY = Path(os.environ.get("TRADING_ROOM_SSH_KEY", "~/.ssh/trading-room")).expanduser()


def ssh_args(key):
    if not HOST:
        raise RuntimeError("set TRADING_ROOM_SSH_HOST before using remote operations")
    return ["ssh", "-i", str(key), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10", "ubuntu@"+HOST]


def remote(key, command, data=None):
    result = subprocess.run(ssh_args(key)+[command], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
    if result.returncode:
        # Commands never contain secrets, but do not relay arbitrary server errors.
        raise RuntimeError(f"remote operation failed ({result.returncode})")
    return result.stdout


def verify(aws):
    if not INSTANCE or not HOST:
        raise RuntimeError("set TRADING_ROOM_AWS_INSTANCE and TRADING_ROOM_SSH_HOST before staging")
    result = subprocess.run([str(aws), "ec2", "describe-instances", "--region", "ap-northeast-2", "--instance-ids", INSTANCE,
                             "--query", "Reservations[].Instances[].{Id:InstanceId,IP:PublicIpAddress,State:State.Name}", "--output", "json", "--no-cli-pager"],
                            capture_output=True, check=True, timeout=30)
    rows = json.loads(result.stdout)
    if len(rows) != 1 or rows[0] != dict(Id=INSTANCE, IP=HOST, State="running"):
        raise RuntimeError("AWS instance identity/IP/state mismatch")
    return rows[0]

