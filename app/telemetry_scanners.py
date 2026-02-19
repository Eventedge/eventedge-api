"""GET /api/v1/admin/telemetry/scanners — scanner run status.

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


def _scanner_runs(cur: Any) -> dict[str, Any]:
    if not _table_exists(cur, "scanner_run_meta"):
        return _unavailable()

    cur.execute(
        "SELECT DISTINCT ON (scanner_id) "
        "  scanner_id, run_at, duration_s, result_count, status "
        "FROM scanner_run_meta "
        "ORDER BY scanner_id, run_at DESC"
    )
    scanners = []
    for sid, run_at, duration, results, status in cur.fetchall():
        age_s = (datetime.now(timezone.utc) - run_at).total_seconds() if run_at else None
        scanners.append({
            "scanner_id": sid,
            "last_run_at": _iso(run_at),
            "duration_s": round(float(duration or 0), 2),
            "result_count": int(results or 0),
            "status": status or "unknown",
            "age_s": round(age_s, 1) if age_s is not None else None,
        })

    return {"available": True, "scanners": scanners}


def _scanner_cache(cur: Any) -> list[dict[str, Any]]:
    if not _table_exists(cur, "scanner_cache"):
        return []

    cur.execute(
        "SELECT cache_key, updated_at, "
        "  EXTRACT(EPOCH FROM NOW() - updated_at) AS age_s "
        "FROM scanner_cache ORDER BY cache_key"
    )
    return [
        {
            "cache_key": row[0],
            "updated_at": _iso(row[1]),
            "age_s": round(float(row[2] or 0), 1),
        }
        for row in cur.fetchall()
    ]


def build_telemetry_scanners() -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    result_scanners: dict[str, Any] = {}
    errors: list[str] = []

    with get_conn() as conn:
        with conn.cursor() as cur:
            try:
                runs = _scanner_runs(cur)
                if runs.get("available"):
                    result_scanners["scanners"] = runs["scanners"]
                else:
                    result_scanners["scanners"] = []
                    errors.append(f"scanners: {runs.get('reason')}")
            except Exception as exc:
                result_scanners["scanners"] = []
                errors.append(f"scanners: {type(exc).__name__}")

            try:
                result_scanners["cache"] = _scanner_cache(cur)
            except Exception as exc:
                result_scanners["cache"] = []
                errors.append(f"cache: {type(exc).__name__}")

    result: dict[str, Any] = {
        "ok": True,
        "generated_at": now,
        "scanners": result_scanners,
    }
    if errors:
        result["_errors"] = errors
    return result
