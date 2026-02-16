"""Alerts builder — detects regime + sentiment shifts by comparing current
state against the last-stored state in edge_dataset_registry.

Never 500s: hard-catches everything and returns ok=True items=[].
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, List, Optional

from psycopg2.extras import Json

from .db import get_conn
from .fear_greed import get_fear_greed
from .regime import build_regime

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _table_exists(cur, name: str) -> bool:  # type: ignore[type-arg]
    cur.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=%s LIMIT 1",
        (name,),
    )
    return cur.fetchone() is not None


def _get_registry(cur, key: str) -> Optional[Dict[str, Any]]:  # type: ignore[type-arg]
    if not _table_exists(cur, "edge_dataset_registry"):
        return None
    cur.execute("SELECT payload FROM edge_dataset_registry WHERE dataset_key=%s", (key,))
    row = cur.fetchone()
    return row[0] if row else None


def _set_registry(cur, key: str, payload: Dict[str, Any]) -> None:  # type: ignore[type-arg]
    cur.execute(
        """
        INSERT INTO edge_dataset_registry (dataset_key, payload, updated_at)
        VALUES (%s, %s, now())
        ON CONFLICT (dataset_key) DO UPDATE SET payload=EXCLUDED.payload, updated_at=EXCLUDED.updated_at
        """,
        (key, Json(payload)),
    )


def _mk_alert(
    *,
    ts: str,
    category: str,
    typ: str,
    asset: str,
    frm: str,
    to: str,
    score: Optional[int],
    confidence: str,
    badge: str,
    headline: str,
    message: str,
) -> Dict[str, Any]:
    return {
        "ts": ts,
        "category": category,
        "type": typ,
        "asset": asset,
        "from": frm,
        "to": to,
        "score": score,
        "confidence": confidence,
        "badge": badge,
        "headline": headline,
        "message": message,
        "cta": "/start",
    }


def _sentiment_bucket(value: Optional[int]) -> str:
    if value is None:
        return "NEUTRAL"
    if value <= 25:
        return "EXTREME_FEAR"
    if value <= 45:
        return "FEAR"
    if value <= 55:
        return "NEUTRAL"
    if value <= 75:
        return "GREED"
    return "EXTREME_GREED"


def build_alerts_live(limit: int = 50) -> Dict[str, Any]:
    now = _now_iso()
    limit = max(1, min(int(limit or 50), 200))

    assets = ["BTC", "ETH", "SOL"]
    items: List[Dict[str, Any]] = []

    try:
        # Pre-compute current values once (both are global, not per-asset)
        reg = build_regime()
        regime_label = (reg or {}).get("regime", {}).get("label")
        regime_conf = (reg or {}).get("regime", {}).get("confidence", "medium")

        fg_parsed, _ = get_fear_greed(max_age_seconds=300)
        fg_value: Optional[int] = None
        if fg_parsed:
            raw = fg_parsed.get("current", {}).get("value")
            fg_value = int(raw) if raw is not None else None
        fg_bucket = _sentiment_bucket(fg_value)

        with get_conn() as conn:
            with conn.cursor() as cur:
                # 1) REGIME CHANGE
                if regime_label:
                    for asset in assets:
                        key = f"edge:regime:last:{asset}"
                        prev = _get_registry(cur, key) or {}
                        prev_label = prev.get("label")

                        if prev_label and prev_label != regime_label:
                            badge = (
                                "\U0001f7e2"
                                if "ACCUM" in regime_label.upper() or "ON" in regime_label.upper()
                                else "\U0001f7e1"
                            )
                            items.append(
                                _mk_alert(
                                    ts=now,
                                    category="intelhub",
                                    typ="REGIME_CHANGE",
                                    asset=asset,
                                    frm=str(prev_label),
                                    to=str(regime_label),
                                    score=None,
                                    confidence=str(regime_conf),
                                    badge=badge,
                                    headline=f"{asset} REGIME CHANGE",
                                    message=f"{prev_label} \u2192 {regime_label}",
                                )
                            )

                        _set_registry(cur, key, {"label": regime_label, "ts": now})

                # 2) SENTIMENT SHIFT
                for asset in assets:
                    key = f"edge:sentiment:last:{asset}"
                    prev = _get_registry(cur, key) or {}
                    prev_bucket = prev.get("bucket")

                    if prev_bucket and prev_bucket != fg_bucket:
                        score_str = f" (Score: {fg_value})" if fg_value is not None else ""
                        items.append(
                            _mk_alert(
                                ts=now,
                                category="market",
                                typ="SENTIMENT_SHIFT",
                                asset=asset,
                                frm=str(prev_bucket),
                                to=str(fg_bucket),
                                score=fg_value,
                                confidence="high",
                                badge="\U0001f9ed",
                                headline=f"SENTIMENT SHIFT | {asset}",
                                message=f"{prev_bucket} \u2192 {fg_bucket}{score_str}",
                            )
                        )

                    _set_registry(cur, key, {"bucket": fg_bucket, "score": fg_value, "ts": now})

                conn.commit()

    except Exception:
        logger.warning("build_alerts_live failed", exc_info=True)
        items = []

    # If no change alerts, build a "state tape" so the website ticker always has content.
    if not items:
        try:
            reg = build_regime() or {}
            reg_label = (reg.get("regime") or {}).get("label") or "\u2014"
            reg_conf = (reg.get("regime") or {}).get("confidence") or "medium"

            fg_parsed, _ = get_fear_greed(max_age_seconds=300)
            fg_val: Optional[int] = None
            if fg_parsed:
                raw = fg_parsed.get("current", {}).get("value")
                fg_val = int(raw) if raw is not None else None
            fg_bkt = _sentiment_bucket(fg_val)

            # 2 regime items (BTC, ETH) + 3 sentiment items (BTC, ETH, SOL) = 5 tape items
            for a in ["BTC", "ETH"]:
                items.append(
                    _mk_alert(
                        ts=now,
                        category="intelhub",
                        typ="STATE",
                        asset=a,
                        frm="\u2014",
                        to=str(reg_label),
                        score=None,
                        confidence=str(reg_conf),
                        badge="\U0001f4ca",
                        headline=f"{a} REGIME",
                        message=f"{reg_label} ({str(reg_conf).upper()})",
                    )
                )
            for a in ["BTC", "ETH", "SOL"]:
                items.append(
                    _mk_alert(
                        ts=now,
                        category="market",
                        typ="STATE",
                        asset=a,
                        frm="\u2014",
                        to=str(fg_bkt),
                        score=fg_val,
                        confidence="high",
                        badge="\U0001f9ed",
                        headline=f"SENTIMENT | {a}",
                        message=f"{fg_bkt} (Score: {fg_val})" if fg_val is not None else str(fg_bkt),
                    )
                )
        except Exception:
            logger.warning("state tape generation failed", exc_info=True)
            items = []

    # newest first
    items = list(reversed(items))[:limit]

    return {"ok": True, "version": "v0.1-live", "source_ts": now, "items": items}
