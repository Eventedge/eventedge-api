"""Stub: GET /api/v1/admin/telemetry/summary

Returns 501 with the list of planned telemetry endpoints.
This lets the admin UI discover available/planned endpoints without breaking.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .telemetry_contracts import PLANNED_ENDPOINTS


def build_telemetry_summary() -> dict:
    return {
        "ok": False,
        "error": "not_implemented",
        "message": "Telemetry endpoints are planned but not yet implemented.",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "planned_endpoints": PLANNED_ENDPOINTS,
    }
