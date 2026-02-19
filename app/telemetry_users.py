"""GET /api/v1/admin/telemetry/users — user population, tiers, activity, invites.

Each sub-block is independently fault-tolerant: if a table or column is missing
the block returns ``available: false`` and the rest of the payload is unaffected.
No migrations required.
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
    """Safely convert a datetime-ish value to ISO-8601 string."""
    if val is None:
        return None
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val)


# -- Sub-block builders ------------------------------------------------------

def _counts(cur: Any) -> dict[str, Any]:
    """total_users, new_24h, new_7d."""
    if not _table_exists(cur, "user_tiers"):
        return _unavailable()

    cur.execute("SELECT COUNT(*) FROM user_tiers")
    total = cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(*) FROM user_tiers "
        "WHERE created_at > NOW() - INTERVAL '24 hours'"
    )
    new_24h = cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(*) FROM user_tiers "
        "WHERE created_at > NOW() - INTERVAL '7 days'"
    )
    new_7d = cur.fetchone()[0]

    return {"available": True, "total_users": total, "new_24h": new_24h, "new_7d": new_7d}


def _activity(cur: Any) -> dict[str, Any]:
    """active_24h, active_7d from user_sessions."""
    if not _table_exists(cur, "user_sessions"):
        return _unavailable()

    cur.execute(
        "SELECT COUNT(DISTINCT user_id) FROM user_sessions "
        "WHERE last_event_at > NOW() - INTERVAL '24 hours'"
    )
    active_24h = cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(DISTINCT user_id) FROM user_sessions "
        "WHERE last_event_at > NOW() - INTERVAL '7 days'"
    )
    active_7d = cur.fetchone()[0]

    return {"available": True, "active_24h": active_24h, "active_7d": active_7d}


def _tiers(cur: Any) -> list[dict[str, Any]]:
    """Tier distribution as [{tier, count}]."""
    if not _table_exists(cur, "user_tiers"):
        return []

    cur.execute(
        "SELECT COALESCE(tier, 'unknown'), COUNT(*) "
        "FROM user_tiers GROUP BY tier ORDER BY COUNT(*) DESC"
    )
    return [{"tier": row[0], "count": row[1]} for row in cur.fetchall()]


def _last_seen_buckets(cur: Any) -> dict[str, Any]:
    """Bucket users by last session activity: h24, d7, gt7d, unknown."""
    if not _table_exists(cur, "user_tiers"):
        return _unavailable()

    has_sessions = _table_exists(cur, "user_sessions")

    if not has_sessions:
        # Without sessions we can only report total as unknown
        cur.execute("SELECT COUNT(*) FROM user_tiers")
        total = cur.fetchone()[0]
        return {"available": True, "h24": 0, "d7": 0, "gt7d": 0, "unknown": total}

    # Left join user_tiers onto the latest session per user
    cur.execute(
        "SELECT "
        "  SUM(CASE WHEN s.last_event_at > NOW() - INTERVAL '24 hours' THEN 1 ELSE 0 END), "
        "  SUM(CASE WHEN s.last_event_at > NOW() - INTERVAL '7 days' "
        "            AND s.last_event_at <= NOW() - INTERVAL '24 hours' THEN 1 ELSE 0 END), "
        "  SUM(CASE WHEN s.last_event_at <= NOW() - INTERVAL '7 days' THEN 1 ELSE 0 END), "
        "  SUM(CASE WHEN s.last_event_at IS NULL THEN 1 ELSE 0 END) "
        "FROM user_tiers ut "
        "LEFT JOIN LATERAL ("
        "  SELECT MAX(last_event_at) AS last_event_at "
        "  FROM user_sessions WHERE user_id = ut.user_id"
        ") s ON TRUE"
    )
    row = cur.fetchone()
    return {
        "available": True,
        "h24": int(row[0] or 0),
        "d7": int(row[1] or 0),
        "gt7d": int(row[2] or 0),
        "unknown": int(row[3] or 0),
    }


def _recent_users(cur: Any) -> list[dict[str, Any]]:
    """20 most recently created users with optional invite info."""
    if not _table_exists(cur, "user_tiers"):
        return []

    has_sessions = _table_exists(cur, "user_sessions")
    has_redemptions = _table_exists(cur, "invite_redemptions")
    has_invite_codes = _table_exists(cur, "invite_codes")

    # Build the query dynamically based on available tables
    select = "SELECT ut.user_id, ut.tier, ut.created_at"
    joins = ""
    order = "ORDER BY ut.created_at DESC LIMIT 20"

    # Last seen from sessions
    if has_sessions:
        select += ", ls.last_event_at"
        joins += (
            " LEFT JOIN LATERAL ("
            "  SELECT MAX(last_event_at) AS last_event_at "
            "  FROM user_sessions WHERE user_id = ut.user_id"
            ") ls ON TRUE"
        )
    else:
        select += ", NULL AS last_event_at"

    # Invite info from redemptions + codes
    if has_redemptions:
        select += ", ir.code AS invite_code"
        joins += (
            " LEFT JOIN LATERAL ("
            "  SELECT code FROM invite_redemptions "
            "  WHERE user_id = ut.user_id ORDER BY redeemed_at LIMIT 1"
            ") ir ON TRUE"
        )
    else:
        select += ", NULL AS invite_code"

    if has_redemptions and has_invite_codes:
        select += ", ic.created_by AS invite_source"
        joins += (
            " LEFT JOIN invite_codes ic ON ic.code = ir.code"
        )
    else:
        select += ", NULL AS invite_source"

    query = f"{select} FROM user_tiers ut{joins} {order}"
    cur.execute(query)
    rows = cur.fetchall()

    result = []
    for row in rows:
        entry: dict[str, Any] = {
            "user_id": row[0],
            "tier": row[1],
            "created_at": _iso(row[2]),
            "last_seen_at": _iso(row[3]),
        }
        if row[4] is not None:
            entry["invite_code"] = row[4]
        if row[5] is not None:
            entry["invite_source"] = row[5]
        result.append(entry)

    return result


# -- Main builder -----------------------------------------------------------

def build_telemetry_users() -> dict[str, Any]:
    """Build the users telemetry payload. Each sub-block is fault-tolerant."""
    now = datetime.now(timezone.utc).isoformat()
    users: dict[str, Any] = {}
    errors: list[str] = []

    with get_conn() as conn:
        with conn.cursor() as cur:
            # Counts: total_users, new_24h, new_7d
            try:
                counts = _counts(cur)
                if counts.get("available"):
                    users["total_users"] = counts["total_users"]
                    users["new_24h"] = counts["new_24h"]
                    users["new_7d"] = counts["new_7d"]
                else:
                    users["total_users"] = None
                    users["new_24h"] = None
                    users["new_7d"] = None
                    errors.append(f"counts: {counts.get('reason', 'unavailable')}")
            except Exception as exc:
                users["total_users"] = None
                users["new_24h"] = None
                users["new_7d"] = None
                errors.append(f"counts: {type(exc).__name__}")

            # Activity: active_24h, active_7d
            try:
                activity = _activity(cur)
                if activity.get("available"):
                    users["active_24h"] = activity["active_24h"]
                    users["active_7d"] = activity["active_7d"]
                else:
                    users["active_24h"] = None
                    users["active_7d"] = None
                    errors.append(f"activity: {activity.get('reason', 'unavailable')}")
            except Exception as exc:
                users["active_24h"] = None
                users["active_7d"] = None
                errors.append(f"activity: {type(exc).__name__}")

            # Tiers
            try:
                users["tiers"] = _tiers(cur)
            except Exception as exc:
                users["tiers"] = []
                errors.append(f"tiers: {type(exc).__name__}")

            # Last-seen buckets
            try:
                buckets = _last_seen_buckets(cur)
                if buckets.get("available"):
                    users["last_seen_buckets"] = {
                        "h24": buckets["h24"],
                        "d7": buckets["d7"],
                        "gt7d": buckets["gt7d"],
                        "unknown": buckets["unknown"],
                    }
                else:
                    users["last_seen_buckets"] = None
                    errors.append(f"last_seen_buckets: {buckets.get('reason', 'unavailable')}")
            except Exception as exc:
                users["last_seen_buckets"] = None
                errors.append(f"last_seen_buckets: {type(exc).__name__}")

            # Recent users
            try:
                users["recent"] = _recent_users(cur)
            except Exception as exc:
                users["recent"] = []
                errors.append(f"recent: {type(exc).__name__}")

    result: dict[str, Any] = {
        "ok": True,
        "generated_at": now,
        "users": users,
    }
    if errors:
        result["_errors"] = errors
    return result
