"""GET /api/v1/admin/telemetry/alerts — alert lifecycle, firing rates, alertd status.

Each sub-block is independently fault-tolerant.  No migrations required.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .db import get_conn


def _table_exists(cur: Any, name: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = %s",
        (name,),
    )
    return cur.fetchone() is not None


def _unavailable(reason: str = "table not found") -> dict[str, Any]:
    return {"available": False, "reason": reason}


def _iso(val: Any) -> str | None:
    if val is None:
        return None
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val)


def _lifecycle(cur: Any) -> dict[str, Any]:
    if not _table_exists(cur, "alert_lifecycle"):
        return _unavailable()

    cur.execute(
        "SELECT COALESCE(event_type, 'unknown'), COUNT(*) "
        "FROM alert_lifecycle "
        "WHERE created_at > NOW() - INTERVAL '24 hours' "
        "GROUP BY event_type ORDER BY COUNT(*) DESC"
    )
    breakdown_24h = {row[0]: row[1] for row in cur.fetchall()}

    cur.execute(
        "SELECT COALESCE(event_type, 'unknown'), COUNT(*) "
        "FROM alert_lifecycle "
        "WHERE created_at > NOW() - INTERVAL '7 days' "
        "GROUP BY event_type ORDER BY COUNT(*) DESC"
    )
    breakdown_7d = {row[0]: row[1] for row in cur.fetchall()}

    cur.execute(
        "SELECT COUNT(*) FROM alert_lifecycle "
        "WHERE created_at > NOW() - INTERVAL '24 hours'"
    )
    total_24h = cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(*) FROM alert_lifecycle "
        "WHERE created_at > NOW() - INTERVAL '7 days'"
    )
    total_7d = cur.fetchone()[0]

    return {
        "available": True,
        "total_24h": total_24h,
        "total_7d": total_7d,
        "fired_24h": breakdown_24h.get("fired", 0),
        "fired_7d": breakdown_7d.get("fired", 0),
        "breakdown_24h": breakdown_24h,
        "breakdown_7d": breakdown_7d,
    }


def _recent_alerts(cur: Any) -> list[dict[str, Any]]:
    if not _table_exists(cur, "alert_lifecycle"):
        return []

    cur.execute(
        "SELECT event_type, created_at, "
        "  COALESCE(metadata::text, '') "
        "FROM alert_lifecycle "
        "ORDER BY created_at DESC LIMIT 20"
    )
    result = []
    for row in cur.fetchall():
        result.append({
            "event_type": row[0],
            "created_at": _iso(row[1]),
            "payload_preview": row[2][:200] if row[2] else None,
        })
    return result


def _alertd_status(cur: Any) -> dict[str, Any]:
    if not _table_exists(cur, "service_heartbeats"):
        return _unavailable()

    now = datetime.now(timezone.utc)
    cur.execute(
        "SELECT service_name, last_seen_at FROM service_heartbeats "
        "WHERE service_name LIKE 'eventedge-alertd%%' OR service_name LIKE 'alertd%%' "
        "ORDER BY last_seen_at DESC"
    )
    rows = cur.fetchall()
    if not rows:
        return {"available": True, "daemons": []}

    daemons = []
    for name, last_seen in rows:
        age = (now - last_seen).total_seconds()
        status = "up" if age < 300 else ("stale" if age < 1800 else "down")
        daemons.append({
            "name": name,
            "status": status,
            "last_seen_at": _iso(last_seen),
            "age_s": round(age, 1),
        })

    return {"available": True, "daemons": daemons}


def build_telemetry_alerts() -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    alerts: dict[str, Any] = {}
    errors: list[str] = []

    with get_conn() as conn:
        with conn.cursor() as cur:
            try:
                lc = _lifecycle(cur)
                if lc.get("available"):
                    alerts["total_24h"] = lc["total_24h"]
                    alerts["total_7d"] = lc["total_7d"]
                    alerts["fired_24h"] = lc["fired_24h"]
                    alerts["fired_7d"] = lc["fired_7d"]
                    alerts["breakdown_24h"] = lc["breakdown_24h"]
                    alerts["breakdown_7d"] = lc["breakdown_7d"]
                else:
                    alerts["total_24h"] = None
                    alerts["total_7d"] = None
                    alerts["fired_24h"] = None
                    alerts["fired_7d"] = None
                    alerts["breakdown_24h"] = {}
                    alerts["breakdown_7d"] = {}
                    errors.append(f"lifecycle: {lc.get('reason')}")
            except Exception as exc:
                conn.rollback()
                alerts["total_24h"] = None
                alerts["total_7d"] = None
                alerts["fired_24h"] = None
                alerts["fired_7d"] = None
                alerts["breakdown_24h"] = {}
                alerts["breakdown_7d"] = {}
                errors.append(f"lifecycle: {type(exc).__name__}")

            try:
                alerts["recent"] = _recent_alerts(cur)
            except Exception as exc:
                conn.rollback()
                alerts["recent"] = []
                errors.append(f"recent: {type(exc).__name__}")

            try:
                ad = _alertd_status(cur)
                alerts["alertd"] = ad if ad.get("available") else {"daemons": []}
                if not ad.get("available"):
                    errors.append(f"alertd: {ad.get('reason')}")
            except Exception as exc:
                conn.rollback()
                alerts["alertd"] = {"daemons": []}
                errors.append(f"alertd: {type(exc).__name__}")

    result: dict[str, Any] = {"ok": True, "generated_at": now, "alerts": alerts}
    if errors:
        result["_errors"] = errors
    return result
