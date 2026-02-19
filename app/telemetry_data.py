"""GET /api/v1/admin/telemetry/data — EdgeCore freshness + DB stats.

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


def _edgecore_snapshots(cur: Any) -> dict[str, Any]:
    """Snapshot freshness from api_snapshots or edge_dataset_registry."""
    # Prefer edge_dataset_registry (EdgeCore SSOT), fall back to api_snapshots
    table = None
    for t in ("edge_dataset_registry", "api_snapshots"):
        if _table_exists(cur, t):
            table = t
            break
    if table is None:
        return _unavailable()

    if table == "edge_dataset_registry":
        key_col = "dataset_key"
        ts_col = "updated_at"
    else:
        key_col = "data_type"
        ts_col = "created_at"

    cur.execute(
        f"SELECT {key_col}, {ts_col}, "
        f"  EXTRACT(EPOCH FROM NOW() - {ts_col}) AS age_s "
        f"FROM {table} ORDER BY {key_col}"
    )
    rows = cur.fetchall()
    total = len(rows)
    stale = 0
    dead = 0
    snapshots = []
    for key, updated_at, age_s in rows:
        age = float(age_s or 0)
        if age > 1200:
            status = "dead"
            dead += 1
        elif age > 600:
            status = "stale"
            stale += 1
        else:
            status = "fresh"
        snapshots.append({
            "key": key,
            "updated_at": _iso(updated_at),
            "age_s": round(age, 1),
            "status": status,
        })

    return {
        "available": True,
        "total_keys": total,
        "fresh": total - stale - dead,
        "stale": stale,
        "dead": dead,
        "snapshots": snapshots,
    }


def _db_stats(cur: Any) -> dict[str, Any]:
    """Database size + largest tables from pg_stat."""
    try:
        cur.execute("SELECT pg_database_size(current_database())")
        db_bytes = cur.fetchone()[0]
        db_mb = round(db_bytes / (1024 * 1024), 1) if db_bytes else 0

        cur.execute(
            "SELECT schemaname || '.' || relname, "
            "  pg_total_relation_size(relid), "
            "  n_live_tup, "
            "  last_vacuum, "
            "  last_analyze "
            "FROM pg_stat_user_tables "
            "ORDER BY pg_total_relation_size(relid) DESC "
            "LIMIT 15"
        )
        tables = []
        for name, size_bytes, rows, vacuum, analyze in cur.fetchall():
            tables.append({
                "table": name,
                "size_mb": round((size_bytes or 0) / (1024 * 1024), 1),
                "rows": int(rows or 0),
                "last_vacuum": _iso(vacuum),
                "last_analyze": _iso(analyze),
            })

        cur.execute(
            "SELECT COUNT(*) FROM pg_stat_user_tables"
        )
        table_count = cur.fetchone()[0]

        return {
            "available": True,
            "database_mb": db_mb,
            "table_count": table_count,
            "largest_tables": tables,
        }
    except Exception:
        return _unavailable("pg_stat query failed")


def build_telemetry_data() -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    data: dict[str, Any] = {}
    errors: list[str] = []

    with get_conn() as conn:
        with conn.cursor() as cur:
            try:
                ec = _edgecore_snapshots(cur)
                if ec.get("available"):
                    data["edgecore"] = {
                        "total_keys": ec["total_keys"],
                        "fresh": ec["fresh"],
                        "stale": ec["stale"],
                        "dead": ec["dead"],
                        "snapshots": ec["snapshots"],
                    }
                else:
                    data["edgecore"] = None
                    errors.append(f"edgecore: {ec.get('reason')}")
            except Exception as exc:
                conn.rollback()
                data["edgecore"] = None
                errors.append(f"edgecore: {type(exc).__name__}")

            try:
                db = _db_stats(cur)
                if db.get("available"):
                    data["database"] = {
                        "database_mb": db["database_mb"],
                        "table_count": db["table_count"],
                        "largest_tables": db["largest_tables"],
                    }
                else:
                    data["database"] = None
                    errors.append(f"database: {db.get('reason')}")
            except Exception as exc:
                conn.rollback()
                data["database"] = None
                errors.append(f"database: {type(exc).__name__}")

    result: dict[str, Any] = {"ok": True, "generated_at": now, "data": data}
    if errors:
        result["_errors"] = errors
    return result
