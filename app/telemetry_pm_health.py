"""PM-OPS-001B: PM health telemetry — reads pm_health.json snapshot.

Returns the latest PM health snapshot written by
scripts/pm_health_snapshot.py (atomic JSON, hourly).
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
PM_HEALTH_FILE = ALERTS_DIR / "pm_health.json"


def build_telemetry_pm_health() -> dict[str, Any]:
    """Read pm_health.json and return it with ok/generated_at wrapper."""
    now = datetime.now(timezone.utc)

    if not PM_HEALTH_FILE.exists():
        return {
            "ok": False,
            "generated_at": now.isoformat(),
            "error": "pm_health.json not found",
        }

    try:
        data = json.loads(PM_HEALTH_FILE.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return {
            "ok": False,
            "generated_at": now.isoformat(),
            "error": f"Failed to read pm_health.json: {e}",
        }

    # Check staleness of the snapshot itself
    asof_ts = data.get("asof_ts")
    snapshot_age_s = None
    if asof_ts:
        snapshot_age_s = round(now.timestamp() - asof_ts, 1)

    return {
        "ok": True,
        "generated_at": now.isoformat(),
        "snapshot_age_s": snapshot_age_s,
        **data,
    }
