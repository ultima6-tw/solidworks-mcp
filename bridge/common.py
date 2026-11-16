"""Shared settings for bridge/host.py and bridge/client.py."""
import json
import os
from pathlib import Path

DEFAULT_PORT = 47110
TOKEN_HEADER = "X-SolidWorks-MCP-Token"


def state_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "solidworks-mcp"


def state_file() -> Path:
    """Written by the host at startup: {"port": int, "token": str, "pid": int}.
    Lives in the user's profile, so only the same Windows account can read
    the token — the SSH session and the desktop session of one user both can."""
    return state_dir() / "bridge.json"


def read_state():
    try:
        return json.loads(state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
