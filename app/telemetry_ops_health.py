"""OPS-RELEVANCE-HEALTH-003 / OPS-COST-001: Ops telemetry endpoints.

Reads file-based JSON outputs from the bot pipeline:
  - relevance_health.json  -> enriched health + drift + family + archive
  - ops_cost_summary.json  -> file sizes, row counts, registry stats

No DB required. Best-effort: missing files return {ok: true, missing: true}.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ALERTS_DIR = Path(os.getenv("ROUTER_ALERT_DIR", "/home/eventedge/alerts"))

RELEVANCE_HEALTH_FILE = ALERTS_DIR / "relevance_health.json"
OPS_COST_FILE = ALERTS_DIR / "ops_cost_summary.json"


def _read_json(path: Path) -> dict | None:
    """Read JSON file, return None on any failure."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def build_telemetry_relevance_health() -> dict[str, Any]:
    """Return enriched relevance health (includes family, strategy, archive, drift_v2)."""
    now = datetime.now(timezone.utc).isoformat()
    data = _read_json(RELEVANCE_HEALTH_FILE)
    if data is None:
        return {"ok": True, "generated_at": now, "missing": True}
    data["ok"] = True
    data["generated_at"] = now
    return data


def build_telemetry_ops_cost() -> dict[str, Any]:
    """Return ops cost summary."""
    now = datetime.now(timezone.utc).isoformat()
    data = _read_json(OPS_COST_FILE)
    if data is None:
        return {"ok": True, "generated_at": now, "missing": True}
    data["ok"] = True
    data["generated_at"] = now
    return data


def build_telemetry_relevance_drift() -> dict[str, Any]:
    """Return compact drift-only slice from relevance health."""
    now = datetime.now(timezone.utc).isoformat()
    data = _read_json(RELEVANCE_HEALTH_FILE)
    if data is None:
        return {"ok": True, "generated_at": now, "missing": True}

    drift_v1 = data.get("checks", {}).get("drift", {})
    drift_v2 = data.get("drift_v2", {})

    return {
        "ok": True,
        "generated_at": now,
        "asof_iso": data.get("asof_iso"),
        "drift": drift_v1,
        "drift_v2": drift_v2,
    }
