from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .fear_greed import get_fear_greed
from .paper import build_paper_summary
from .regime import build_regime
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
        "http://localhost:3000",
    ],
    allow_credentials=False,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/api/v1/health")
def health():
    payload = {"ok": True, "service": "eventedge-api", "api": "v1", "ts": now_iso()}
    return json_with_cache(payload, "public, max-age=5")


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
