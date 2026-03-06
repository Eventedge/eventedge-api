"""AGENT-RELEVANCE-API-001 + AGENT-CACHE-001: Read-only relevance endpoints.

Serves /home/eventedge/alerts/relevance_now.json as structured API responses.
No DB required — pure filesystem read.

Cache strategy: ETag derived from file mtime+size, Last-Modified from mtime.
Supports If-None-Match and If-Modified-Since conditional requests (304).
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response

ALERTS_DIR = Path(os.getenv("ROUTER_ALERT_DIR", "/home/eventedge/alerts"))
RELEVANCE_FILE = ALERTS_DIR / "relevance_now.json"

VALID_ASSETS = {"BTC", "ETH", "SOL", "HYPE"}
VALID_HORIZONS = {"4h", "12h", "24h"}

CACHE_CONTROL = "public, max-age=30, stale-while-revalidate=60"


# ---------------------------------------------------------------------------
# File-based cache helpers (reusable for any JSON-file-backed endpoint)
# ---------------------------------------------------------------------------

def _file_etag(path: Path) -> str | None:
    """Compute a weak ETag from file mtime + size. Returns None if file missing."""
    try:
        st = path.stat()
        raw = f"{st.st_mtime_ns}:{st.st_size}".encode()
        return f'W/"{hashlib.md5(raw).hexdigest()}"'
    except OSError:
        return None


def _file_mtime_dt(path: Path) -> datetime | None:
    """Get file mtime as UTC datetime. Returns None if file missing."""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _check_conditional(request: Request, etag: str | None, mtime: datetime | None) -> Response | None:
    """Check If-None-Match / If-Modified-Since. Returns 304 Response or None."""
    if etag:
        client_etag = request.headers.get("if-none-match")
        if client_etag and client_etag == etag:
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": CACHE_CONTROL})

    if mtime:
        ims = request.headers.get("if-modified-since")
        if ims:
            try:
                ims_dt = parsedate_to_datetime(ims)
                if mtime <= ims_dt:
                    headers = {"Cache-Control": CACHE_CONTROL}
                    if etag:
                        headers["ETag"] = etag
                    return Response(status_code=304, headers=headers)
            except (ValueError, TypeError):
                pass
    return None


def _cache_headers(etag: str | None, mtime: datetime | None) -> dict[str, str]:
    """Build cache response headers."""
    headers: dict[str, str] = {"Cache-Control": CACHE_CONTROL}
    if etag:
        headers["ETag"] = etag
    if mtime:
        headers["Last-Modified"] = format_datetime(mtime, usegmt=True)
    return headers


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

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


def _file_meta() -> dict[str, Any]:
    """File-level metadata for response body."""
    mtime = _file_mtime_dt(RELEVANCE_FILE)
    etag = _file_etag(RELEVANCE_FILE)
    return {
        "source_file_mtime": mtime.isoformat() if mtime else None,
        "etag": etag,
    }


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def build_relevance_now(request: Request) -> JSONResponse:
    """Full relevance snapshot with cache headers."""
    now = datetime.now(timezone.utc)
    etag = _file_etag(RELEVANCE_FILE)
    mtime = _file_mtime_dt(RELEVANCE_FILE)

    cached = _check_conditional(request, etag, mtime)
    if cached:
        return cached

    data, err = _load_relevance()
    if err:
        return JSONResponse(
            content={"ok": False, "generated_at": now.isoformat(), "error": err},
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    payload = {
        "ok": True,
        "generated_at": now.isoformat(),
        "snapshot_age_s": _snapshot_age(data),
        **_file_meta(),
        **data,
    }
    return JSONResponse(content=payload, headers=_cache_headers(etag, mtime))


def build_relevance_asset(request: Request, asset: str, horizon: str | None = None) -> JSONResponse:
    """Slice relevance for a single asset, optionally filtered by horizon."""
    now = datetime.now(timezone.utc)
    etag = _file_etag(RELEVANCE_FILE)
    mtime = _file_mtime_dt(RELEVANCE_FILE)

    cached = _check_conditional(request, etag, mtime)
    if cached:
        return cached

    data, err = _load_relevance()
    if err:
        return JSONResponse(
            content={"ok": False, "generated_at": now.isoformat(), "error": err},
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    asset_upper = asset.upper()
    if asset_upper not in VALID_ASSETS:
        return JSONResponse(
            content={"ok": False, "generated_at": now.isoformat(),
                     "error": f"Unknown asset: {asset}. Valid: {sorted(VALID_ASSETS)}"},
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    asset_data = data.get("assets", {}).get(asset_upper)
    if not asset_data:
        return JSONResponse(
            content={"ok": False, "generated_at": now.isoformat(),
                     "error": f"No data for asset {asset_upper}"},
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    result: dict[str, Any] = {
        "ok": True,
        "generated_at": now.isoformat(),
        "snapshot_age_s": _snapshot_age(data),
        **_file_meta(),
        "asset": asset_upper,
        "day": data.get("day"),
        "scoring_mode": data.get("scoring_mode"),
        "regime_bucket": asset_data.get("regime_bucket"),
        "regime_label": asset_data.get("regime_label"),
    }

    if horizon:
        h = horizon.lower()
        if h not in VALID_HORIZONS:
            return JSONResponse(
                content={"ok": False, "generated_at": now.isoformat(),
                         "error": f"Unknown horizon: {horizon}. Valid: {sorted(VALID_HORIZONS)}"},
                status_code=200,
                headers={"Cache-Control": "no-store"},
            )
        h_data = asset_data.get("horizons", {}).get(h)
        result["horizons"] = {h: h_data} if h_data else {}
    else:
        result["horizons"] = asset_data.get("horizons", {})

    return JSONResponse(content=result, headers=_cache_headers(etag, mtime))


def build_relevance_explain(request: Request, asset: str, horizon: str | None = None) -> JSONResponse:
    """Explain view: regime, scoring mode, top features with why/perf/open."""
    now = datetime.now(timezone.utc)
    etag = _file_etag(RELEVANCE_FILE)
    mtime = _file_mtime_dt(RELEVANCE_FILE)

    cached = _check_conditional(request, etag, mtime)
    if cached:
        return cached

    data, err = _load_relevance()
    if err:
        return JSONResponse(
            content={"ok": False, "generated_at": now.isoformat(), "error": err},
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    asset_upper = asset.upper()
    asset_data = data.get("assets", {}).get(asset_upper)
    if not asset_data:
        return JSONResponse(
            content={"ok": False, "generated_at": now.isoformat(),
                     "error": f"No data for asset {asset_upper}"},
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    h = (horizon or "24h").lower()
    h_data = asset_data.get("horizons", {}).get(h, {})
    top = h_data.get("top", [])
    meta = h_data.get("meta", {})

    features = []
    for item in top:
        entry: dict[str, Any] = {
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

    result = {
        "ok": True,
        "generated_at": now.isoformat(),
        "snapshot_age_s": _snapshot_age(data),
        **_file_meta(),
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
    return JSONResponse(content=result, headers=_cache_headers(etag, mtime))
