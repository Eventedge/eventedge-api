"""STRATEGY-TEMPLATES-001/002: Static strategy templates + instantiate + recommendations.

Templates are hardcoded for v1. Instantiation creates a normal strategy
via the strategies module.

v002: enriched templates with recommended_regimes, risk_style, usage_note;
      recommendations endpoint using current regime + top_families.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from .strategies import (
    _load_store, _save_store, _validate_name, _validate_tags,
    _ensure_metadata, MAX_STRATEGIES,
)

TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "default_workspace",
        "label": "Default Workspace",
        "description": "Balanced view across all signal families with no filters.",
        "defaults": {
            "asset": "BTC",
            "horizon": "24h",
            "preset_id": "default",
            "family_filter": None,
            "pinned": {},
            "hidden": {},
        },
        "rules": {
            "recommended_families": [],
            "note": "Good starting point for any market condition.",
        },
        "recommended_regimes": [],
        "risk_style": "balanced",
        "usage_note": "Works in any regime. A neutral starting point before customizing.",
        "template_quality_note": "No family bias — covers all signals equally.",
    },
    {
        "id": "momentum_workspace",
        "label": "Momentum Workspace",
        "description": "Trend-following signals from TA scanners and derivatives directional indicators.",
        "defaults": {
            "asset": "BTC",
            "horizon": "24h",
            "preset_id": "momentum",
            "family_filter": None,
            "pinned": {},
            "hidden": {},
        },
        "rules": {
            "recommended_families": ["ta", "derivs"],
            "note": "Best for trend-following regimes.",
        },
        "recommended_regimes": ["trend_up", "trend_up_highvol", "breakout"],
        "risk_style": "aggressive",
        "usage_note": "Best when clear directional trend is established. Avoid in choppy ranges.",
        "template_quality_note": "Relies heavily on TA scanner + derivatives direction signals.",
    },
    {
        "id": "mean_reversion_workspace",
        "label": "Mean Reversion Workspace",
        "description": "Funding extremes, liquidation bias, and prediction market signals for contrarian plays.",
        "defaults": {
            "asset": "BTC",
            "horizon": "24h",
            "preset_id": "derivatives",
            "family_filter": None,
            "pinned": {},
            "hidden": {},
        },
        "rules": {
            "recommended_families": ["derivs", "pm"],
            "note": "Best for range-bound or overextended regimes.",
        },
        "recommended_regimes": ["range_lowvol", "range_highvol", "overextended"],
        "risk_style": "balanced",
        "usage_note": "Works best when funding is extreme or OI is stretched. Contrarian by design.",
        "template_quality_note": "Combines derivatives extremes with prediction market conviction.",
    },
    {
        "id": "defensive_workspace",
        "label": "Defensive Workspace",
        "description": "Quality, alerts, and macro risk signals for capital preservation.",
        "defaults": {
            "asset": "BTC",
            "horizon": "24h",
            "preset_id": "defensive",
            "family_filter": None,
            "pinned": {},
            "hidden": {},
        },
        "rules": {
            "recommended_families": ["quality", "alerts", "macro"],
            "note": "Best during high uncertainty or drawdown regimes.",
        },
        "recommended_regimes": ["drawdown", "high_uncertainty", "crisis"],
        "risk_style": "conservative",
        "usage_note": "Capital preservation focus. Prioritizes data quality and risk-off signals.",
        "template_quality_note": "Emphasizes system health and macro risk over directional alpha.",
    },
    {
        "id": "macro_workspace",
        "label": "Macro Workspace",
        "description": "Macro regime and prediction market conviction signals.",
        "defaults": {
            "asset": "BTC",
            "horizon": "24h",
            "preset_id": "macro",
            "family_filter": None,
            "pinned": {},
            "hidden": {},
        },
        "rules": {
            "recommended_families": ["macro", "pm"],
            "note": "Best when macro catalysts are driving price.",
        },
        "recommended_regimes": ["macro_driven", "risk_on", "risk_off"],
        "risk_style": "balanced",
        "usage_note": "Best when macro events (DXY, rates, ETF flows) dominate price action.",
        "template_quality_note": "DXY/rates/flow signals combined with prediction market conviction.",
    },
]

_TEMPLATE_MAP: dict[str, dict[str, Any]] = {t["id"]: t for t in TEMPLATES}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_templates_list() -> JSONResponse:
    """GET /api/v1/strategy-templates"""
    return JSONResponse(
        content={
            "ok": True,
            "generated_at": _now_iso(),
            "count": len(TEMPLATES),
            "items": TEMPLATES,
        },
        headers={"Cache-Control": "public, max-age=300"},
    )


def build_template_get(template_id: str) -> JSONResponse:
    """GET /api/v1/strategy-templates/{id}"""
    tpl = _TEMPLATE_MAP.get(template_id)
    if not tpl:
        return JSONResponse(
            content={"ok": False, "generated_at": _now_iso(), "error": f"Template '{template_id}' not found"},
            status_code=404,
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse(
        content={"ok": True, "generated_at": _now_iso(), "item": tpl},
        headers={"Cache-Control": "public, max-age=300"},
    )


async def build_template_instantiate(request: Request, template_id: str) -> JSONResponse:
    """POST /api/v1/strategy-templates/{id}/instantiate — create strategy from template."""
    tpl = _TEMPLATE_MAP.get(template_id)
    if not tpl:
        return JSONResponse(
            content={"ok": False, "generated_at": _now_iso(), "error": f"Template '{template_id}' not found"},
            status_code=404,
            headers={"Cache-Control": "no-store"},
        )

    try:
        body = await request.json()
    except Exception:
        body = {}

    name = body.get("name", tpl["label"])
    err = _validate_name(name)
    if err:
        return JSONResponse(
            content={"ok": False, "error": err},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )

    store = _load_store()
    if len(store["items"]) >= MAX_STRATEGIES:
        return JSONResponse(
            content={"ok": False, "error": f"Max {MAX_STRATEGIES} strategies reached"},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )

    defaults = tpl["defaults"]
    now = _now_iso()
    item = {
        "id": str(uuid.uuid4()),
        "name": name.strip(),
        "created_at": now,
        "updated_at": now,
        "source": "template",
        "template_id": template_id,
        "template_version": "1.0",
        "payload": {
            "asset_defaults": {
                "asset": defaults.get("asset", "BTC"),
                "horizon": defaults.get("horizon", "24h"),
            },
            "preset_id": defaults.get("preset_id"),
            "family_filter": defaults.get("family_filter"),
            "pinned": defaults.get("pinned", {}),
            "hidden": defaults.get("hidden", {}),
        },
        "description": tpl.get("description"),
        "tags": list(tpl.get("rules", {}).get("recommended_families", [])),
        "preferred_regime": (tpl.get("recommended_regimes") or [None])[0],
        "revision": 1,
        "archived": False,
    }
    store["items"].append(item)
    _save_store(store)

    return JSONResponse(
        content={
            "ok": True,
            "generated_at": now,
            "item": item,
            "template": tpl,
        },
        status_code=201,
        headers={"Cache-Control": "no-store"},
    )


def build_workspace_summary(strategy_id: str) -> JSONResponse:
    """GET /api/v1/strategies/{id}/workspace_summary — condensed summary."""
    store = _load_store()
    target = None
    for item in store["items"]:
        if item["id"] == strategy_id:
            target = item
            break

    if not target:
        return JSONResponse(
            content={"ok": False, "generated_at": _now_iso(), "error": f"Strategy {strategy_id} not found"},
            status_code=404,
            headers={"Cache-Control": "no-store"},
        )

    _ensure_metadata(target)
    payload = target.get("payload", {})
    ad = payload.get("asset_defaults", {})
    pinned = payload.get("pinned", {})
    hidden = payload.get("hidden", {})

    pin_count = sum(len(v) for v in pinned.values()) if isinstance(pinned, dict) else 0
    hide_count = sum(len(v) for v in hidden.values()) if isinstance(hidden, dict) else 0

    # Family distribution from pinned features
    families: dict[str, int] = {}
    for ids_list in (pinned.values() if isinstance(pinned, dict) else []):
        for fid in ids_list:
            fam = fid.split(".")[0] if "." in fid else "unknown"
            families[fam] = families.get(fam, 0) + 1

    tpl_id = target.get("template_id")
    tpl = _TEMPLATE_MAP.get(tpl_id) if tpl_id else None

    summary = {
        "ok": True,
        "generated_at": _now_iso(),
        "strategy_id": strategy_id,
        "name": target["name"],
        "source": target.get("source", "api"),
        "asset": ad.get("asset", "BTC"),
        "horizon": ad.get("horizon", "24h"),
        "preset_id": payload.get("preset_id"),
        "family_filter": payload.get("family_filter"),
        "pinned_count": pin_count,
        "hidden_count": hide_count,
        "family_distribution": families,
        "description": target.get("description"),
        "tags": target.get("tags", []),
        "preferred_regime": target.get("preferred_regime"),
        "revision": target.get("revision", 1),
        "archived": target.get("archived", False),
    }

    if tpl:
        summary["template"] = {
            "id": tpl["id"],
            "label": tpl["label"],
            "note": tpl.get("rules", {}).get("note", ""),
        }

    return JSONResponse(
        content=summary,
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# Template recommendations — rule-based fit scoring
# ---------------------------------------------------------------------------

ALERTS_DIR = Path(os.getenv("ROUTER_ALERT_DIR", "/home/eventedge/alerts"))
RELEVANCE_FILE = ALERTS_DIR / "relevance_now.json"


def _load_relevance_lite(asset: str, horizon: str) -> tuple[str, list[str]]:
    """Load current regime + top_families for asset/horizon. Returns (regime, [family_names])."""
    try:
        data = json.loads(RELEVANCE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return "unknown", []
    asset_data = data.get("assets", {}).get(asset.upper(), {})
    regime = asset_data.get("regime_bucket", "unknown")
    h_data = asset_data.get("horizons", {}).get(horizon.lower(), {})
    top_families = h_data.get("meta", {}).get("top_families", [])
    family_names = [f.get("family", "") for f in top_families if f.get("family")]
    return regime, family_names


def build_template_recommendations(
    asset: str = "BTC", horizon: str = "24h",
) -> JSONResponse:
    """GET /api/v1/strategy-templates/recommendations — templates ranked by fit."""
    now = _now_iso()
    regime, active_families = _load_relevance_lite(asset, horizon)

    scored: list[dict] = []
    for tpl in TEMPLATES:
        fit_score = 0.0
        reasons: list[str] = []

        # Regime match
        rec_regimes = tpl.get("recommended_regimes", [])
        if not rec_regimes:
            fit_score += 0.3
            reasons.append("universal (any regime)")
        elif regime in rec_regimes:
            fit_score += 0.5
            reasons.append(f"regime match: {regime}")
        else:
            # Partial: check if regime bucket starts with any recommended
            partial = any(regime.startswith(r.split("_")[0]) for r in rec_regimes if r)
            if partial:
                fit_score += 0.2
                reasons.append("partial regime match")

        # Family overlap
        rec_families = set(tpl.get("rules", {}).get("recommended_families", []))
        if rec_families and active_families:
            overlap = rec_families & set(active_families[:5])
            if overlap:
                bonus = 0.3 * len(overlap) / max(len(rec_families), 1)
                fit_score += bonus
                reasons.append(f"active families: {', '.join(sorted(overlap))}")
            # Extra boost if recommended family is #1
            if active_families and active_families[0] in rec_families:
                fit_score += 0.1
                reasons.append(f"top family: {active_families[0]}")
        elif not rec_families:
            fit_score += 0.15
            reasons.append("no family bias")

        scored.append({
            "template": tpl,
            "fit_score": round(fit_score, 3),
            "reasons": reasons,
        })

    scored.sort(key=lambda x: x["fit_score"], reverse=True)

    return JSONResponse(
        content={
            "ok": True,
            "generated_at": now,
            "asset": asset,
            "horizon": horizon,
            "regime": regime,
            "active_families": active_families[:5],
            "count": len(scored),
            "items": scored,
        },
        headers={"Cache-Control": "no-store"},
    )
