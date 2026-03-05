"""EB-TA-CHARTS-021: ChartFeed health telemetry — reads chartfeed JSON files.

Returns the latest chartfeed health + last run summary for admin dashboard.
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
HEALTH_FILE = ALERTS_DIR / "chartfeed_health.json"
LEDGER_FILE = ALERTS_DIR / "chartfeed_runs.json"


def _read_json_safe(path: Path) -> tuple[Any, float | None]:
    """Read JSON file and return (data, mtime) or (None, None)."""
    if not path.exists():
        return None, None
    try:
        mtime = path.stat().st_mtime
        data = json.loads(path.read_text())
        return data, mtime
    except (json.JSONDecodeError, OSError):
        return None, None


def build_telemetry_chartfeed() -> dict[str, Any]:
    """Build chartfeed health telemetry payload."""
    now = datetime.now(timezone.utc)

    health, health_mtime = _read_json_safe(HEALTH_FILE)
    ledger, ledger_mtime = _read_json_safe(LEDGER_FILE)

    if health is None and ledger is None:
        return {
            "ok": False,
            "generated_at": now.isoformat(),
            "error": "chartfeed health/ledger files not found",
        }

    # Build payload from health file
    result: dict[str, Any] = {
        "ok": True,
        "generated_at": now.isoformat(),
    }

    if health:
        result["last_run_at"] = health.get("last_run_at")
        result["last_ok"] = health.get("last_ok", False)
        result["posted_count"] = health.get("posted_count", 0)
        result["failed_count"] = health.get("failed_count", 0)
        result["skipped_count"] = health.get("skipped_count", 0)
        result["why_count"] = health.get("why_count", 0)
        result["destination"] = health.get("destination", "")
        result["status"] = health.get("status", "unknown")

        # Compute age of last run
        last_run_at = health.get("last_run_at")
        if last_run_at:
            try:
                last_ts = datetime.fromisoformat(last_run_at)
                result["last_run_age_s"] = round((now - last_ts).total_seconds(), 1)
            except (ValueError, TypeError):
                pass

    # Enrich from last ledger entry
    if isinstance(ledger, list) and ledger:
        last_entry = ledger[-1]
        result["run_id"] = last_entry.get("run_id", "")
        result["preset_source"] = last_entry.get("preset_source", "")
        result["sparse_policy"] = last_entry.get("sparse_policy", "")
        result["duration_s"] = last_entry.get("duration_s")
        result["fallback_triggered"] = last_entry.get("fallback_triggered", 0)
        result["total_runs_in_ledger"] = len(ledger)

    # File metadata
    result["files"] = {
        "health": {
            "path": str(HEALTH_FILE),
            "exists": HEALTH_FILE.exists(),
            "mtime": datetime.fromtimestamp(health_mtime, tz=timezone.utc).isoformat() if health_mtime else None,
        },
        "ledger": {
            "path": str(LEDGER_FILE),
            "exists": LEDGER_FILE.exists(),
            "mtime": datetime.fromtimestamp(ledger_mtime, tz=timezone.utc).isoformat() if ledger_mtime else None,
        },
    }

    return result
