"""GET /api/v1/admin/telemetry/ta_health — TA artifact parity + rollup freshness.

Each sub-block is independently fault-tolerant.  No migrations required.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from .db import get_conn

# ---------------------------------------------------------------------------
# Constants (must match scripts/ta_ops_health.py)
# ---------------------------------------------------------------------------

ASSETS = ["BTC", "ETH", "SOL", "HYPE"]

EXPECTED_ARTIFACT_IDS = [
    "feature.ta.rsi_state@1.0",
    "feature.ta.trend_strength@1.0",
    "feature.ta.squeeze_state@1.0",
    "feature.ta.supertrend@1.0",
    "feature.ta.breadth@1.0",
    "feature.ta.rsi_div@1.0",
    "feature.ta.trend_conf@1.0",
    "feature.ta.ema200@1.0",
    "feature.ta.funding@1.0",
    "feature.ta.oi_flow@1.0",
    "feature.ta.ttm_squeeze@1.0",
    "feature.ta.bos@1.0",
    "feature.ta.rvol_break@1.0",
    "feature.ta.sweep@1.0",
    "feature.ta.basis@1.0",
]
EXPECTED_SET = set(EXPECTED_ARTIFACT_IDS)
EXPECTED_COUNT = len(EXPECTED_SET)  # 15

STALE_THRESHOLD_S = 8100  # 2h 15m


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


# ---------------------------------------------------------------------------
# Sub-blocks
# ---------------------------------------------------------------------------

ARTIFACT_QUERY = """
SELECT asset, artifact_id, MAX(ts) AS last_seen
FROM ts_artifact_observations
WHERE artifact_id LIKE 'feature.ta.%%@1.0'
  AND ts > NOW() - INTERVAL '3 hours'
GROUP BY asset, artifact_id
"""


def _artifact_parity(cur: Any) -> dict[str, Any]:
    if not _table_exists(cur, "ts_artifact_observations"):
        return _unavailable()

    cur.execute(ARTIFACT_QUERY)
    rows = cur.fetchall()
    now = datetime.now(timezone.utc)

    by_asset: dict[str, dict[str, datetime]] = {a: {} for a in ASSETS}
    for asset, artifact_id, last_seen in rows:
        if asset in by_asset:
            by_asset[asset][artifact_id] = last_seen

    per_asset = []
    assets_ok = 0
    for asset in ASSETS:
        seen = by_asset[asset]
        seen_set = set(seen.keys())
        missing = sorted(EXPECTED_SET - seen_set)
        stale_count = 0
        last_seen_max = None

        for aid, ts in seen.items():
            if ts is not None:
                age = (now - ts).total_seconds()
                if age > STALE_THRESHOLD_S:
                    stale_count += 1
                if last_seen_max is None or ts > last_seen_max:
                    last_seen_max = ts

        ok = len(missing) == 0 and stale_count == 0
        if ok:
            assets_ok += 1

        per_asset.append({
            "asset": asset,
            "distinct": len(seen_set),
            "expected": EXPECTED_COUNT,
            "ok": ok,
            "stale_count": stale_count,
            "missing_ids": missing,
            "last_seen_max": _iso(last_seen_max),
        })

    return {
        "per_asset": per_asset,
        "overall": {
            "expected_per_asset": EXPECTED_COUNT,
            "assets_ok": assets_ok,
            "assets_total": len(ASSETS),
        },
    }


ROLLUP_QUERY = """
SELECT MAX(day), MAX(updated_at), COUNT(*)
FROM ta_scanner_rollups_daily
WHERE day = (SELECT MAX(day) FROM ta_scanner_rollups_daily)
"""

ROLLUP_HORIZONS_QUERY = """
SELECT DISTINCT horizon
FROM ta_scanner_rollups_daily
WHERE day = (SELECT MAX(day) FROM ta_scanner_rollups_daily)
ORDER BY horizon
"""


def _rollup_freshness(cur: Any) -> dict[str, Any]:
    if not _table_exists(cur, "ta_scanner_rollups_daily"):
        return _unavailable()

    cur.execute(ROLLUP_QUERY)
    row = cur.fetchone()
    if row is None or row[0] is None:
        return {"latest_day": None, "fresh": False, "rows_latest_day": 0, "horizons_present": []}

    latest_day, updated_at, row_count = row
    yesterday = date.today() - timedelta(days=1)
    fresh = latest_day >= yesterday

    cur.execute(ROLLUP_HORIZONS_QUERY)
    horizons = [r[0] for r in cur.fetchall()]

    return {
        "latest_day": str(latest_day),
        "fresh": fresh,
        "rows_latest_day": row_count,
        "horizons_present": horizons,
    }


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------

def build_telemetry_ta_health() -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    errors: list[str] = []

    artifacts: dict[str, Any] = {}
    rollups: dict[str, Any] = {}

    with get_conn() as conn:
        with conn.cursor() as cur:
            try:
                artifacts = _artifact_parity(cur)
            except Exception as exc:
                conn.rollback()
                artifacts = _unavailable(f"query error: {type(exc).__name__}")
                errors.append(f"artifacts: {type(exc).__name__}")

            try:
                rollups = _rollup_freshness(cur)
            except Exception as exc:
                conn.rollback()
                rollups = _unavailable(f"query error: {type(exc).__name__}")
                errors.append(f"rollups: {type(exc).__name__}")

    result: dict[str, Any] = {
        "ok": True,
        "generated_at": now,
        "artifacts": artifacts,
        "rollups": rollups,
    }
    if errors:
        result["_errors"] = errors
    return result
