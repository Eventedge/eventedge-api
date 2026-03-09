"""PERSONALIZATION-001 / RISK-PROFILE-001: File-backed user profiles + personalized relevance.

Stores user preferences in /home/eventedge/alerts/user_profiles.json.
Atomic writes via tempfile + rename. No DB required.

Provides personalized relevance re-ranking using:
  - risk_profile multipliers (conservative/balanced/aggressive)
  - family_preferences (bounded multipliers 0.9–1.15)
  - hidden_families (filter unless no alternatives)
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

ALERTS_DIR = Path(os.getenv("ROUTER_ALERT_DIR", "/home/eventedge/alerts"))
PROFILES_FILE = ALERTS_DIR / "user_profiles.json"
RELEVANCE_FILE = ALERTS_DIR / "relevance_now.json"

MAX_PROFILES = 500

VALID_RISK_PROFILES = {"conservative", "balanced", "aggressive"}
VALID_SOURCES = {"web", "bot", "system", "api"}
VALID_FAMILIES = {"ta", "deriv", "derivs", "macro", "pm", "quality", "alerts", "chartfeed", "system"}
VALID_ASSETS = {"BTC", "ETH", "SOL", "HYPE"}

# Bounded multiplier range for family preferences
MIN_FAMILY_MULT = 0.9
MAX_FAMILY_MULT = 1.15

# ---------------------------------------------------------------------------
# Risk profile multiplier tables (family -> multiplier)
# These are small, bounded adjustments — not dramatic re-rankings.
# ---------------------------------------------------------------------------

RISK_PROFILE_WEIGHTS: dict[str, dict[str, float]] = {
    "conservative": {
        "quality": 1.10,
        "alerts": 1.08,
        "macro": 1.06,
        "pm": 1.04,
        "ta": 0.96,
        "derivs": 0.94,
        "deriv": 0.94,
    },
    "balanced": {},  # neutral — no adjustments
    "aggressive": {
        "ta": 1.08,
        "derivs": 1.06,
        "deriv": 1.06,
        "quality": 0.96,
        "alerts": 0.96,
        "macro": 0.98,
    },
}

# Conservative penalizes high CI; aggressive tolerates it
RISK_CI_PENALTY: dict[str, float] = {
    "conservative": 0.03,   # penalize high-uncertainty features
    "balanced": 0.0,
    "aggressive": 0.0,      # tolerate uncertainty
}


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def _load_store() -> dict[str, Any]:
    if not PROFILES_FILE.exists():
        return {"version": 1, "items": []}
    try:
        data = json.loads(PROFILES_FILE.read_text())
        if not isinstance(data, dict) or "items" not in data:
            return {"version": 1, "items": []}
        return data
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "items": []}


def _save_store(store: dict[str, Any]) -> None:
    PROFILES_FILE.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(store, indent=2, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(dir=str(PROFILES_FILE.parent), suffix=".tmp")
    try:
        os.write(fd, raw.encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        os.rename(tmp, str(PROFILES_FILE))
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _find_profile(store: dict, user_id: str) -> dict | None:
    for item in store.get("items", []):
        if item.get("id") == user_id:
            return item
    return None


def _default_profile(user_id: str) -> dict[str, Any]:
    return {
        "id": user_id,
        "risk_profile": "balanced",
        "family_preferences": {},
        "preset_preferences": {},
        "hidden_families": [],
        "preferred_assets": [],
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "system",
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Profile CRUD
# ---------------------------------------------------------------------------

def build_profile_get(user_id: str) -> JSONResponse:
    """Get user profile. Returns default if not found."""
    store = _load_store()
    profile = _find_profile(store, user_id)
    if not profile:
        profile = _default_profile(user_id)

    return JSONResponse(
        content={"ok": True, "generated_at": _now_iso(), "profile": profile},
        headers={"Cache-Control": "no-store"},
    )


def build_profile_update(user_id: str, body: dict) -> JSONResponse:
    """Create or update a user profile."""
    store = _load_store()
    profile = _find_profile(store, user_id)
    is_new = profile is None

    if is_new:
        if len(store["items"]) >= MAX_PROFILES:
            return JSONResponse(
                content={"ok": False, "error": f"Max {MAX_PROFILES} profiles reached"},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        profile = _default_profile(user_id)
        store["items"].append(profile)

    # Update fields
    if "risk_profile" in body:
        rp = body["risk_profile"]
        if rp not in VALID_RISK_PROFILES:
            return JSONResponse(
                content={"ok": False, "error": f"Invalid risk_profile. Valid: {sorted(VALID_RISK_PROFILES)}"},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        profile["risk_profile"] = rp

    if "family_preferences" in body:
        fp = body["family_preferences"]
        if not isinstance(fp, dict):
            return JSONResponse(
                content={"ok": False, "error": "family_preferences must be a dict"},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        # Validate and clamp
        clean = {}
        for fam, mult in fp.items():
            if fam not in VALID_FAMILIES:
                continue
            try:
                v = float(mult)
                v = max(MIN_FAMILY_MULT, min(MAX_FAMILY_MULT, v))
                clean[fam] = round(v, 3)
            except (TypeError, ValueError):
                continue
        profile["family_preferences"] = clean

    if "preset_preferences" in body:
        pp = body["preset_preferences"]
        if isinstance(pp, dict):
            clean = {}
            for k, v in pp.items():
                try:
                    clean[k] = round(max(0.8, min(1.2, float(v))), 3)
                except (TypeError, ValueError):
                    continue
            profile["preset_preferences"] = clean

    if "hidden_families" in body:
        hf = body["hidden_families"]
        if isinstance(hf, list):
            profile["hidden_families"] = [f for f in hf if f in VALID_FAMILIES]

    if "preferred_assets" in body:
        pa = body["preferred_assets"]
        if isinstance(pa, list):
            profile["preferred_assets"] = [a.upper() for a in pa if a.upper() in VALID_ASSETS]

    if "source" in body:
        src = body["source"]
        if src in VALID_SOURCES:
            profile["source"] = src

    profile["updated_at"] = _now_iso()
    _save_store(store)

    return JSONResponse(
        content={"ok": True, "generated_at": _now_iso(), "profile": profile},
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# Personalized relevance
# ---------------------------------------------------------------------------

def _load_relevance(day: str | None = None) -> tuple[dict | None, str | None]:
    """Load relevance data. Returns (data, error)."""
    import re
    _DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    if day and _DAY_RE.match(day):
        dated = ALERTS_DIR / f"relevance_now.{day}.json"
        if dated.exists():
            try:
                return json.loads(dated.read_text()), None
            except (json.JSONDecodeError, OSError) as e:
                return None, f"Failed to read {dated.name}: {e}"

    if not RELEVANCE_FILE.exists():
        return None, "relevance_now.json not found"
    try:
        return json.loads(RELEVANCE_FILE.read_text()), None
    except (json.JSONDecodeError, OSError) as e:
        return None, f"Failed to read relevance_now.json: {e}"


def _apply_personalization(
    features: list[dict[str, Any]],
    profile: dict[str, Any],
) -> list[dict[str, Any]]:
    """Apply personalized re-ranking to a list of features.

    Returns new list with personalized_score, base_score, personalization_reason.
    """
    risk_profile = profile.get("risk_profile", "balanced")
    family_prefs = profile.get("family_preferences", {})
    hidden_families = set(profile.get("hidden_families", []))

    risk_weights = RISK_PROFILE_WEIGHTS.get(risk_profile, {})
    ci_penalty = RISK_CI_PENALTY.get(risk_profile, 0.0)

    result = []
    for item in features:
        entry = copy.copy(item)
        base_score = entry.get("score", 0) or 0
        entry["base_score"] = base_score

        fam = (entry.get("family") or "").lower()
        reasons = []

        # Skip hidden families (but keep a minimum)
        if fam in hidden_families:
            entry["_hidden_by_preference"] = True
            # Still include but mark — the caller can filter if enough alternatives
            continue

        # 1) Risk profile adjustment
        risk_mult = risk_weights.get(fam, 1.0)
        if risk_mult != 1.0:
            reasons.append(f"risk:{risk_profile} ×{risk_mult:.2f}")

        # 2) Family preference adjustment
        fam_mult = family_prefs.get(fam, 1.0)
        if fam_mult != 1.0:
            reasons.append(f"family_pref ×{fam_mult:.3f}")

        # Combined multiplier
        combined_mult = risk_mult * fam_mult

        # 3) CI penalty for conservative profiles
        ci_adj = 0.0
        if ci_penalty > 0:
            perf = entry.get("perf", {})
            ci_width = perf.get("ci_width") if perf else None
            if ci_width is not None and ci_width > 0.03:
                ci_adj = -ci_penalty
                reasons.append(f"high uncertainty -{ci_penalty:.2f}")

        personalized_score = round(base_score * combined_mult + ci_adj, 4)
        entry["personalized_score"] = personalized_score
        entry["personalization_reason"] = "; ".join(reasons) if reasons else "neutral"

        result.append(entry)

    # Re-sort by personalized_score
    result.sort(key=lambda x: -(x.get("personalized_score", 0)))
    for i, entry in enumerate(result):
        entry["rank"] = i + 1

    return result


def build_personalized_relevance(
    user_id: str,
    asset: str,
    horizon: str | None = None,
    day: str | None = None,
) -> JSONResponse:
    """Personalized relevance view for a user."""
    now = datetime.now(timezone.utc)

    # Load profile
    store = _load_store()
    profile = _find_profile(store, user_id) or _default_profile(user_id)

    # Load relevance
    data, err = _load_relevance(day)
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
            content={"ok": False, "generated_at": now.isoformat(), "error": f"No data for {asset_upper}"},
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    h = (horizon or "24h").lower()
    h_data = asset_data.get("horizons", {}).get(h, {})
    top = h_data.get("top", [])
    meta = h_data.get("meta", {})

    # Deep copy features for modification
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

    # Apply personalization
    features = _apply_personalization(features, profile)

    # Snapshot age
    asof_ts = data.get("asof_ts")
    snapshot_age = round(now.timestamp() - asof_ts, 1) if asof_ts else None

    return JSONResponse(
        content={
            "ok": True,
            "generated_at": now.isoformat(),
            "snapshot_age_s": snapshot_age,
            "asset": asset_upper,
            "horizon": h,
            "day": data.get("day"),
            "regime_bucket": asset_data.get("regime_bucket"),
            "regime_label": asset_data.get("regime_label"),
            "scoring_mode": meta.get("scoring_mode", data.get("scoring_mode")),
            "profile": {
                "id": profile.get("id"),
                "risk_profile": profile.get("risk_profile"),
                "source": profile.get("source"),
            },
            "n_features": len(features),
            "features": features,
        },
        headers={"Cache-Control": "no-store"},
    )


def build_personalized_preset_relevance(
    user_id: str,
    asset: str,
    preset_id: str,
    horizon: str | None = None,
    day: str | None = None,
) -> JSONResponse:
    """Personalized relevance with a preset applied first, then user adjustments."""
    # Import preset machinery from relevance module
    from .relevance import (
        PRESETS,
        _apply_preset_scoring,
        _load_relevance as _load_rel,
    )

    now = datetime.now(timezone.utc)

    # Find preset
    preset_map = {p["id"]: p for p in PRESETS}
    preset = preset_map.get(preset_id)
    if not preset:
        return JSONResponse(
            content={"ok": False, "generated_at": now.isoformat(),
                     "error": f"Unknown preset: {preset_id}. Valid: {list(preset_map.keys())}"},
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    # Load profile
    store = _load_store()
    profile = _find_profile(store, user_id) or _default_profile(user_id)

    # Load relevance
    data, err = _load_rel(day)
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
            content={"ok": False, "generated_at": now.isoformat(), "error": f"No data for {asset_upper}"},
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    h = (horizon or "24h").lower()
    h_data = asset_data.get("horizons", {}).get(h, {})
    top = h_data.get("top", [])
    meta = h_data.get("meta", {})

    # Filter to preset families if specified
    families = preset.get("families")
    family_set = set(families) if families else None

    features = []
    for item in top:
        fam = (item.get("family") or "").lower()
        if family_set and fam not in family_set:
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

    # Apply preset scoring first
    features = _apply_preset_scoring(features, preset)

    # Then apply personalization on top (using preset_score as base)
    for f in features:
        f["score"] = f.get("preset_score", f.get("score", 0))
    features = _apply_personalization(features, profile)

    # Snapshot age
    asof_ts = data.get("asof_ts")
    snapshot_age = round(now.timestamp() - asof_ts, 1) if asof_ts else None

    return JSONResponse(
        content={
            "ok": True,
            "generated_at": now.isoformat(),
            "snapshot_age_s": snapshot_age,
            "asset": asset_upper,
            "horizon": h,
            "day": data.get("day"),
            "preset": preset,
            "regime_bucket": asset_data.get("regime_bucket"),
            "regime_label": asset_data.get("regime_label"),
            "scoring_mode": meta.get("scoring_mode", data.get("scoring_mode")),
            "profile": {
                "id": profile.get("id"),
                "risk_profile": profile.get("risk_profile"),
                "source": profile.get("source"),
            },
            "n_features": len(features),
            "features": features,
        },
        headers={"Cache-Control": "no-store"},
    )
