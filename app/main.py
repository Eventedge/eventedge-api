from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse


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
    payload = {
        "ts": now_iso(),
        "kpis": [
            {"key": "btc_price", "label": "BTC Price", "value": "$—", "sub": "wire price snapshot"},
            {"key": "funding_oiw", "label": "Funding (OI-weighted)", "value": "—", "sub": "8h / 24h toggle"},
            {"key": "open_interest", "label": "Open Interest", "value": "—", "sub": "per exchange + total"},
            {"key": "liq_24h", "label": "Liquidations (24h)", "value": "—", "sub": "long/short breakdown"},
        ],
    }
    return json_with_cache(payload, "public, s-maxage=30, stale-while-revalidate=300")


@app.get("/api/v1/assets/{symbol}/card")
def asset_card(symbol: str):
    sym = (symbol or "BTC").upper()
    payload = {
        "ts": now_iso(),
        "symbol": sym,
        "card": {
            "price": "—",
            "change_24h": "—",
            "dominance": "—",
            "vol_24h": "—",
            "funding": "—",
            "open_interest": "—",
            "liquidations_24h": "—",
        },
    }
    return json_with_cache(payload, "public, s-maxage=20, stale-while-revalidate=300")


@app.get("/api/v1/sentiment/fear-greed")
def fear_greed():
    payload = {
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
    }
    return json_with_cache(payload, "public, s-maxage=60, stale-while-revalidate=600")
