"""ALERTS-POLISH-001: Router alert health snapshot.

Returns filesystem stats for all alert-related files.
No DB — just os.stat() calls.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ALERTS_DIR = Path(
    os.getenv("ROUTER_ALERT_DIR", "/home/eventedge/alerts")
)

_FILES = {
    "prefs": "router_alert_prefs.json",
    "audit": "router_alert_prefs_audit.jsonl",
    "delivery": "router_alert_delivery.jsonl",
    "delivery_last": "router_alert_delivery_last.json",
    "health": "router_alert_health.json",
    "state": "router_state.json",
}


def _file_info(path: Path) -> dict[str, Any]:
    """Return existence + mtime + size for a file."""
    if not path.exists():
        return {"exists": False}
    try:
        st = path.stat()
        return {
            "exists": True,
            "size_bytes": st.st_size,
            "mtime_iso": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            "mtime_epoch": int(st.st_mtime),
        }
    except OSError:
        return {"exists": False}


def build_alert_health() -> dict[str, Any]:
    """Build health snapshot for all alert files."""
    now = datetime.now(timezone.utc)
    files = {}
    for key, name in _FILES.items():
        files[key] = _file_info(ALERTS_DIR / name)

    # Extract last tick timestamp from health.json if present
    last_tick = None
    health_path = ALERTS_DIR / "router_alert_health.json"
    if health_path.exists():
        try:
            data = json.loads(health_path.read_text())
            last_tick = data.get("last_tick")
        except (json.JSONDecodeError, OSError):
            pass

    # Subscriber count from prefs
    sub_count = 0
    prefs_path = ALERTS_DIR / "router_alert_prefs.json"
    if prefs_path.exists():
        try:
            data = json.loads(prefs_path.read_text())
            sub_count = len(data) if isinstance(data, dict) else 0
        except (json.JSONDecodeError, OSError):
            pass

    return {
        "ok": True,
        "generated_at": now.isoformat(),
        "alerts_dir": str(ALERTS_DIR),
        "files": files,
        "last_tick": last_tick,
        "subscriber_count": sub_count,
    }
