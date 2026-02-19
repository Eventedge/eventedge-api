"""GET /api/v1/admin/telemetry/overview — top-level KPI counters.

Each section is independently safe-faulted: if a table is missing or a query
fails, the section returns ``available: false`` and the rest of the payload
is unaffected.  No migrations required.
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


# -- Section builders --------------------------------------------------------

def _service_health(cur: Any) -> dict[str, Any]:
    """Reuses service_heartbeats directly (same logic as health_services.py)."""
    if not _table_exists(cur, "service_heartbeats"):
        return _unavailable()

    cur.execute(
        "SELECT service_name, last_seen_at FROM service_heartbeats "
        "ORDER BY service_name"
    )
    rows = cur.fetchall()
    now = datetime.now(timezone.utc)
    total = len(rows)
    down = 0
    stale = 0
    for _, last_seen_at in rows:
        age = (now - last_seen_at).total_seconds()
        if age > 1800:
            down += 1
        elif age > 300:
            stale += 1
    return {
        "available": True,
        "total_services": total,
        "services_up": total - stale - down,
        "services_stale": stale,
        "services_down": down,
    }


def _users_summary(cur: Any) -> dict[str, Any]:
    if not _table_exists(cur, "user_tiers"):
        return _unavailable()

    cur.execute("SELECT COUNT(*) FROM user_tiers")
    total_users = cur.fetchone()[0]

    cur.execute(
        "SELECT COALESCE(tier, 'unknown'), COUNT(*) "
        "FROM user_tiers GROUP BY tier ORDER BY COUNT(*) DESC"
    )
    tier_distribution = {row[0]: row[1] for row in cur.fetchall()}

    active_24h = None
    if _table_exists(cur, "user_sessions"):
        cur.execute(
            "SELECT COUNT(DISTINCT user_id) FROM user_sessions "
            "WHERE last_event_at > NOW() - INTERVAL '24 hours'"
        )
        active_24h = cur.fetchone()[0]

    return {
        "available": True,
        "total_users": total_users,
        "tier_distribution": tier_distribution,
        "active_users_24h": active_24h,
    }


def _invites_summary(cur: Any) -> dict[str, Any]:
    if not _table_exists(cur, "invite_codes"):
        return _unavailable()

    cur.execute("SELECT COUNT(*) FROM invite_codes")
    total_codes = cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(*) FROM invite_codes "
        "WHERE created_at > NOW() - INTERVAL '24 hours'"
    )
    created_24h = cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(*) FROM invite_codes "
        "WHERE created_at > NOW() - INTERVAL '7 days'"
    )
    created_7d = cur.fetchone()[0]

    redeemed_total = None
    redeemed_24h = None
    redeemed_7d = None
    if _table_exists(cur, "invite_redemptions"):
        cur.execute("SELECT COUNT(*) FROM invite_redemptions")
        redeemed_total = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM invite_redemptions "
            "WHERE redeemed_at > NOW() - INTERVAL '24 hours'"
        )
        redeemed_24h = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM invite_redemptions "
            "WHERE redeemed_at > NOW() - INTERVAL '7 days'"
        )
        redeemed_7d = cur.fetchone()[0]

    return {
        "available": True,
        "total_codes": total_codes,
        "created_24h": created_24h,
        "created_7d": created_7d,
        "redeemed_total": redeemed_total,
        "redeemed_24h": redeemed_24h,
        "redeemed_7d": redeemed_7d,
    }


def _menu_toggles_summary(cur: Any) -> dict[str, Any]:
    if not _table_exists(cur, "menu_toggle_events"):
        return _unavailable()

    cur.execute(
        "SELECT COUNT(*) FROM menu_toggle_events "
        "WHERE ts > NOW() - INTERVAL '24 hours'"
    )
    events_24h = cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(*) FROM menu_toggle_events "
        "WHERE ts > NOW() - INTERVAL '7 days'"
    )
    events_7d = cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(DISTINCT user_id) FROM menu_toggle_events "
        "WHERE ts > NOW() - INTERVAL '24 hours'"
    )
    users_24h = cur.fetchone()[0]

    return {
        "available": True,
        "events_24h": events_24h,
        "events_7d": events_7d,
        "distinct_users_24h": users_24h,
    }


def _api_usage_summary(cur: Any) -> dict[str, Any]:
    if not _table_exists(cur, "metrics_api_calls"):
        return _unavailable()

    cur.execute(
        "SELECT COUNT(*), "
        "  COALESCE(SUM(CASE WHEN failure_count > 0 THEN failure_count ELSE 0 END), 0), "
        "  COALESCE(AVG(avg_latency_ms), 0) "
        "FROM metrics_api_calls "
        "WHERE hour > NOW() - INTERVAL '24 hours'"
    )
    row = cur.fetchone()
    total_rows = row[0]
    total_failures = int(row[1])
    avg_latency = round(float(row[2]), 1)

    cur.execute(
        "SELECT COALESCE(api_name, 'unknown'), "
        "  SUM(COALESCE(success_count, 0) + COALESCE(failure_count, 0)) AS calls, "
        "  SUM(COALESCE(failure_count, 0)) AS errors "
        "FROM metrics_api_calls "
        "WHERE hour > NOW() - INTERVAL '24 hours' "
        "GROUP BY api_name ORDER BY calls DESC LIMIT 20"
    )
    by_provider = {}
    for api_name, calls, errors in cur.fetchall():
        by_provider[api_name] = {"calls": int(calls), "errors": int(errors)}

    return {
        "available": True,
        "rows_24h": total_rows,
        "total_failures_24h": total_failures,
        "avg_latency_ms": avg_latency,
        "by_provider": by_provider,
    }


def _telemetry_summary(cur: Any) -> dict[str, Any]:
    if not _table_exists(cur, "telemetry_events"):
        return _unavailable()

    cur.execute(
        "SELECT COUNT(*) FROM telemetry_events "
        "WHERE created_at > NOW() - INTERVAL '24 hours'"
    )
    events_24h = cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(DISTINCT user_id) FROM telemetry_events "
        "WHERE created_at > NOW() - INTERVAL '24 hours'"
    )
    active_users_24h = cur.fetchone()[0]

    cur.execute(
        "SELECT COALESCE(event_type, 'unknown'), COUNT(*) "
        "FROM telemetry_events "
        "WHERE created_at > NOW() - INTERVAL '24 hours' "
        "GROUP BY event_type ORDER BY COUNT(*) DESC LIMIT 10"
    )
    top_events = {row[0]: row[1] for row in cur.fetchall()}

    return {
        "available": True,
        "events_24h": events_24h,
        "active_users_24h": active_users_24h,
        "top_event_types": top_events,
    }


def _alerts_summary(cur: Any) -> dict[str, Any]:
    if not _table_exists(cur, "alert_lifecycle"):
        return _unavailable()

    cur.execute(
        "SELECT COALESCE(event_type, 'unknown'), COUNT(*) "
        "FROM alert_lifecycle "
        "WHERE created_at > NOW() - INTERVAL '24 hours' "
        "GROUP BY event_type"
    )
    breakdown = {row[0]: row[1] for row in cur.fetchall()}
    fired = breakdown.get("fired", 0)

    return {
        "available": True,
        "fired_24h": fired,
        "lifecycle_24h": breakdown,
    }


def _abuse_summary(cur: Any) -> dict[str, Any]:
    if not _table_exists(cur, "abuse_rollup_hourly"):
        return _unavailable()

    cur.execute(
        "SELECT COALESCE(SUM(allows), 0), COALESCE(SUM(denies), 0) "
        "FROM abuse_rollup_hourly "
        "WHERE hour_ts > NOW() - INTERVAL '24 hours'"
    )
    row = cur.fetchone()
    return {
        "available": True,
        "allows_24h": int(row[0]),
        "denies_24h": int(row[1]),
    }


# -- Main builder -----------------------------------------------------------

_SECTIONS = [
    ("service_health", _service_health),
    ("users", _users_summary),
    ("invites", _invites_summary),
    ("menu_toggles", _menu_toggles_summary),
    ("api_usage", _api_usage_summary),
    ("telemetry", _telemetry_summary),
    ("alerts", _alerts_summary),
    ("abuse", _abuse_summary),
]


def build_telemetry_overview() -> dict[str, Any]:
    """Build the overview payload. Each section is independently fault-tolerant."""
    result: dict[str, Any] = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    errors: list[str] = []

    with get_conn() as conn:
        with conn.cursor() as cur:
            for name, builder in _SECTIONS:
                try:
                    result[name] = builder(cur)
                except Exception as exc:
                    result[name] = _unavailable(f"query error: {type(exc).__name__}")
                    errors.append(f"{name}: {exc}")

    if errors:
        result["_errors"] = errors

    return result
