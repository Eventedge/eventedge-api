"""AGENT-RELEVANCE-API-001/002 + AGENT-CACHE-001: Read-only relevance endpoints.

Serves /home/eventedge/alerts/relevance_now.json as structured API responses.
No DB required — pure filesystem read.

Cache strategy: ETag derived from file mtime+size, Last-Modified from mtime.
Supports If-None-Match and If-Modified-Since conditional requests (304).

API-002 additions: dated snapshots (?day=), filtered slices (?family=, ?top_k=),
agent presets, richer explain metadata (family distribution, stability, caps).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response

ALERTS_DIR = Path(os.getenv("ROUTER_ALERT_DIR", "/home/eventedge/alerts"))
RELEVANCE_FILE = ALERTS_DIR / "relevance_now.json"
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

VALID_ASSETS = {"BTC", "ETH", "SOL", "HYPE"}
VALID_HORIZONS = {"4h", "12h", "24h"}
VALID_FAMILIES = {"ta", "deriv", "derivs", "macro", "pm", "quality", "alerts", "chartfeed", "system"}

CACHE_CONTROL = "public, max-age=30, stale-while-revalidate=60"

# ---------------------------------------------------------------------------
# Agent presets (static, no DB)
# ---------------------------------------------------------------------------

PRESETS: list[dict[str, Any]] = [
    {
        "id": "default",
        "label": "Default",
        "description": "Top mixed signals across all families",
        "families": None,
        "family_weights": {},
        "preferred_groups": [],
        "quality_intent": "balanced",
    },
    {
        "id": "momentum",
        "label": "Momentum",
        "description": "Trend and directional signals from TA and derivatives",
        "families": ["ta", "derivs"],
        "family_weights": {"ta": 1.3, "derivs": 1.2},
        "preferred_groups": ["scanner_dir", "confluence"],
        "quality_intent": "tolerant",
    },
    {
        "id": "mean_reversion",
        "label": "Mean Reversion",
        "description": "Contrarian and crowding signals for mean-reversion setups",
        "families": ["derivs", "ta", "pm"],
        "family_weights": {"derivs": 1.3, "ta": 1.1, "pm": 1.1},
        "preferred_groups": ["funding_dir", "liq_bias", "oi_accel"],
        "quality_intent": "strict",
    },
    {
        "id": "macro",
        "label": "Macro",
        "description": "Macro regime and prediction market signals",
        "families": ["macro", "pm"],
        "family_weights": {"macro": 1.3, "pm": 1.2},
        "preferred_groups": [],
        "quality_intent": "strict",
    },
    {
        "id": "defensive",
        "label": "Defensive",
        "description": "Quality, alerts, and macro-focused risk view",
        "families": ["quality", "alerts", "macro"],
        "family_weights": {"quality": 1.3, "macro": 1.2, "alerts": 1.1},
        "preferred_groups": [],
        "quality_intent": "strict",
    },
    {
        "id": "derivatives",
        "label": "Derivatives",
        "description": "Funding, OI, liquidation, and leverage signals",
        "families": ["deriv", "derivs"],
        "family_weights": {"deriv": 1.2, "derivs": 1.3},
        "preferred_groups": ["funding_dir", "oi_accel", "liq_bias"],
        "quality_intent": "tolerant",
    },
]

_PRESET_MAP: dict[str, dict[str, Any]] = {p["id"]: p for p in PRESETS}

# Quality intent modifiers: how strict a preset is about maturity/uncertainty
_QUALITY_INTENT_MATURITY_PENALTY: dict[str, float] = {
    "balanced": 0.0,
    "tolerant": 0.0,    # accepts moderate uncertainty
    "strict": 0.05,     # penalizes weak maturity
}
_QUALITY_INTENT_UNCERTAINTY_PENALTY: dict[str, float] = {
    "balanced": 0.0,
    "tolerant": 0.0,
    "strict": 0.05,     # penalizes wide CI
}


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

def _resolve_file(day: str | None = None) -> Path:
    """Return the relevance file for a given day, or current if None."""
    if day and _DAY_RE.match(day):
        dated = ALERTS_DIR / f"relevance_now.{day}.json"
        if dated.exists():
            return dated
    return RELEVANCE_FILE


def _load_relevance(day: str | None = None) -> tuple[dict | None, str | None]:
    """Load relevance_now.json (or dated variant). Returns (data, error)."""
    path = _resolve_file(day)
    if not path.exists():
        name = path.name
        return None, f"{name} not found"
    try:
        data = json.loads(path.read_text())
        return data, None
    except (json.JSONDecodeError, OSError) as e:
        return None, f"Failed to read {path.name}: {e}"


def _snapshot_age(data: dict) -> float | None:
    asof_ts = data.get("asof_ts")
    if not asof_ts:
        return None
    return round(datetime.now(timezone.utc).timestamp() - asof_ts, 1)


def _file_meta(path: Path | None = None) -> dict[str, Any]:
    """File-level metadata for response body."""
    p = path or RELEVANCE_FILE
    mtime = _file_mtime_dt(p)
    etag = _file_etag(p)
    return {
        "source_file": p.name,
        "source_file_mtime": mtime.isoformat() if mtime else None,
        "etag": etag,
    }


def _error_response(now: datetime, err: str) -> JSONResponse:
    return JSONResponse(
        content={"ok": False, "generated_at": now.isoformat(), "error": err},
        status_code=200,
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# Feature filtering helpers
# ---------------------------------------------------------------------------

def _filter_features(
    features: list[dict[str, Any]],
    family: str | None = None,
    top_k: int | None = None,
) -> list[dict[str, Any]]:
    """Filter feature list by family and/or top_k."""
    result = features
    if family:
        fam_lower = family.lower()
        result = [f for f in result if (f.get("family") or "").lower() == fam_lower]
    if top_k and top_k > 0:
        result = result[:top_k]
    return result


def _family_distribution(features: list[dict[str, Any]]) -> dict[str, int]:
    """Count features per family."""
    return dict(Counter(f.get("family", "unknown") for f in features).most_common())


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def build_relevance_now(request: Request, day: str | None = None) -> JSONResponse:
    """Full relevance snapshot with cache headers. Supports ?day=YYYY-MM-DD."""
    now = datetime.now(timezone.utc)
    rfile = _resolve_file(day)
    etag = _file_etag(rfile)
    mtime = _file_mtime_dt(rfile)

    cached = _check_conditional(request, etag, mtime)
    if cached:
        return cached

    data, err = _load_relevance(day)
    if err:
        return _error_response(now, err)

    payload = {
        "ok": True,
        "generated_at": now.isoformat(),
        "snapshot_age_s": _snapshot_age(data),
        **_file_meta(rfile),
        **data,
    }
    return JSONResponse(content=payload, headers=_cache_headers(etag, mtime))


def build_relevance_asset(
    request: Request,
    asset: str,
    horizon: str | None = None,
    day: str | None = None,
) -> JSONResponse:
    """Slice relevance for a single asset, optionally filtered by horizon."""
    now = datetime.now(timezone.utc)
    rfile = _resolve_file(day)
    etag = _file_etag(rfile)
    mtime = _file_mtime_dt(rfile)

    cached = _check_conditional(request, etag, mtime)
    if cached:
        return cached

    data, err = _load_relevance(day)
    if err:
        return _error_response(now, err)

    asset_upper = asset.upper()
    if asset_upper not in VALID_ASSETS:
        return _error_response(now, f"Unknown asset: {asset}. Valid: {sorted(VALID_ASSETS)}")

    asset_data = data.get("assets", {}).get(asset_upper)
    if not asset_data:
        return _error_response(now, f"No data for asset {asset_upper}")

    result: dict[str, Any] = {
        "ok": True,
        "generated_at": now.isoformat(),
        "snapshot_age_s": _snapshot_age(data),
        **_file_meta(rfile),
        "asset": asset_upper,
        "day": data.get("day"),
        "scoring_mode": data.get("scoring_mode"),
        "regime_bucket": asset_data.get("regime_bucket"),
        "regime_label": asset_data.get("regime_label"),
    }

    if horizon:
        h = horizon.lower()
        if h not in VALID_HORIZONS:
            return _error_response(now, f"Unknown horizon: {horizon}. Valid: {sorted(VALID_HORIZONS)}")
        h_data = asset_data.get("horizons", {}).get(h)
        result["horizons"] = {h: h_data} if h_data else {}
    else:
        result["horizons"] = asset_data.get("horizons", {})

    return JSONResponse(content=result, headers=_cache_headers(etag, mtime))


def build_relevance_explain(
    request: Request,
    asset: str,
    horizon: str | None = None,
    day: str | None = None,
    family: str | None = None,
    top_k: int | None = None,
) -> JSONResponse:
    """Explain view: regime, scoring mode, top features with why/perf/open.

    Supports ?day=, ?family=, ?top_k= filters applied after loading.
    """
    now = datetime.now(timezone.utc)
    rfile = _resolve_file(day)
    etag = _file_etag(rfile)
    mtime = _file_mtime_dt(rfile)

    cached = _check_conditional(request, etag, mtime)
    if cached:
        return cached

    data, err = _load_relevance(day)
    if err:
        return _error_response(now, err)

    asset_upper = asset.upper()
    asset_data = data.get("assets", {}).get(asset_upper)
    if not asset_data:
        return _error_response(now, f"No data for asset {asset_upper}")

    h = (horizon or "24h").lower()
    h_data = asset_data.get("horizons", {}).get(h, {})
    top = h_data.get("top", [])
    meta = h_data.get("meta", {})

    # Build feature entries
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
        if item.get("_kept_due_to_stability"):
            entry["_kept_due_to_stability"] = True
        if item.get("_skipped_due_to_diversity_cap"):
            entry["_skipped_due_to_diversity_cap"] = True
        features.append(entry)

    # Apply filters
    if family and family.lower() not in VALID_FAMILIES:
        return _error_response(now, f"Unknown family: {family}. Valid: {sorted(VALID_FAMILIES)}")
    filtered = _filter_features(features, family=family, top_k=top_k)

    stability = meta.get("stability")
    diversity_caps = meta.get("diversity_caps")

    result: dict[str, Any] = {
        "ok": True,
        "generated_at": now.isoformat(),
        "snapshot_age_s": _snapshot_age(data),
        **_file_meta(rfile),
        "asset": asset_upper,
        "horizon": h,
        "day": data.get("day"),
        "regime_bucket": asset_data.get("regime_bucket"),
        "regime_label": asset_data.get("regime_label"),
        "scoring_mode": meta.get("scoring_mode", data.get("scoring_mode")),
        "n_features": meta.get("n_features", 0),
        "n_scored": meta.get("n_scored", 0),
        "features": filtered,
        "family_distribution": _family_distribution(features),
    }
    if family:
        result["filter_family"] = family.lower()
    if top_k:
        result["filter_top_k"] = top_k
    if stability:
        result["stability"] = stability
    if diversity_caps:
        result["diversity_caps"] = diversity_caps
        result["skipped_due_to_caps"] = meta.get("skipped_due_to_caps", 0)
    return JSONResponse(content=result, headers=_cache_headers(etag, mtime))


def build_relevance_presets() -> JSONResponse:
    """Return static agent presets list."""
    return JSONResponse(
        content={
            "ok": True,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "presets": PRESETS,
        },
        headers={"Cache-Control": "public, max-age=3600"},
    )


def _apply_preset_scoring(
    features: list[dict[str, Any]],
    preset: dict[str, Any],
) -> list[dict[str, Any]]:
    """Apply preset-aware re-ranking: family weights, preferred groups, quality intent."""
    family_weights = preset.get("family_weights", {})
    preferred_groups = set(preset.get("preferred_groups", []))
    quality_intent = preset.get("quality_intent", "balanced")

    mat_penalty = _QUALITY_INTENT_MATURITY_PENALTY.get(quality_intent, 0.0)
    unc_penalty = _QUALITY_INTENT_UNCERTAINTY_PENALTY.get(quality_intent, 0.0)

    for entry in features:
        base_score = entry.get("score", 0) or 0
        entry["base_score"] = base_score
        entry["base_rank"] = entry.get("rank", 0)

        fam = (entry.get("family") or "").lower()
        fid = entry.get("feature_id", "")

        # Family weight multiplier
        multiplier = family_weights.get(fam, 1.0)

        # Preferred group bonus
        group_bonus = 0.0
        fid_parts = fid.split("@")[0].split(".") if fid else []
        short_id = ".".join(fid_parts[1:]) if len(fid_parts) > 1 else fid_parts[0] if fid_parts else ""
        if preferred_groups and short_id in preferred_groups:
            group_bonus = 0.10

        # Quality intent adjustments
        quality_adj = 0.0
        perf = entry.get("perf", {})
        reasons = []

        if multiplier != 1.0:
            reasons.append(f"family fit ×{multiplier:.1f}")

        if group_bonus > 0:
            reasons.append("preferred group +0.10")

        if mat_penalty > 0 and perf:
            stage = perf.get("maturity_stage", "")
            if stage in ("seed", "early"):
                quality_adj -= mat_penalty
                reasons.append(f"weak maturity -{mat_penalty:.2f}")

        if unc_penalty > 0 and perf:
            ci = perf.get("ci_width")
            if ci is not None and ci > 0.03:
                quality_adj -= unc_penalty
                reasons.append(f"high uncertainty -{unc_penalty:.2f}")

        preset_score = round(base_score * multiplier + group_bonus + quality_adj, 4)
        entry["preset_score"] = preset_score
        entry["preset_multiplier"] = multiplier
        entry["preset_group_bonus"] = group_bonus
        entry["preset_reason"] = "; ".join(reasons) if reasons else "base score"
        if quality_adj != 0:
            entry["preset_quality_note"] = f"quality intent={quality_intent}, adj={quality_adj:+.2f}"

    # Re-sort by preset_score
    features.sort(key=lambda x: -(x.get("preset_score", 0)))
    for i, entry in enumerate(features):
        entry["rank"] = i + 1

    return features


def build_relevance_preset_view(
    request: Request,
    asset: str,
    preset_id: str,
    horizon: str | None = None,
    day: str | None = None,
) -> JSONResponse:
    """Apply preset-aware scoring and re-ranking server-side."""
    preset = _PRESET_MAP.get(preset_id)
    if not preset:
        return _error_response(
            datetime.now(timezone.utc),
            f"Unknown preset: {preset_id}. Valid: {list(_PRESET_MAP.keys())}",
        )

    families = preset.get("families")
    # For default preset (families=None), return unfiltered
    if not families:
        return build_relevance_explain(request, asset, horizon=horizon, day=day)

    # Load data once, filter to union of families
    now = datetime.now(timezone.utc)
    rfile = _resolve_file(day)
    etag = _file_etag(rfile)
    mtime = _file_mtime_dt(rfile)

    cached = _check_conditional(request, etag, mtime)
    if cached:
        return cached

    data, err = _load_relevance(day)
    if err:
        return _error_response(now, err)

    asset_upper = asset.upper()
    asset_data = data.get("assets", {}).get(asset_upper)
    if not asset_data:
        return _error_response(now, f"No data for asset {asset_upper}")

    h = (horizon or "24h").lower()
    h_data = asset_data.get("horizons", {}).get(h, {})
    top = h_data.get("top", [])
    meta = h_data.get("meta", {})

    family_set = set(families)
    features = []
    for item in top:
        fam = (item.get("family") or "").lower()
        if fam not in family_set:
            continue
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

    # Apply preset-aware re-ranking
    features = _apply_preset_scoring(features, preset)

    result: dict[str, Any] = {
        "ok": True,
        "generated_at": now.isoformat(),
        "snapshot_age_s": _snapshot_age(data),
        **_file_meta(rfile),
        "asset": asset_upper,
        "horizon": h,
        "day": data.get("day"),
        "preset": preset,
        "regime_bucket": asset_data.get("regime_bucket"),
        "regime_label": asset_data.get("regime_label"),
        "scoring_mode": meta.get("scoring_mode", data.get("scoring_mode")),
        "n_features": len(features),
        "features": features,
        "family_distribution": _family_distribution(features),
    }
    return JSONResponse(content=result, headers=_cache_headers(etag, mtime))
