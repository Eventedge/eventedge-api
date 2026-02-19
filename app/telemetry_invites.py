"""GET /api/v1/admin/telemetry/invites — invite code stats, redemptions, recent activity.

Each sub-block is independently fault-tolerant: if a table is missing or a query
fails, the sub-block returns ``available: false`` and the rest of the payload is
unaffected.  No migrations required.
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


# -- Sub-block builders ------------------------------------------------------

def _code_counts(cur: Any) -> dict[str, Any]:
    """Total codes, created 24h/7d, active/expired/disabled counts."""
    if not _table_exists(cur, "invite_codes"):
        return _unavailable()

    cur.execute("SELECT COUNT(*) FROM invite_codes")
    total = cur.fetchone()[0]

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

    cur.execute(
        "SELECT COUNT(*) FROM invite_codes WHERE is_enabled = TRUE"
    )
    active = cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(*) FROM invite_codes WHERE is_enabled = FALSE"
    )
    disabled = cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(*) FROM invite_codes "
        "WHERE expires_at IS NOT NULL AND expires_at < NOW()"
    )
    expired = cur.fetchone()[0]

    return {
        "available": True,
        "total_codes": total,
        "created_24h": created_24h,
        "created_7d": created_7d,
        "active": active,
        "disabled": disabled,
        "expired": expired,
    }


def _redemption_counts(cur: Any) -> dict[str, Any]:
    """Total redemptions, redeemed 24h/7d."""
    if not _table_exists(cur, "invite_redemptions"):
        return _unavailable()

    cur.execute("SELECT COUNT(*) FROM invite_redemptions")
    total = cur.fetchone()[0]

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
        "total_redeemed": total,
        "redeemed_24h": redeemed_24h,
        "redeemed_7d": redeemed_7d,
    }


def _by_tier(cur: Any) -> list[dict[str, Any]]:
    """Codes grouped by tier with usage counts."""
    if not _table_exists(cur, "invite_codes"):
        return []

    cur.execute(
        "SELECT COALESCE(tier, 'unknown'), COUNT(*), COALESCE(SUM(uses), 0) "
        "FROM invite_codes GROUP BY tier ORDER BY COUNT(*) DESC"
    )
    return [
        {"tier": row[0], "codes": row[1], "total_uses": int(row[2])}
        for row in cur.fetchall()
    ]


def _top_codes(cur: Any) -> list[dict[str, Any]]:
    """Top 20 most-redeemed invite codes."""
    if not _table_exists(cur, "invite_codes"):
        return []

    cur.execute(
        "SELECT code, tier, uses, max_uses, is_enabled, created_at, "
        "  created_by, expires_at, note "
        "FROM invite_codes "
        "ORDER BY uses DESC, created_at DESC "
        "LIMIT 20"
    )
    result = []
    for row in cur.fetchall():
        entry: dict[str, Any] = {
            "code": row[0],
            "tier": row[1],
            "uses": row[2],
            "max_uses": row[3],
            "is_enabled": row[4],
            "created_at": _iso(row[5]),
            "created_by": row[6],
        }
        if row[7] is not None:
            entry["expires_at"] = _iso(row[7])
        if row[8] is not None:
            entry["note"] = row[8]
        result.append(entry)

    return result


def _recent_redemptions(cur: Any) -> list[dict[str, Any]]:
    """20 most recent redemptions with optional code tier."""
    if not _table_exists(cur, "invite_redemptions"):
        return []

    has_codes = _table_exists(cur, "invite_codes")

    if has_codes:
        cur.execute(
            "SELECT ir.user_id, ir.code, ir.redeemed_at, ic.tier "
            "FROM invite_redemptions ir "
            "LEFT JOIN invite_codes ic ON ic.code = ir.code "
            "ORDER BY ir.redeemed_at DESC LIMIT 20"
        )
    else:
        cur.execute(
            "SELECT user_id, code, redeemed_at, NULL "
            "FROM invite_redemptions "
            "ORDER BY redeemed_at DESC LIMIT 20"
        )

    return [
        {
            "user_id": row[0],
            "code": row[1],
            "redeemed_at": _iso(row[2]),
            **({"tier": row[3]} if row[3] is not None else {}),
        }
        for row in cur.fetchall()
    ]


# -- Main builder -----------------------------------------------------------

_BLOCKS: list[tuple[str, str]] = [
    ("codes", "_code_counts"),
    ("redemptions", "_redemption_counts"),
    ("by_tier", "_by_tier"),
    ("top_codes", "_top_codes"),
    ("recent_redemptions", "_recent_redemptions"),
]

_BUILDERS = {
    "_code_counts": _code_counts,
    "_redemption_counts": _redemption_counts,
    "_by_tier": _by_tier,
    "_top_codes": _top_codes,
    "_recent_redemptions": _recent_redemptions,
}


def build_telemetry_invites() -> dict[str, Any]:
    """Build the invites telemetry payload. Each sub-block is fault-tolerant."""
    now = datetime.now(timezone.utc).isoformat()
    invites: dict[str, Any] = {}
    errors: list[str] = []

    with get_conn() as conn:
        with conn.cursor() as cur:
            # Code counts → flatten into invites
            try:
                codes = _code_counts(cur)
                if codes.get("available"):
                    invites["total_codes"] = codes["total_codes"]
                    invites["created_24h"] = codes["created_24h"]
                    invites["created_7d"] = codes["created_7d"]
                    invites["active"] = codes["active"]
                    invites["disabled"] = codes["disabled"]
                    invites["expired"] = codes["expired"]
                else:
                    invites["total_codes"] = None
                    invites["created_24h"] = None
                    invites["created_7d"] = None
                    invites["active"] = None
                    invites["disabled"] = None
                    invites["expired"] = None
                    errors.append(f"codes: {codes.get('reason', 'unavailable')}")
            except Exception as exc:
                invites["total_codes"] = None
                invites["created_24h"] = None
                invites["created_7d"] = None
                invites["active"] = None
                invites["disabled"] = None
                invites["expired"] = None
                errors.append(f"codes: {type(exc).__name__}")

            # Redemption counts → flatten
            try:
                reds = _redemption_counts(cur)
                if reds.get("available"):
                    invites["total_redeemed"] = reds["total_redeemed"]
                    invites["redeemed_24h"] = reds["redeemed_24h"]
                    invites["redeemed_7d"] = reds["redeemed_7d"]
                else:
                    invites["total_redeemed"] = None
                    invites["redeemed_24h"] = None
                    invites["redeemed_7d"] = None
                    errors.append(f"redemptions: {reds.get('reason', 'unavailable')}")
            except Exception as exc:
                invites["total_redeemed"] = None
                invites["redeemed_24h"] = None
                invites["redeemed_7d"] = None
                errors.append(f"redemptions: {type(exc).__name__}")

            # By tier
            try:
                invites["by_tier"] = _by_tier(cur)
            except Exception as exc:
                invites["by_tier"] = []
                errors.append(f"by_tier: {type(exc).__name__}")

            # Top codes
            try:
                invites["top_codes"] = _top_codes(cur)
            except Exception as exc:
                invites["top_codes"] = []
                errors.append(f"top_codes: {type(exc).__name__}")

            # Recent redemptions
            try:
                invites["recent_redemptions"] = _recent_redemptions(cur)
            except Exception as exc:
                invites["recent_redemptions"] = []
                errors.append(f"recent_redemptions: {type(exc).__name__}")

    result: dict[str, Any] = {
        "ok": True,
        "generated_at": now,
        "invites": invites,
    }
    if errors:
        result["_errors"] = errors
    return result
