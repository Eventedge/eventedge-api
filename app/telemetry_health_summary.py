"""MEGA-FIX-001D: Health confluence — aggregates PM + TA + Alerts health.

GET /api/v1/admin/telemetry/health_summary
Returns a single health overview with green/yellow/red status.
Best-effort: if a source is missing, includes {missing: true}.
No DB — reads JSON files and delegates to existing builders.
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
TA_HEALTH_FILE = ALERTS_DIR / "ta_ops_health.json"
ALERT_HEALTH_FILE = ALERTS_DIR / "router_alert_health.json"
ALERT_DELIVERY_FILE = ALERTS_DIR / "router_alert_delivery_last.json"


def _read_json(path: Path) -> dict | None:
    """Read JSON file, return None on any failure."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _classify_pm(data: dict | None) -> tuple[str, list[str]]:
    """Return (status, reasons) for PM health."""
    if data is None:
        return "red", ["pm_health.json missing"]

    summary = data.get("summary", {})
    if summary.get("all_fresh"):
        return "green", []

    reasons = []
    state_counts = summary.get("state_counts", {})
    if state_counts.get("dead", 0) > 0:
        reasons.append(f"{state_counts['dead']} PM asset(s) dead")
    if state_counts.get("stale", 0) > 0:
        reasons.append(f"{state_counts['stale']} PM asset(s) stale")
    if state_counts.get("degraded", 0) > 0:
        reasons.append(f"{state_counts['degraded']} PM asset(s) degraded")
    if state_counts.get("missing", 0) > 0:
        reasons.append(f"{state_counts['missing']} PM asset(s) missing")

    if state_counts.get("dead", 0) > 0 or state_counts.get("missing", 0) > 0:
        return "red", reasons
    if state_counts.get("stale", 0) > 0 or state_counts.get("degraded", 0) > 0:
        return "yellow", reasons
    return "green", reasons


def _classify_ta(data: dict | None) -> tuple[str, list[str]]:
    """Return (status, reasons) for TA health."""
    if data is None:
        return "red", ["ta_ops_health.json missing"]

    reasons = []
    artifacts = data.get("artifacts", {})
    overall = artifacts.get("overall", {})
    rollups = data.get("rollups", {})

    assets_ok = overall.get("assets_ok", 0)
    assets_total = overall.get("assets_total", 0)
    if assets_ok < assets_total:
        reasons.append(f"TA artifacts: {assets_ok}/{assets_total} OK")

    if not rollups.get("fresh", False):
        reasons.append("TA rollups stale")

    if assets_ok == 0:
        return "red", reasons
    if reasons:
        return "yellow", reasons
    return "green", reasons


def _classify_alerts(health: dict | None, delivery: dict | None) -> tuple[str, list[str]]:
    """Return (status, reasons) for alerts health."""
    if health is None:
        return "yellow", ["router_alert_health.json missing"]

    reasons = []
    last_tick = health.get("last_tick")
    if last_tick:
        try:
            lt = datetime.fromisoformat(last_tick)
            if lt.tzinfo is None:
                lt = lt.replace(tzinfo=timezone.utc)
            age_s = (datetime.now(timezone.utc) - lt).total_seconds()
            if age_s > 600:  # >10min since last tick
                reasons.append(f"alert tick stale ({age_s / 60:.0f}m ago)")
        except (ValueError, TypeError):
            pass

    if delivery:
        fail_count = delivery.get("fail_count", 0)
        if fail_count > 0:
            reasons.append(f"{fail_count} delivery failure(s)")

    if reasons:
        return "yellow", reasons
    return "green", reasons


def _overall_status(pm_status: str, ta_status: str, alerts_status: str) -> str:
    """Derive overall status from subsystem statuses."""
    statuses = [pm_status, ta_status, alerts_status]
    if "red" in statuses:
        return "red"
    if "yellow" in statuses:
        return "yellow"
    return "green"


def build_health_summary() -> dict[str, Any]:
    """Build aggregated health summary from all subsystems."""
    now = datetime.now(timezone.utc)

    # Read all sources (best-effort)
    pm_data = _read_json(PM_HEALTH_FILE)
    ta_data = _read_json(TA_HEALTH_FILE)
    alert_health = _read_json(ALERT_HEALTH_FILE)
    alert_delivery = _read_json(ALERT_DELIVERY_FILE)

    # Classify each subsystem
    pm_status, pm_reasons = _classify_pm(pm_data)
    ta_status, ta_reasons = _classify_ta(ta_data)
    alerts_status, alerts_reasons = _classify_alerts(alert_health, alert_delivery)

    overall = _overall_status(pm_status, ta_status, alerts_status)
    all_reasons = pm_reasons + ta_reasons + alerts_reasons

    # Build PM summary
    pm_summary: dict[str, Any] = {"missing": True} if pm_data is None else {
        "status": pm_status,
        "asof_iso": pm_data.get("asof_iso"),
        "state_counts": pm_data.get("summary", {}).get("state_counts", {}),
        "all_fresh": pm_data.get("summary", {}).get("all_fresh", False),
    }

    # Build TA summary
    ta_summary: dict[str, Any] = {"missing": True} if ta_data is None else {
        "status": ta_status,
        "as_of": ta_data.get("as_of"),
        "assets_ok": ta_data.get("artifacts", {}).get("overall", {}).get("assets_ok", 0),
        "assets_total": ta_data.get("artifacts", {}).get("overall", {}).get("assets_total", 0),
        "rollup_fresh": ta_data.get("rollups", {}).get("fresh", False),
    }

    # Build alerts summary
    alerts_summary: dict[str, Any] = {
        "status": alerts_status,
        "health": {"missing": True} if alert_health is None else {
            "last_tick": alert_health.get("last_tick"),
            "triggers_detected": alert_health.get("triggers_detected", 0),
            "messages_sent": alert_health.get("messages_sent", 0),
        },
        "delivery_last": {"missing": True} if alert_delivery is None else {
            "last_delivery": alert_delivery.get("last_delivery"),
            "fail_count": alert_delivery.get("fail_count", 0),
        },
    }

    return {
        "ok": True,
        "generated_at": now.isoformat(),
        "asof_ts": int(now.timestamp()),
        "pm": pm_summary,
        "ta": ta_summary,
        "alerts": alerts_summary,
        "overall": {
            "status": overall,
            "reasons": all_reasons,
        },
    }
