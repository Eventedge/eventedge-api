"""AGENT-RELEVANCE-API-001/002/003 + AGENT-CACHE-001: Read-only relevance endpoints.

Serves /home/eventedge/alerts/relevance_now.json as structured API responses.
No DB required — pure filesystem read.

Cache strategy: ETag derived from file mtime+size, Last-Modified from mtime.
Supports If-None-Match and If-Modified-Since conditional requests (304).

API-002: dated snapshots (?day=), filtered slices (?family=, ?top_k=),
agent presets, richer explain metadata (family distribution, stability, caps).

API-003/AGENT-PRESETS-001: preset-aware re-ranking with family weight multipliers,
preferred redundancy groups, mean_reversion preset, /presets/explain endpoint.
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
# Agent presets with ranking weights
# ---------------------------------------------------------------------------

PRESETS: list[dict[str, Any]] = [
    {
        "id": "default",
        "label": "Default",
        "description": "Top mixed signals across all families",
        "intent": "Balanced overview — no family bias, pure EdgeMind ranking",
        "families": None,
        "family_weights": None,
        "preferred_groups": None,
    },
    {
        "id": "momentum",
        "label": "Momentum",
        "description": "Trend and directional signals from TA and derivatives",
        "intent": "Catch breakouts and trend continuations — favors TA scanners and OI acceleration",
        "families": ["ta", "derivs"],
        "family_weights": {"ta": 1.3, "derivs": 1.2},
        "preferred_groups": ["ta.scanner", "ta.confluence", "derivs.crowding"],
    },
    {
        "id": "mean_reversion",
        "label": "Mean Reversion",
        "description": "Funding extremes, crowding, and PM conviction for reversal signals",
        "intent": "Identify overextended positioning — favors funding, liquidation bias, and PM divergence",
        "families": ["derivs", "pm"],
        "family_weights": {"derivs": 1.3, "pm": 1.2},
        "preferred_groups": ["derivs.funding", "derivs.crowding", "pm.conviction"],
    },
    {
        "id": "macro",
        "label": "Macro",
        "description": "Macro regime and prediction market signals",
        "intent": "Top-down view — ETF flows, risk regime, and prediction market consensus",
        "families": ["macro", "pm"],
        "family_weights": {"macro": 1.3, "pm": 1.2},
        "preferred_groups": ["macro.risk", "pm.conviction"],
    },
    {
        "id": "defensive",
        "label": "Defensive",
        "description": "Quality, alerts, and macro-focused risk view",
        "intent": "Monitor system health and risk signals — useful during uncertainty",
        "families": ["quality", "alerts", "macro"],
        "family_weights": {"quality": 1.2, "alerts": 1.2, "macro": 1.1},
        "preferred_groups": None,
    },
    {
        "id": "derivatives",
        "label": "Derivatives",
        "description": "Funding, OI, liquidation, and leverage signals",
        "intent": "Pure derivatives view — funding rates, open interest, liquidation imbalance",
        "families": ["deriv", "derivs"],
        "family_weights": {"deriv": 1.1, "derivs": 1.3},
        "preferred_groups": ["derivs.funding", "derivs.crowding", "derivs.termstructure"],
    },
]

_PRESET_MAP: dict[str, dict[str, Any]] = {p["id"]: p for p in PRESETS}

# Bonus for features whose redundancy_group starts with a preferred group prefix
_PREFERRED_GROUP_BONUS = 0.10


# ---------------------------------------------------------------------------
# File-based cache helpers
# ---------------------------------------------------------------------------

def _file_etag(path: Path) -> str | None:
    try:
        st = path.stat()
        raw = f"{st.st_mtime_ns}:{st.st_size}".encode()
        return f'W/"{hashlib.md5(raw).hexdigest()}"'
    except OSError:
        return None


def _file_mtime_dt(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _check_conditional(request: Request, etag: str | None, mtime: datetime | None) -> Response | None:
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
    if day and _DAY_RE.match(day):
        dated = ALERTS_DIR / f"relevance_now.{day}.json"
        if dated.exists():
            return dated
    return RELEVANCE_FILE


def _load_relevance(day: str | None = None) -> tuple[dict | None, str | None]:
    path = _resolve_file(day)
    if not path.exists():
        return None, f"{path.name} not found"
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
# Feature helpers
# ---------------------------------------------------------------------------

def _filter_features(
    features: list[dict[str, Any]],
    family: str | None = None,
    top_k: int | None = None,
) -> list[dict[str, Any]]:
    result = features
    if family:
        fam_lower = family.lower()
        result = [f for f in result if (f.get("family") or "").lower() == fam_lower]
    if top_k and top_k > 0:
        result = result[:top_k]
    return result


def _family_distribution(features: list[dict[str, Any]]) -> dict[str, int]:
    return dict(Counter(f.get("family", "unknown") for f in features).most_common())


def _build_feature_entry(item: dict[str, Any]) -> dict[str, Any]:
    """Extract a feature entry from a raw snapshot top-list item."""
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
    return entry


# ---------------------------------------------------------------------------
# Preset-aware re-ranking
# ---------------------------------------------------------------------------

def _apply_preset_ranking(
    features: list[dict[str, Any]],
    preset: dict[str, Any],
) -> list[dict[str, Any]]:
    """Re-rank features based on preset family weights and preferred groups.

    Returns new list with preset_score and preset_rank fields added.
    Original rank/score are preserved as base_rank/base_score.
    """
    family_weights: dict[str, float] | None = preset.get("family_weights")
    preferred_groups: list[str] | None = preset.get("preferred_groups")
    families_filter: list[str] | None = preset.get("families")

    # Filter to preset families if specified
    if families_filter:
        family_set = set(families_filter)
        features = [f for f in features if (f.get("family") or "").lower() in family_set]

    # Compute preset scores
    ranked = []
    for f in features:
        base_score = f.get("score", 0) or 0
        fam = (f.get("family") or "").lower()
        multiplier = 1.0

        if family_weights and fam in family_weights:
            multiplier = family_weights[fam]

        # Preferred group bonus based on feature_id prefix matching
        group_bonus = 0.0
        if preferred_groups:
            fid = (f.get("feature_id") or "").lower()
            for pg in preferred_groups:
                if fid.startswith(pg.lower()):
                    group_bonus = _PREFERRED_GROUP_BONUS
                    break

        preset_score = round(base_score * multiplier + group_bonus, 4)

        entry = {
            **f,
            "base_rank": f.get("rank"),
            "base_score": base_score,
            "preset_score": preset_score,
            "preset_multiplier": multiplier,
        }
        if group_bonus > 0:
            entry["preset_group_bonus"] = group_bonus
        ranked.append(entry)

    # Sort by preset_score descending
    ranked.sort(key=lambda x: x["preset_score"], reverse=True)

    # Assign new ranks
    for i, entry in enumerate(ranked, 1):
        entry["preset_rank"] = i
        entry["rank"] = i  # Override rank for display

    return ranked


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def build_relevance_now(request: Request, day: str | None = None) -> JSONResponse:
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

    features = [_build_feature_entry(item) for item in top]

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
    return JSONResponse(
        content={
            "ok": True,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "presets": PRESETS,
        },
        headers={"Cache-Control": "public, max-age=3600"},
    )


def build_relevance_presets_explain() -> JSONResponse:
    """Return detailed preset definitions with weights and intent."""
    details = []
    for p in PRESETS:
        detail: dict[str, Any] = {
            "id": p["id"],
            "label": p["label"],
            "description": p["description"],
            "intent": p.get("intent", ""),
            "families": p.get("families"),
            "family_weights": p.get("family_weights"),
            "preferred_groups": p.get("preferred_groups"),
            "preferred_group_bonus": _PREFERRED_GROUP_BONUS if p.get("preferred_groups") else 0,
        }
        details.append(detail)
    return JSONResponse(
        content={
            "ok": True,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "presets": details,
        },
        headers={"Cache-Control": "public, max-age=3600"},
    )


def build_relevance_preset_view(
    request: Request,
    asset: str,
    preset_id: str,
    horizon: str | None = None,
    day: str | None = None,
) -> JSONResponse:
    """Apply preset-aware re-ranking and return explain-like response."""
    preset = _PRESET_MAP.get(preset_id)
    if not preset:
        return _error_response(
            datetime.now(timezone.utc),
            f"Unknown preset: {preset_id}. Valid: {list(_PRESET_MAP.keys())}",
        )

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

    # Build base feature entries
    base_features = [_build_feature_entry(item) for item in top]

    # Apply preset-aware re-ranking
    ranked = _apply_preset_ranking(base_features, preset)

    # Build reason string per feature
    for f in ranked:
        reasons = []
        if f.get("preset_multiplier", 1.0) != 1.0:
            reasons.append(f"family weight {f['preset_multiplier']:.1f}x")
        if f.get("preset_group_bonus"):
            reasons.append(f"preferred group +{f['preset_group_bonus']:.2f}")
        f["preset_reason"] = "; ".join(reasons) if reasons else "base score"

    result: dict[str, Any] = {
        "ok": True,
        "generated_at": now.isoformat(),
        "snapshot_age_s": _snapshot_age(data),
        **_file_meta(rfile),
        "asset": asset_upper,
        "horizon": h,
        "day": data.get("day"),
        "preset": {
            "id": preset["id"],
            "label": preset["label"],
            "description": preset["description"],
            "intent": preset.get("intent", ""),
        },
        "regime_bucket": asset_data.get("regime_bucket"),
        "regime_label": asset_data.get("regime_label"),
        "scoring_mode": meta.get("scoring_mode", data.get("scoring_mode")),
        "n_features": len(ranked),
        "features": ranked,
        "family_distribution": _family_distribution(ranked),
    }
    return JSONResponse(content=result, headers=_cache_headers(etag, mtime))
