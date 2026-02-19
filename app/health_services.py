"""GET /api/v1/admin/health/services — read service_heartbeats table.

Returns status buckets:
  up    — last_seen_at within STALE_THRESHOLD_S (default 300s)
  stale — last_seen_at within DOWN_THRESHOLD_S  (default 1800s)
  down  — older than DOWN_THRESHOLD_S or no row at all
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from .db import get_conn

STALE_S = int(os.getenv("HEALTH_STALE_THRESHOLD_S", "300"))
DOWN_S = int(os.getenv("HEALTH_DOWN_THRESHOLD_S", "1800"))


def _status(age_seconds: float) -> str:
    if age_seconds <= STALE_S:
        return "up"
    if age_seconds <= DOWN_S:
        return "stale"
    return "down"


def build_health_services() -> dict[str, Any]:
    """Query service_heartbeats and return status for all known services."""
    now = datetime.now(timezone.utc)
    services: list[dict[str, Any]] = []

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT service_name, last_seen_at, meta "
                "FROM service_heartbeats "
                "ORDER BY service_name"
            )
            rows = cur.fetchall()

    for service_name, last_seen_at, meta in rows:
        age_s = (now - last_seen_at).total_seconds()
        services.append({
            "service_key": service_name,
            "status": _status(age_s),
            "age_s": round(age_s, 1),
            "last_seen": last_seen_at.isoformat(),
            "detail": meta if isinstance(meta, dict) else {},
        })

    return {
        "now_utc": now.isoformat(),
        "thresholds": {"stale_s": STALE_S, "down_s": DOWN_S},
        "services": services,
    }
