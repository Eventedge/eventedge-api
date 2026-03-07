"""GET /api/v1/admin/telemetry/ta_relevance — TA relevance rankings.

Returns top-ranked TA artifacts by relevance score for a given day/horizon.
Each sub-block is independently fault-tolerant.  No migrations required.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from .db import get_conn


def _table_exists(cur: Any, name: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = %s",
        (name,),
    )
    return cur.fetchone() is not None


def _iso(val: Any) -> str | None:
    if val is None:
        return None
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val)


RANKING_QUERY = """
SELECT day, asset, horizon, artifact_id, score, rank, n_obs, join_rate, hit_rate
FROM ta_scanner_relevance_daily
WHERE day = %s
  AND (%s IS NULL OR horizon = %s)
ORDER BY asset, horizon, rank
LIMIT %s
"""

SUMMARY_QUERY = """
SELECT day, asset, horizon, COUNT(*) AS rows,
       MIN(score) AS min_score, MAX(score) AS max_score,
       AVG(score) AS avg_score
FROM ta_scanner_relevance_daily
WHERE day = %s
GROUP BY day, asset, horizon
ORDER BY asset, horizon
"""

AVAILABLE_DAYS_QUERY = """
SELECT DISTINCT day FROM ta_scanner_relevance_daily
ORDER BY day DESC LIMIT 7
"""


def build_telemetry_ta_relevance(
    day_str: str | None = None,
    horizon: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()

    with get_conn() as conn:
        with conn.cursor() as cur:
            if not _table_exists(cur, "ta_scanner_relevance_daily"):
                return {
                    "ok": False,
                    "generated_at": now,
                    "error": "ta_scanner_relevance_daily table not found",
                }

            # Resolve day
            if day_str:
                try:
                    target_day = date.fromisoformat(day_str)
                except ValueError:
                    return {"ok": False, "generated_at": now, "error": f"Invalid day: {day_str}"}
            else:
                target_day = date.today() - timedelta(days=1)

            # Rankings
            cur.execute(RANKING_QUERY, (target_day, horizon, horizon, limit))
            ranking_rows = cur.fetchall()

            rankings = []
            for row in ranking_rows:
                rankings.append({
                    "day": str(row[0]),
                    "asset": row[1],
                    "horizon": row[2],
                    "artifact_id": row[3],
                    "score": round(float(row[4]), 6) if row[4] is not None else 0,
                    "rank": row[5],
                    "n_obs": row[6],
                    "join_rate": round(float(row[7]), 4) if row[7] is not None else None,
                    "hit_rate": round(float(row[8]), 4) if row[8] is not None else None,
                })

            # Summary stats
            cur.execute(SUMMARY_QUERY, (target_day,))
            summary_rows = cur.fetchall()

            summary = []
            for row in summary_rows:
                summary.append({
                    "day": str(row[0]),
                    "asset": row[1],
                    "horizon": row[2],
                    "rows": row[3],
                    "min_score": round(float(row[4]), 6) if row[4] is not None else None,
                    "max_score": round(float(row[5]), 6) if row[5] is not None else None,
                    "avg_score": round(float(row[6]), 6) if row[6] is not None else None,
                })

            # Available days
            cur.execute(AVAILABLE_DAYS_QUERY)
            available_days = [str(r[0]) for r in cur.fetchall()]

    return {
        "ok": True,
        "generated_at": now,
        "query": {
            "day": str(target_day),
            "horizon": horizon,
            "limit": limit,
        },
        "rankings": rankings,
        "summary": summary,
        "available_days": available_days,
    }
