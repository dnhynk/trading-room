"""Code/configuration stay in the checkout; recordings and output live elsewhere."""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_ROOT = Path(os.environ.get('TRADING_ROOM_HOME', PROJECT_ROOT.parent / 'trading-room-state')).expanduser().resolve()


def runtime_root(project_root=PROJECT_ROOT):
    """Allow isolated test workspaces without redirecting them to production data."""
    root = Path(project_root).resolve()
    return STATE_ROOT if root == PROJECT_ROOT else root
