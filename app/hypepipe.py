"""HypePipe skeleton endpoints (HP-001).

Provides:
  GET  /api/v1/hypepipe/health  — liveness probe (no auth)
  POST /api/v1/hypepipe/cap     — capability dispatch (auth required)

Auth (v1 minimal):
  - X-Agent-Id header required
  - Authorization: Bearer <token> required (any non-empty token accepted)

Audit:
  - Every /cap call logs to hypepipe_audit_events table.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .db import get_conn
from .snapshots import get_snapshot

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/hypepipe", tags=["hypepipe"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class CapContext(BaseModel):
    agent_id: str
    user_id: Optional[int] = None
    tier: Optional[str] = None


class CapOpts(BaseModel):
    freshness_s: Optional[int] = None
    trace: Optional[bool] = None


class CapRequest(BaseModel):
    cap: str
    input: Dict[str, Any] = Field(default_factory=dict)
    ctx: CapContext
    opts: CapOpts = Field(default_factory=CapOpts)
    request_id: str = Field(default_factory=lambda: uuid.uuid4().hex)


class CapMeta(BaseModel):
    cap: str
    trace_id: str
    asof: Optional[str] = None
    cache_hit: Optional[bool] = None


class CapResponse(BaseModel):
    ok: bool
    data: Any = None
    meta: CapMeta


# ---------------------------------------------------------------------------
# Auth dependency (minimal v1)
# ---------------------------------------------------------------------------

def _check_auth(
    x_agent_id: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
) -> str:
    """Validate X-Agent-Id + Bearer token. Returns agent_id on success."""
    if not x_agent_id:
        raise HTTPException(status_code=401, detail="Missing X-Agent-Id header")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization Bearer token")
    token = authorization[len("Bearer "):]
    if not token.strip():
        raise HTTPException(status_code=401, detail="Empty bearer token")
    # HP-001: accept any non-empty token (real JWT verify in HP-002)
    return x_agent_id


# ---------------------------------------------------------------------------
# Audit table + logging
# ---------------------------------------------------------------------------

_TABLE_ENSURED = False

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS hypepipe_audit_events (
    id           BIGSERIAL PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    agent_id     TEXT NOT NULL,
    user_id      BIGINT,
    cap          TEXT NOT NULL,
    request_id   TEXT NOT NULL,
    trace_id     TEXT NOT NULL,
    decision     TEXT NOT NULL,
    latency_ms   INTEGER
);
"""


def _ensure_audit_table() -> None:
    global _TABLE_ENSURED
    if _TABLE_ENSURED:
        return
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(_CREATE_TABLE_SQL)
            conn.commit()
        _TABLE_ENSURED = True
    except Exception:
        logger.warning("Failed to ensure hypepipe_audit_events table", exc_info=True)


def _log_audit(
    agent_id: str,
    user_id: Optional[int],
    cap: str,
    request_id: str,
    trace_id: str,
    decision: str,
    latency_ms: Optional[int] = None,
) -> None:
    try:
        _ensure_audit_table()
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO hypepipe_audit_events
                       (agent_id, user_id, cap, request_id, trace_id, decision, latency_ms)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (agent_id, user_id, cap, request_id, trace_id, decision, latency_ms),
                )
            conn.commit()
    except Exception:
        logger.warning("Failed to log audit event", exc_info=True)


# ---------------------------------------------------------------------------
# Capability dispatch
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dispatch_core_asset_snapshot(input_data: Dict[str, Any]) -> Dict[str, Any]:
    """Attempt to read a real snapshot; fall back to stub."""
    asset = str(input_data.get("asset", "BTC")).upper()
    cg_id_map = {"BTC": "bitcoin", "ETH": "ethereum"}
    cg_id = cg_id_map.get(asset)

    if cg_id:
        snap = get_snapshot(f"coingecko:price_simple:usd:{cg_id}")
        if snap:
            payload = snap["payload"]
            data = payload.get("data", {})
            return {
                "asset": asset,
                "price": data.get("price"),
                "change_24h": data.get("change_24h"),
                "asof": snap["updated_at"].isoformat() if snap.get("updated_at") else _now_iso(),
            }

    # Stub fallback
    return {"asset": asset, "note": "stub", "asof": _now_iso()}


CAPABILITY_HANDLERS = {
    "core.asset.snapshot": _dispatch_core_asset_snapshot,
}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/health")
def hypepipe_health():
    return JSONResponse(
        content={"ok": True, "service": "hypepipe", "ts": _now_iso()},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/cap")
def hypepipe_cap(
    body: CapRequest,
    request: Request,
    x_agent_id: str | None = Header(None),
    authorization: str | None = Header(None),
):
    trace_id = uuid.uuid4().hex
    t0 = time.monotonic()

    # Auth check
    try:
        agent_id = _check_auth(x_agent_id, authorization)
    except HTTPException as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        _log_audit(
            agent_id=x_agent_id or "unknown",
            user_id=body.ctx.user_id,
            cap=body.cap,
            request_id=body.request_id,
            trace_id=trace_id,
            decision="deny",
            latency_ms=latency_ms,
        )
        raise exc

    # Capability dispatch
    handler = CAPABILITY_HANDLERS.get(body.cap)
    if handler is None:
        latency_ms = int((time.monotonic() - t0) * 1000)
        _log_audit(
            agent_id=agent_id,
            user_id=body.ctx.user_id,
            cap=body.cap,
            request_id=body.request_id,
            trace_id=trace_id,
            decision="unknown_cap",
            latency_ms=latency_ms,
        )
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": f"Unknown capability: {body.cap}",
                "known_caps": sorted(CAPABILITY_HANDLERS.keys()),
                "meta": {"cap": body.cap, "trace_id": trace_id, "asof": None, "cache_hit": None},
            },
        )

    try:
        result = handler(body.input)
    except Exception:
        logger.exception("Capability handler failed: cap=%s", body.cap)
        latency_ms = int((time.monotonic() - t0) * 1000)
        _log_audit(
            agent_id=agent_id,
            user_id=body.ctx.user_id,
            cap=body.cap,
            request_id=body.request_id,
            trace_id=trace_id,
            decision="error",
            latency_ms=latency_ms,
        )
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": "Internal capability error",
                "meta": {"cap": body.cap, "trace_id": trace_id, "asof": None, "cache_hit": None},
            },
        )

    latency_ms = int((time.monotonic() - t0) * 1000)
    _log_audit(
        agent_id=agent_id,
        user_id=body.ctx.user_id,
        cap=body.cap,
        request_id=body.request_id,
        trace_id=trace_id,
        decision="allow",
        latency_ms=latency_ms,
    )

    asof = result.get("asof") if isinstance(result, dict) else None

    return JSONResponse(
        content={
            "ok": True,
            "data": result,
            "meta": {"cap": body.cap, "trace_id": trace_id, "asof": asof, "cache_hit": None},
        },
        headers={"Cache-Control": "no-store"},
    )
