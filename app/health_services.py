"""service_heartbeats — read + ingest helpers.

GET  /api/v1/admin/health/services  — read all rows (existing)
POST /api/v1/admin/health/heartbeat — remote service heartbeat ingest (OPS-ACP-003)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from .db import get_conn

STALE_S = int(os.getenv("HEALTH_STALE_THRESHOLD_S", "300"))
DOWN_S = int(os.getenv("HEALTH_DOWN_THRESHOLD_S", "1800"))

HEARTBEAT_INGEST_SECRET = os.getenv("HEARTBEAT_INGEST_SECRET", "")

# v1 allowlist — only these service names may write via the HTTP endpoint.
_ALLOWED_SERVICES: set[str] = {"acp-adapter"}


def _status(age_seconds: float) -> str:
    if age_seconds <= STALE_S:
        return "up"
    if age_seconds <= DOWN_S:
        return "stale"
    return "down"


REQUIRED_SERVICES = ("eventedge-bot", "eventedge-alertd")


def build_health_services() -> dict[str, Any]:
    """Query service_heartbeats and return status for all known services.

    Always includes REQUIRED_SERVICES even when no DB row exists (shown as
    ``down`` with ``last_seen: null``).
    """
    now = datetime.now(timezone.utc)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT service_name, last_seen_at, meta "
                "FROM service_heartbeats "
                "ORDER BY service_name"
            )
            rows = cur.fetchall()

    seen: dict[str, dict[str, Any]] = {}
    for service_name, last_seen_at, meta in rows:
        age_s = (now - last_seen_at).total_seconds()
        seen[service_name] = {
            "service_key": service_name,
            "status": _status(age_s),
            "age_s": round(age_s, 1),
            "last_seen": last_seen_at.isoformat(),
            "detail": meta if isinstance(meta, dict) else {},
        }

    # Ensure required services are always present
    for svc in REQUIRED_SERVICES:
        if svc not in seen:
            seen[svc] = {
                "service_key": svc,
                "status": "down",
                "age_s": 999999,
                "last_seen": None,
                "detail": {},
            }

    services = sorted(seen.values(), key=lambda s: s["service_key"])

    return {
        "now_utc": now.isoformat(),
        "thresholds": {"stale_s": STALE_S, "down_s": DOWN_S},
        "services": services,
    }


def ingest_heartbeat(service_name: str, meta: dict | None) -> dict[str, Any]:
    """Upsert a row into service_heartbeats for a remote service."""
    now = datetime.now(timezone.utc)
    meta_json = json.dumps(meta or {})

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO service_heartbeats(service_name, last_seen_at, meta) "
                "VALUES (%s, NOW(), %s::jsonb) "
                "ON CONFLICT (service_name) "
                "DO UPDATE SET last_seen_at = EXCLUDED.last_seen_at, "
                "             meta = EXCLUDED.meta",
                (service_name, meta_json),
            )
        conn.commit()

    return {"ok": True, "service_name": service_name, "ts": now.isoformat()}
