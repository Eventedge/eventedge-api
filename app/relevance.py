"""AGENT-RELEVANCE-API-001: Read-only relevance endpoints.

Serves /home/eventedge/alerts/relevance_now.json as structured API responses.
No DB required — pure filesystem read.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ALERTS_DIR = Path(os.getenv("ROUTER_ALERT_DIR", "/home/eventedge/alerts"))
RELEVANCE_FILE = ALERTS_DIR / "relevance_now.json"

VALID_ASSETS = {"BTC", "ETH", "SOL", "HYPE"}
VALID_HORIZONS = {"4h", "12h", "24h"}


def _load_relevance() -> tuple[dict | None, str | None]:
    """Load relevance_now.json. Returns (data, error)."""
    if not RELEVANCE_FILE.exists():
        return None, "relevance_now.json not found"
    try:
        data = json.loads(RELEVANCE_FILE.read_text())
        return data, None
    except (json.JSONDecodeError, OSError) as e:
        return None, f"Failed to read relevance_now.json: {e}"


def _snapshot_age(data: dict) -> float | None:
    asof_ts = data.get("asof_ts")
    if not asof_ts:
        return None
    return round(datetime.now(timezone.utc).timestamp() - asof_ts, 1)


def build_relevance_now() -> dict[str, Any]:
    """Full relevance snapshot with metadata."""
    now = datetime.now(timezone.utc)
    data, err = _load_relevance()
    if err:
        return {"ok": False, "generated_at": now.isoformat(), "error": err}

    return {
        "ok": True,
        "generated_at": now.isoformat(),
        "snapshot_age_s": _snapshot_age(data),
        **data,
    }


def build_relevance_asset(asset: str, horizon: str | None = None) -> dict[str, Any]:
    """Slice relevance for a single asset, optionally filtered by horizon."""
    now = datetime.now(timezone.utc)
    data, err = _load_relevance()
    if err:
        return {"ok": False, "generated_at": now.isoformat(), "error": err}

    asset_upper = asset.upper()
    if asset_upper not in VALID_ASSETS:
        return {
            "ok": False,
            "generated_at": now.isoformat(),
            "error": f"Unknown asset: {asset}. Valid: {sorted(VALID_ASSETS)}",
        }

    asset_data = data.get("assets", {}).get(asset_upper)
    if not asset_data:
        return {
            "ok": False,
            "generated_at": now.isoformat(),
            "error": f"No data for asset {asset_upper}",
        }

    result = {
        "ok": True,
        "generated_at": now.isoformat(),
        "snapshot_age_s": _snapshot_age(data),
        "asset": asset_upper,
        "day": data.get("day"),
        "scoring_mode": data.get("scoring_mode"),
        "regime_bucket": asset_data.get("regime_bucket"),
        "regime_label": asset_data.get("regime_label"),
    }

    if horizon:
        h = horizon.lower()
        if h not in VALID_HORIZONS:
            return {
                "ok": False,
                "generated_at": now.isoformat(),
                "error": f"Unknown horizon: {horizon}. Valid: {sorted(VALID_HORIZONS)}",
            }
        h_data = asset_data.get("horizons", {}).get(h)
        if not h_data:
            result["horizons"] = {}
        else:
            result["horizons"] = {h: h_data}
    else:
        result["horizons"] = asset_data.get("horizons", {})

    return result


def build_relevance_explain(asset: str, horizon: str | None = None) -> dict[str, Any]:
    """Explain view: regime, scoring mode, top features with why/perf/open."""
    now = datetime.now(timezone.utc)
    data, err = _load_relevance()
    if err:
        return {"ok": False, "generated_at": now.isoformat(), "error": err}

    asset_upper = asset.upper()
    asset_data = data.get("assets", {}).get(asset_upper)
    if not asset_data:
        return {
            "ok": False,
            "generated_at": now.isoformat(),
            "error": f"No data for asset {asset_upper}",
        }

    h = (horizon or "24h").lower()
    h_data = asset_data.get("horizons", {}).get(h, {})
    top = h_data.get("top", [])
    meta = h_data.get("meta", {})

    features = []
    for item in top:
        entry = {
            "feature_id": item.get("feature_id"),
            "label": item.get("label", item.get("feature_id", "").split("@")[0]),
            "family": item.get("family", ""),
            "rank": item.get("rank"),
            "score": item.get("score"),
            "badge": item.get("badge"),
            "why": item.get("why"),
            "relevance_source": item.get("relevance_source"),
        }
        if item.get("perf"):
            entry["perf"] = item["perf"]
        if item.get("open"):
            entry["open"] = item["open"]
        features.append(entry)

    return {
        "ok": True,
        "generated_at": now.isoformat(),
        "snapshot_age_s": _snapshot_age(data),
        "asset": asset_upper,
        "horizon": h,
        "day": data.get("day"),
        "regime_bucket": asset_data.get("regime_bucket"),
        "regime_label": asset_data.get("regime_label"),
        "scoring_mode": meta.get("scoring_mode", data.get("scoring_mode")),
        "n_features": meta.get("n_features", 0),
        "n_scored": meta.get("n_scored", 0),
        "features": features,
    }
