from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .fear_greed import get_fear_greed
from .paper import build_paper_summary
from .regime import build_regime
from .alerts import build_alerts_live
from .health_services import (
    build_health_services,
    ingest_heartbeat,
    HEARTBEAT_INGEST_SECRET,
    _ALLOWED_SERVICES,
)
from .telemetry_overview import build_telemetry_overview
from .telemetry_summary import build_telemetry_summary
from .telemetry_users import build_telemetry_users
from .telemetry_invites import build_telemetry_invites
from .telemetry_alerts import build_telemetry_alerts
from .telemetry_paper import build_telemetry_paper
from .telemetry_data import build_telemetry_data
from .telemetry_scanners import build_telemetry_scanners
from .telemetry_ta_health import build_telemetry_ta_health
from .telemetry_ta_relevance import build_telemetry_ta_relevance
from .ops_backup_status import build_backup_status
from .admin_alert_settings import (
    build_alert_settings_list,
    build_alert_settings_get,
    build_alert_settings_update,
)
from .admin_alert_telemetry import build_delivery_telemetry
from .admin_alert_health import build_alert_health
from .telemetry_pm_health import build_telemetry_pm_health
from .telemetry_chartfeed import build_telemetry_chartfeed
from .telemetry_health_summary import build_health_summary
from .simlab import build_simlab_overview, build_simlab_trades_live
from .supercard import build_supercard
from .snapshots import (
    extract_funding,
    extract_global,
    extract_liquidations,
    extract_oi,
    extract_price,
    fmt_pct,
    fmt_usd,
    get_snapshot,
)
from .hypepipe import router as hypepipe_router
from .relevance import (
    build_relevance_now,
    build_relevance_asset,
    build_relevance_explain,
    build_relevance_presets,
    build_relevance_preset_view,
)
from .strategies import (
    build_strategies_list,
    build_strategy_get,
    build_strategy_create,
    build_strategy_update,
    build_strategy_delete,
    build_strategy_import,
    build_strategy_export,
)
from .strategy_alerts import (
    build_alert_list,
    build_alert_get,
    build_alert_create,
    build_alert_update,
    build_alert_delete,
    build_strategy_diff,
    build_alert_preview,
    build_alert_history,
)
from .strategy_templates import (
    build_templates_list,
    build_template_get,
    build_template_instantiate,
    build_workspace_summary,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def etag_for(obj: Any) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def json_with_cache(payload: Dict[str, Any], cache_control: str) -> JSONResponse:
    etag = etag_for(payload)
    return JSONResponse(
        content=payload,
        headers={
            "Cache-Control": cache_control,
            "ETag": etag,
        },
    )


def _src_ts(snap: dict | None) -> str | None:
    if snap and snap.get("updated_at"):
        return snap["updated_at"].isoformat()
    return None


app = FastAPI(title="EventEdge API", version="v1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://edgeblocks.io",
        "https://www.edgeblocks.io",
        "https://admin.edgeblocks.io",
        "http://localhost:3000",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(hypepipe_router)


@app.get("/api/v1/health")
def health():
    payload = {"ok": True, "service": "eventedge-api", "api": "v1", "ts": now_iso()}
    return json_with_cache(payload, "public, max-age=5")


@app.get("/api/v1/admin/health/services")
def admin_health_services():
    try:
        payload = build_health_services()
        return json_with_cache(payload, "no-store")
    except Exception:
        return JSONResponse(
            content={
                "now_utc": now_iso(),
                "thresholds": {"stale_s": 300, "down_s": 1800},
                "services": [],
                "error": "Failed to query service_heartbeats table",
            },
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )


@app.post("/api/v1/admin/health/heartbeat")
async def admin_health_heartbeat(request: Request):
    # Auth: shared secret via header
    secret = request.headers.get("X-Heartbeat-Secret", "")
    if not HEARTBEAT_INGEST_SECRET or secret != HEARTBEAT_INGEST_SECRET:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    service_name = (body.get("service_name") or "").strip()
    if not service_name or len(service_name) > 64:
        return JSONResponse(
            {"ok": False, "error": "service_name required (1-64 chars)"},
            status_code=400,
        )

    if service_name not in _ALLOWED_SERVICES:
        return JSONResponse(
            {"ok": False, "error": f"service '{service_name}' not in allowlist"},
            status_code=403,
        )

    meta = body.get("meta") if isinstance(body.get("meta"), dict) else None

    try:
        result = ingest_heartbeat(service_name, meta)
        return JSONResponse(result, status_code=200)
    except Exception:
        return JSONResponse(
            {"ok": False, "error": "DB upsert failed"},
            status_code=500,
        )


@app.get("/api/v1/admin/telemetry/overview")
def admin_telemetry_overview():
    try:
        payload = build_telemetry_overview()
        return json_with_cache(payload, "no-store")
    except Exception:
        return JSONResponse(
            content={
                "ok": False,
                "generated_at": now_iso(),
                "error": "Failed to build telemetry overview",
            },
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )


@app.get("/api/v1/admin/telemetry/users")
def admin_telemetry_users():
    try:
        payload = build_telemetry_users()
        return json_with_cache(payload, "no-store")
    except Exception:
        return JSONResponse(
            content={
                "ok": False,
                "generated_at": now_iso(),
                "error": "Failed to build users telemetry",
            },
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )


@app.get("/api/v1/admin/telemetry/invites")
def admin_telemetry_invites():
    try:
        payload = build_telemetry_invites()
        return json_with_cache(payload, "no-store")
    except Exception:
        return JSONResponse(
            content={
                "ok": False,
                "generated_at": now_iso(),
                "error": "Failed to build invites telemetry",
            },
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )


@app.get("/api/v1/admin/telemetry/alerts")
def admin_telemetry_alerts():
    try:
        payload = build_telemetry_alerts()
        return json_with_cache(payload, "no-store")
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to build alerts telemetry"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.get("/api/v1/admin/telemetry/paper")
def admin_telemetry_paper():
    try:
        payload = build_telemetry_paper()
        return json_with_cache(payload, "no-store")
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to build paper telemetry"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.get("/api/v1/admin/telemetry/data")
def admin_telemetry_data():
    try:
        payload = build_telemetry_data()
        return json_with_cache(payload, "no-store")
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to build data telemetry"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.get("/api/v1/admin/telemetry/scanners")
def admin_telemetry_scanners():
    try:
        payload = build_telemetry_scanners()
        return json_with_cache(payload, "no-store")
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to build scanners telemetry"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.get("/api/v1/admin/telemetry/ta_health")
def admin_telemetry_ta_health():
    try:
        payload = build_telemetry_ta_health()
        return json_with_cache(payload, "no-store")
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to build TA health"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.get("/api/v1/admin/telemetry/ta_relevance")
def admin_telemetry_ta_relevance(
    day: str | None = Query(None),
    horizon: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
):
    try:
        payload = build_telemetry_ta_relevance(day_str=day, horizon=horizon, limit=limit)
        return json_with_cache(payload, "no-store")
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to build TA relevance"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.get("/api/v1/admin/telemetry/pm_health")
def admin_telemetry_pm_health():
    try:
        payload = build_telemetry_pm_health()
        return json_with_cache(payload, "no-store")
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to build PM health"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.get("/api/v1/admin/telemetry/chartfeed_health")
def admin_telemetry_chartfeed_health():
    try:
        payload = build_telemetry_chartfeed()
        return json_with_cache(payload, "no-store")
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to build chartfeed health"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.get("/api/v1/admin/telemetry/health_summary")
def admin_telemetry_health_summary():
    try:
        payload = build_health_summary()
        return json_with_cache(payload, "no-store")
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to build health summary"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.get("/api/v1/admin/ops/backup_status")
def admin_ops_backup_status():
    try:
        payload = build_backup_status()
        return json_with_cache(payload, "no-store")
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to read backup status"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.get("/api/v1/admin/telemetry/summary")
def admin_telemetry_summary():
    payload = build_telemetry_summary()
    return JSONResponse(
        content=payload,
        status_code=501,
        headers={"Cache-Control": "no-store"},
    )


# ---- P5-ALERTS-003: Router alert settings (admin) ----

@app.get("/api/v1/admin/alerts/settings")
def admin_alerts_settings_list(user_id: str = Query(None)):
    try:
        if user_id:
            payload = build_alert_settings_get(user_id)
        else:
            payload = build_alert_settings_list()
        return json_with_cache(payload, "no-store")
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to read alert settings"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.post("/api/v1/admin/alerts/settings")
async def admin_alerts_settings_update(request: Request, user_id: str = Query(...)):
    try:
        body = await request.json()
        payload = build_alert_settings_update(user_id, body)
        status = 200 if payload.get("ok") else 400
        return JSONResponse(content=payload, status_code=status, headers={"Cache-Control": "no-store"})
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to update alert settings"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.get("/api/v1/admin/alerts/telemetry")
def admin_alerts_delivery_telemetry(day: str = Query(None)):
    try:
        payload = build_delivery_telemetry(day)
        return json_with_cache(payload, "no-store")
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to read delivery telemetry"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.get("/api/v1/admin/alerts/health")
def admin_alerts_health():
    try:
        payload = build_alert_health()
        return json_with_cache(payload, "no-store")
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to read alert health"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


# ---- AGENT-RELEVANCE-API-001 + AGENT-CACHE-001: Relevance endpoints ----
# Builders handle cache headers (ETag, Last-Modified, 304) internally.

@app.get("/api/v1/relevance/now")
def relevance_now(request: Request, day: str | None = Query(None)):
    try:
        return build_relevance_now(request, day=day)
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to read relevance data"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.get("/api/v1/relevance/presets")
def relevance_presets():
    try:
        return build_relevance_presets()
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to build presets"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.get("/api/v1/relevance/explain/{asset}")
def relevance_explain(
    request: Request,
    asset: str,
    horizon: str | None = Query(None),
    day: str | None = Query(None),
    family: str | None = Query(None),
    top_k: int | None = Query(None, ge=1, le=50),
):
    try:
        return build_relevance_explain(request, asset, horizon, day=day, family=family, top_k=top_k)
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to read relevance data"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.get("/api/v1/relevance/{asset}/preset/{preset_id}")
def relevance_preset_view(
    request: Request,
    asset: str,
    preset_id: str,
    horizon: str | None = Query(None),
    day: str | None = Query(None),
):
    try:
        return build_relevance_preset_view(request, asset, preset_id, horizon=horizon, day=day)
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to read relevance data"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.get("/api/v1/relevance/{asset}")
def relevance_asset(
    request: Request,
    asset: str,
    horizon: str | None = Query(None),
    day: str | None = Query(None),
):
    try:
        return build_relevance_asset(request, asset, horizon, day=day)
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to read relevance data"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


# ---- STRATEGY-TEMPLATES-001: Strategy templates ----

@app.get("/api/v1/strategy-templates")
def strategy_templates_list():
    try:
        return build_templates_list()
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to list templates"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.get("/api/v1/strategy-templates/{template_id}")
def strategy_templates_get(template_id: str):
    try:
        return build_template_get(template_id)
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to get template"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.post("/api/v1/strategy-templates/{template_id}/instantiate")
async def strategy_templates_instantiate(request: Request, template_id: str):
    try:
        return await build_template_instantiate(request, template_id)
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to instantiate template"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


# ---- SERVER-STRATEGIES-001: Strategy CRUD ----

@app.get("/api/v1/strategies")
def strategies_list(request: Request):
    try:
        return build_strategies_list(request)
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to list strategies"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.post("/api/v1/strategies/import")
async def strategies_import(request: Request):
    try:
        return await build_strategy_import(request)
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to import strategy"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.post("/api/v1/strategies")
async def strategies_create(request: Request):
    try:
        return await build_strategy_create(request)
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to create strategy"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.get("/api/v1/strategies/{strategy_id}/diff")
def strategies_diff(strategy_id: str):
    try:
        return build_strategy_diff(strategy_id)
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to compute strategy diff"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.get("/api/v1/strategies/{strategy_id}/export")
def strategies_export(strategy_id: str):
    try:
        return build_strategy_export(strategy_id)
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to export strategy"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.get("/api/v1/strategies/{strategy_id}/workspace_summary")
def strategies_workspace_summary(strategy_id: str):
    try:
        return build_workspace_summary(strategy_id)
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to build workspace summary"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.get("/api/v1/strategies/{strategy_id}")
def strategies_get(request: Request, strategy_id: str):
    try:
        return build_strategy_get(request, strategy_id)
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to get strategy"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.put("/api/v1/strategies/{strategy_id}")
async def strategies_update(request: Request, strategy_id: str):
    try:
        return await build_strategy_update(request, strategy_id)
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to update strategy"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.delete("/api/v1/strategies/{strategy_id}")
def strategies_delete(strategy_id: str):
    try:
        return build_strategy_delete(strategy_id)
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to delete strategy"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


# ---- SERVER-STRATEGIES-ALERTS-001: Strategy alert subscriptions ----

@app.get("/api/v1/strategy-alerts")
def strategy_alerts_list():
    try:
        return build_alert_list()
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to list strategy alerts"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.post("/api/v1/strategy-alerts")
async def strategy_alerts_create(request: Request):
    try:
        return await build_alert_create(request)
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to create strategy alert"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.get("/api/v1/strategy-alerts/{alert_id}/preview")
def strategy_alerts_preview(alert_id: str):
    try:
        return build_alert_preview(alert_id)
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to build alert preview"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.get("/api/v1/strategy-alerts/{alert_id}/history")
def strategy_alerts_history(alert_id: str, limit: int = Query(20, ge=1, le=100)):
    try:
        return build_alert_history(alert_id, limit=limit)
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to read alert history"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.get("/api/v1/strategy-alerts/{alert_id}")
def strategy_alerts_get(alert_id: str):
    try:
        return build_alert_get(alert_id)
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to get strategy alert"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.put("/api/v1/strategy-alerts/{alert_id}")
async def strategy_alerts_update(request: Request, alert_id: str):
    try:
        return await build_alert_update(request, alert_id)
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to update strategy alert"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.delete("/api/v1/strategy-alerts/{alert_id}")
def strategy_alerts_delete(alert_id: str):
    try:
        return build_alert_delete(alert_id)
    except Exception:
        return JSONResponse(
            content={"ok": False, "generated_at": now_iso(), "error": "Failed to delete strategy alert"},
            status_code=200, headers={"Cache-Control": "no-store"},
        )


@app.get("/api/v1/market/overview")
def market_overview():
    price_snap = get_snapshot("coingecko:price_simple:usd:bitcoin")
    funding_snap = get_snapshot("coinglass:oi_weighted_funding:BTC")
    oi_snap = get_snapshot("coinglass:open_interest:BTC")
    liq_snap = get_snapshot("coinglass:liquidations:BTC")
    global_snap = get_snapshot("coingecko:global")

    btc_price, btc_chg = (None, None)
    if price_snap:
        btc_price, btc_chg = extract_price(price_snap["payload"])

    funding_pct = extract_funding(funding_snap["payload"]) if funding_snap else None
    oi = extract_oi(oi_snap["payload"]) if oi_snap else {}
    liq = extract_liquidations(liq_snap["payload"]) if liq_snap else {}
    glob = extract_global(global_snap["payload"]) if global_snap else {}

    payload = {
        "ts": now_iso(),
        "kpis": [
            {
                "key": "btc_price",
                "label": "BTC Price",
                "value": fmt_usd(btc_price),
                "sub": f"{fmt_pct(btc_chg)} 24h" if btc_chg is not None else "—",
            },
            {
                "key": "funding_oiw",
                "label": "Funding (OI-weighted)",
                "value": fmt_pct(funding_pct, 4) if funding_pct is not None else "—",
                "sub": "BTC · Coinglass",
            },
            {
                "key": "open_interest",
                "label": "Open Interest",
                "value": fmt_usd(oi.get("oi_usd")),
                "sub": (
                    f"{fmt_pct(oi.get('oi_change_24h'))} 24h"
                    if oi.get("oi_change_24h") is not None
                    else "—"
                ),
            },
            {
                "key": "liq_24h",
                "label": "Liquidations (24h)",
                "value": fmt_usd(liq.get("total_usd")),
                "sub": (
                    f"{liq.get('long_pct', 0):.0f}% long / {liq.get('short_pct', 0):.0f}% short"
                    if liq.get("long_pct") is not None
                    else "—"
                ),
            },
        ],
        "global": {
            "btc_dominance": fmt_pct(glob.get("btc_dominance"), 1, signed=False),
            "total_mcap": fmt_usd(glob.get("total_mcap_usd")),
            "total_vol_24h": fmt_usd(glob.get("total_vol_usd")),
        },
        "sources": {
            "price": _src_ts(price_snap),
            "funding": _src_ts(funding_snap),
            "open_interest": _src_ts(oi_snap),
            "liquidations": _src_ts(liq_snap),
            "global": _src_ts(global_snap),
        },
    }

    return json_with_cache(payload, "public, s-maxage=20, stale-while-revalidate=120")


@app.get("/api/v1/assets/{symbol}/card")
def asset_card(symbol: str):
    sym = (symbol or "BTC").upper()
    if sym not in ("BTC", "ETH"):
        sym = "BTC"

    cg_id = "bitcoin" if sym == "BTC" else "ethereum"

    price_snap = get_snapshot(f"coingecko:price_simple:usd:{cg_id}")
    funding_snap = get_snapshot(f"coinglass:oi_weighted_funding:{sym}")
    oi_snap = get_snapshot(f"coinglass:open_interest:{sym}")
    liq_snap = get_snapshot(f"coinglass:liquidations:{sym}")
    global_snap = get_snapshot("coingecko:global")

    price, chg24 = (None, None)
    if price_snap:
        price, chg24 = extract_price(price_snap["payload"])

    funding_pct = extract_funding(funding_snap["payload"]) if funding_snap else None
    oi = extract_oi(oi_snap["payload"]) if oi_snap else {}
    liq = extract_liquidations(liq_snap["payload"]) if liq_snap else {}
    glob = extract_global(global_snap["payload"]) if global_snap else {}

    dom_key = "btc_dominance" if sym == "BTC" else "eth_dominance"

    payload = {
        "ts": now_iso(),
        "symbol": sym,
        "card": {
            "price": fmt_usd(price),
            "change_24h": fmt_pct(chg24),
            "dominance": fmt_pct(glob.get(dom_key), 1, signed=False),
            "vol_24h": fmt_usd(glob.get("total_vol_usd")),
            "funding": fmt_pct(funding_pct, 4) if funding_pct is not None else "—",
            "open_interest": fmt_usd(oi.get("oi_usd")),
            "liquidations_24h": fmt_usd(liq.get("total_usd")),
        },
        "sources": {
            "price": _src_ts(price_snap),
            "funding": _src_ts(funding_snap),
            "open_interest": _src_ts(oi_snap),
            "liquidations": _src_ts(liq_snap),
            "global": _src_ts(global_snap),
        },
    }

    return json_with_cache(payload, "public, s-maxage=20, stale-while-revalidate=120")


@app.get("/api/v1/sentiment/fear-greed")
def fear_greed():
    parsed, source_ts = get_fear_greed(max_age_seconds=300)
    if not parsed:
        parsed = {
            "ts": now_iso(),
            "current": {"value": 50, "label": "Neutral"},
            "history": [
                {"t": "D-6", "v": 46},
                {"t": "D-5", "v": 52},
                {"t": "D-4", "v": 58},
                {"t": "D-3", "v": 55},
                {"t": "D-2", "v": 49},
                {"t": "D-1", "v": 51},
                {"t": "Now", "v": 50},
            ],
            "source": {"provider": "fallback"},
        }
    if source_ts:
        parsed["source_ts"] = source_ts
    return json_with_cache(parsed, "public, s-maxage=60, stale-while-revalidate=600")


@app.get("/api/v1/edge/supercard")
def edge_supercard(symbol: str = Query("BTC")):
    try:
        payload = build_supercard(symbol)
        payload["ts"] = now_iso()
        return json_with_cache(payload, "public, s-maxage=20, stale-while-revalidate=300")
    except Exception:
        # Hard fallback to previous stable placeholder schema (never 500)
        sym = (symbol or "BTC").upper()
        if sym not in ("BTC", "ETH"):
            sym = "BTC"
        payload = {
            "ts": now_iso(),
            "symbol": sym,
            "version": "v0.1-placeholder",
            "summary": {"headline": "—", "stance": "—", "confidence": "—", "notes": ["—", "—", "—"]},
            "pillars": [
                {"key": "flow", "label": "Flow", "value": "—", "status": "neutral", "hint": "pressure proxy"},
                {"key": "leverage", "label": "Leverage", "value": "—", "status": "neutral", "hint": "OI + funding stress"},
                {"key": "fragility", "label": "Fragility", "value": "—", "status": "neutral", "hint": "liq imbalance + spikes"},
                {"key": "momentum", "label": "Momentum", "value": "—", "status": "neutral", "hint": "trend + volatility"},
                {"key": "sentiment", "label": "Sentiment", "value": "—", "status": "neutral", "hint": "fear/greed"},
                {"key": "risk", "label": "Risk", "value": "—", "status": "neutral", "hint": "regime + confidence"},
            ],
            "disclaimer": "Fallback placeholder. Upstream snapshots unavailable.",
        }
        return json_with_cache(payload, "public, s-maxage=20, stale-while-revalidate=300")


@app.get("/api/v1/edge/regime")
def edge_regime():
    try:
        payload = build_regime()
        payload["ts"] = now_iso()
        return json_with_cache(payload, "public, s-maxage=20, stale-while-revalidate=300")
    except Exception:
        payload = {
            "ts": now_iso(),
            "version": "v0.1-placeholder",
            "regime": {"label": "—", "confidence": "—", "since": None},
            "axes": [
                {"key": "trend", "label": "Trend", "value": "—"},
                {"key": "volatility", "label": "Volatility", "value": "—"},
                {"key": "leverage", "label": "Leverage", "value": "—"},
                {"key": "liquidity", "label": "Liquidity", "value": "—"},
            ],
            "drivers": ["—", "—", "—"],
            "disclaimer": "Fallback placeholder. Upstream snapshots unavailable.",
        }
        return json_with_cache(payload, "public, s-maxage=20, stale-while-revalidate=300")


@app.get("/api/v1/paper/summary")
def paper_summary():
    try:
        payload = build_paper_summary()
        return json_with_cache(payload, "public, s-maxage=15, stale-while-revalidate=120")
    except Exception:
        payload = {
            "ts": now_iso(),
            "version": "v0.1-placeholder",
            "accounts": {"active": 0, "tracked": 0},
            "kpis": {"equity_30d": "—", "win_rate": "—", "max_drawdown": "—", "active_positions": "—"},
            "sample": {"name": "—", "equity_curve": []},
            "disclaimer": "Fallback placeholder. Paper tables unavailable.",
        }
        return json_with_cache(payload, "public, s-maxage=15, stale-while-revalidate=120")


@app.get("/api/v1/simlab/overview")
def simlab_overview(days: int = 30):
    try:
        import os
        tg_id = int(os.environ.get("SIMLAB_ADMIN_TG_ID", "0") or "0")
        payload = build_simlab_overview(tg_id, days=days)
        return json_with_cache(payload, "public, s-maxage=10, stale-while-revalidate=60")
    except Exception:
        payload = {
            "ts": now_iso(),
            "version": "v0.1",
            "admin": {"tg_id": 0, "accounts": {"total": 0, "active": 0}},
            "kpis": {"pnl_30d_usdt": "\u2014", "win_rate": "\u2014", "trades_30d": 0, "open_positions": 0, "max_drawdown": "\u2014"},
            "curve": [],
            "per_account": [],
            "disclaimer": "Fallback: simlab overview unavailable.",
        }
        return json_with_cache(payload, "public, s-maxage=10, stale-while-revalidate=60")


@app.get("/api/v1/simlab/trades/live")
def simlab_trades_live(limit: int = 50):
    try:
        import os
        tg_id = int(os.environ.get("SIMLAB_ADMIN_TG_ID", "0") or "0")
        payload = build_simlab_trades_live(tg_id, limit=limit)
        return json_with_cache(payload, "public, s-maxage=5, stale-while-revalidate=30")
    except Exception:
        payload = {"ts": now_iso(), "version": "v0.1", "admin": {"tg_id": 0}, "items": [], "disclaimer": "Fallback: trades feed unavailable."}
        return json_with_cache(payload, "public, s-maxage=5, stale-while-revalidate=30")


@app.get("/api/v1/alerts/live")
def alerts_live(limit: int = 50):
    try:
        payload = build_alerts_live(limit=limit)
        return json_with_cache(payload, "public, s-maxage=5, stale-while-revalidate=30")
    except Exception:
        payload = {"ok": True, "version": "v0.1-live", "source_ts": now_iso(), "items": []}
        return json_with_cache(payload, "public, s-maxage=5, stale-while-revalidate=30")
