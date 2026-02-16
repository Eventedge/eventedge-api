"""Fear & Greed Index — fetch from Alternative.me, cache in edge_dataset_registry."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, Optional, Tuple

import httpx
from psycopg2.extras import Json

from .db import get_conn
from .snapshots import get_snapshot

logger = logging.getLogger(__name__)

DATASET_KEY = "altme:fear_greed"
ALTME_URL = "https://api.alternative.me/fng/?limit=30&format=json"


def _iso(ts: Optional[dt.datetime]) -> Optional[str]:
    if not ts:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts.isoformat()


def _parse_altme_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Parse Alternative.me raw response into our API shape."""
    data = payload.get("data") or []
    current = data[0] if len(data) > 0 else {}
    cur_val = int(current.get("value", 50)) if str(current.get("value", "50")).isdigit() else 50
    cur_label = current.get("value_classification") or "Neutral"

    # Build small history (oldest->newest for chart)
    hist = []
    for i, row in enumerate(data[:7][::-1]):
        v_raw = row.get("value", "50")
        v = int(v_raw) if str(v_raw).isdigit() else 50
        t = row.get("timestamp")
        if t and str(t).isdigit():
            d = dt.datetime.fromtimestamp(int(t), tz=dt.timezone.utc).date()
            label = d.strftime("%b %d")
        else:
            label = f"D-{6 - i}"
        hist.append({"t": label, "v": v})

    return {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "current": {"value": cur_val, "label": cur_label},
        "history": hist,
        "source": {"provider": "alternative.me", "dataset_key": DATASET_KEY},
    }


def _upsert_dataset(payload: Dict[str, Any]) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO edge_dataset_registry (dataset_key, payload, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (dataset_key)
                DO UPDATE SET payload = EXCLUDED.payload, updated_at = now()
                """,
                (DATASET_KEY, Json(payload)),
            )
        conn.commit()


def get_fear_greed(max_age_seconds: int = 300) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return (parsed_payload, source_ts_iso).

    Prefer DB snapshot if updated within max_age_seconds.
    Otherwise fetch provider and upsert.
    """
    ds = get_snapshot(DATASET_KEY)
    if ds and ds.get("updated_at"):
        updated_at: dt.datetime = ds["updated_at"]
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=dt.timezone.utc)
        age = (dt.datetime.now(dt.timezone.utc) - updated_at).total_seconds()
        if age <= max_age_seconds:
            parsed = _parse_altme_payload(ds["payload"])
            return parsed, _iso(updated_at)

    # Fetch from provider
    try:
        with httpx.Client(timeout=6.0, headers={"Accept": "application/json"}) as client:
            r = client.get(ALTME_URL)
            r.raise_for_status()
            raw = r.json()
        _upsert_dataset(raw)
        parsed = _parse_altme_payload(raw)
        return parsed, _iso(dt.datetime.now(dt.timezone.utc))
    except Exception:
        logger.warning("Fear & Greed provider fetch failed", exc_info=True)
        # Fall back to stale DB data if available
        if ds:
            try:
                parsed = _parse_altme_payload(ds["payload"])
                return parsed, _iso(ds.get("updated_at"))
            except Exception:
                return None, None
        return None, None
