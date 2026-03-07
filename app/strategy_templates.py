"""STRATEGY-TEMPLATES-001: Static strategy templates + instantiate flow.

Templates are hardcoded for v1. Instantiation creates a normal strategy
via the strategies module.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from .strategies import _load_store, _save_store, _validate_name, MAX_STRATEGIES

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
