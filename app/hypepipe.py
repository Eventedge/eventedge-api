"""HypePipe endpoints (HP-001 / HP-002 / HP-003).

Provides:
  GET  /api/v1/hypepipe/health  — liveness probe (no auth)
  POST /api/v1/hypepipe/cap     — capability dispatch (auth required)

Auth (HP-002 — JWT HS256):
  - Env: HYPEPIPE_JWT_SECRET  (required; 500 if missing)
  - Authorization: Bearer <jwt> with claims:
      agent_id (str), scopes ([str]), tier (str), exp (int)
      policy_version (str, optional)
  - X-Agent-Id header must match JWT agent_id claim
  - Scopes enforced per capability (e.g. read:core.asset.snapshot)

Cache (HP-003 — in-memory TTL):
  - Read caps are cached by (cap, normalized_input).
  - Default TTL 30s.  Override via opts.freshness_s (clamped to TTL).
  - Disable entirely: HYPEPIPE_CACHE_DISABLE=1

Audit:
  - Every /cap call logs to hypepipe_audit_events table.
  - Columns: policy_version, deny_reason, asof, cache_hit.

Deny reason codes:
  missing_header | missing_token | invalid_token | expired
  agent_mismatch | scope_denied  | unknown_cap

Dev token helper (ops-only, NOT an endpoint):
  python3 -c "
  import jwt, os, time
  s = os.environ['HYPEPIPE_JWT_SECRET']
  t = jwt.encode({
      'agent_id': 'edgenavigator-v1',
      'scopes': ['read:core.asset.snapshot'],
      'tier': 'readonly',
      'exp': int(time.time()) + 3600,
      'policy_version': 'v1',
  }, s, algorithm='HS256')
  print(t)
  "
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import jwt
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
    agent_id: Optional[str] = None
    user_id: Optional[int] = None
    tier: Optional[str] = None


class CapOpts(BaseModel):
    freshness_s: Optional[int] = None
    trace: Optional[bool] = None


class CapRequest(BaseModel):
    cap: str
    input: Dict[str, Any] = Field(default_factory=dict)
    ctx: CapContext = Field(default_factory=CapContext)
    opts: CapOpts = Field(default_factory=CapOpts)
    request_id: str


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
# Scope map: capability -> required scope
# ---------------------------------------------------------------------------

CAP_REQUIRED_SCOPE: Dict[str, str] = {
    "core.asset.snapshot": "read:core.asset.snapshot",
}


# ---------------------------------------------------------------------------
# Auth (HP-002 — JWT HS256)
# ---------------------------------------------------------------------------

class AuthResult:
    """Holds verified JWT claims after successful auth."""

    __slots__ = ("agent_id", "scopes", "tier", "policy_version")

    def __init__(self, agent_id: str, scopes: List[str], tier: str, policy_version: Optional[str]):
        self.agent_id = agent_id
        self.scopes = scopes
        self.tier = tier
        self.policy_version = policy_version


_JWT_SECRET_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".hypepipe_jwt_secret")


def _get_jwt_secret() -> str:
    # 1. Env var takes precedence
    secret = os.environ.get("HYPEPIPE_JWT_SECRET", "")
    # 2. Fall back to secret file (useful when systemd env can't be edited)
    if not secret:
        try:
            with open(_JWT_SECRET_FILE) as f:
                secret = f.read().strip()
        except FileNotFoundError:
            pass
    if not secret:
        logger.error("HYPEPIPE_JWT_SECRET is not set and %s not found", _JWT_SECRET_FILE)
        raise HTTPException(status_code=500, detail="Server auth configuration missing")
    return secret


def _check_auth(
    x_agent_id: Optional[str],
    authorization: Optional[str],
) -> AuthResult:
    """Verify JWT and match X-Agent-Id. Returns AuthResult on success."""
    if not x_agent_id:
        raise HTTPException(status_code=401, detail="missing_header")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing_token")

    token = authorization[len("Bearer "):]
    if not token.strip():
        raise HTTPException(status_code=401, detail="missing_token")

    secret = _get_jwt_secret()

    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["exp", "agent_id", "scopes", "tier"]})
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="invalid_token")

    jwt_agent_id = claims.get("agent_id", "")
    if jwt_agent_id != x_agent_id:
        raise HTTPException(status_code=401, detail="agent_mismatch")

    scopes = claims.get("scopes", [])
    if not isinstance(scopes, list):
        raise HTTPException(status_code=401, detail="invalid_token")

    tier = claims.get("tier", "")
    if tier not in ("readonly", "paper", "orchestrator"):
        raise HTTPException(status_code=401, detail="invalid_token")

    return AuthResult(
        agent_id=jwt_agent_id,
        scopes=scopes,
        tier=tier,
        policy_version=claims.get("policy_version"),
    )


def _check_scope(auth: AuthResult, cap: str) -> Optional[str]:
    """Return deny_reason code if scope check fails, else None."""
    required = CAP_REQUIRED_SCOPE.get(cap)
    if required and required not in auth.scopes:
        return "scope_denied"
    return None


# ---------------------------------------------------------------------------
# Audit table + logging
# ---------------------------------------------------------------------------

_TABLE_ENSURED = False

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS hypepipe_audit_events (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    agent_id        TEXT NOT NULL,
    user_id         BIGINT,
    cap             TEXT NOT NULL,
    request_id      TEXT NOT NULL,
    trace_id        TEXT NOT NULL,
    decision        TEXT NOT NULL,
    latency_ms      INTEGER,
    policy_version  TEXT,
    deny_reason     TEXT,
    asof            TEXT,
    cache_hit       BOOLEAN
);
"""

_MIGRATE_COLUMNS_SQL = [
    "ALTER TABLE hypepipe_audit_events ADD COLUMN IF NOT EXISTS policy_version TEXT",
    "ALTER TABLE hypepipe_audit_events ADD COLUMN IF NOT EXISTS deny_reason TEXT",
    "ALTER TABLE hypepipe_audit_events ADD COLUMN IF NOT EXISTS asof TEXT",
    "ALTER TABLE hypepipe_audit_events ADD COLUMN IF NOT EXISTS cache_hit BOOLEAN",
]


def _ensure_audit_table() -> None:
    global _TABLE_ENSURED
    if _TABLE_ENSURED:
        return
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(_CREATE_TABLE_SQL)
                for stmt in _MIGRATE_COLUMNS_SQL:
                    cur.execute(stmt)
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
    policy_version: Optional[str] = None,
    deny_reason: Optional[str] = None,
    asof: Optional[str] = None,
    cache_hit: Optional[bool] = None,
) -> None:
    try:
        _ensure_audit_table()
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO hypepipe_audit_events
                       (agent_id, user_id, cap, request_id, trace_id, decision,
                        latency_ms, policy_version, deny_reason, asof, cache_hit)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (agent_id, user_id, cap, request_id, trace_id, decision,
                     latency_ms, policy_version, deny_reason, asof, cache_hit),
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

# Default TTL per cap (seconds).  Caps not listed here are not cached.
CAP_DEFAULT_TTL: Dict[str, int] = {
    "core.asset.snapshot": 30,
}


# ---------------------------------------------------------------------------
# In-memory TTL cache (HP-003)
# ---------------------------------------------------------------------------

_CACHE_DISABLED = os.environ.get("HYPEPIPE_CACHE_DISABLE", "") == "1"

# {cache_key: (data_dict, asof_str, cached_at_monotonic)}
_cap_cache: Dict[str, Tuple[Dict[str, Any], str, float]] = {}
_cache_lock = threading.Lock()


def _cache_key(cap: str, input_data: Dict[str, Any]) -> str:
    """Deterministic key from cap + sorted input."""
    raw = json.dumps({"cap": cap, "input": input_data}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _cache_get(key: str, max_age: float) -> Optional[Tuple[Dict[str, Any], str]]:
    """Return (data, asof) if cached and fresh enough, else None."""
    with _cache_lock:
        entry = _cap_cache.get(key)
        if entry is None:
            return None
        data, asof, cached_at = entry
        if (time.monotonic() - cached_at) > max_age:
            return None
        return data, asof


def _cache_put(key: str, data: Dict[str, Any], asof: str) -> None:
    with _cache_lock:
        _cap_cache[key] = (data, asof, time.monotonic())


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
    policy_version: Optional[str] = None

    # --- request_id validation ---
    if not body.request_id or not body.request_id.strip():
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": "request_id is required and must be non-empty",
                "meta": {"cap": body.cap, "trace_id": trace_id, "asof": None, "cache_hit": None},
            },
        )

    # --- Auth check (JWT) ---
    try:
        auth = _check_auth(x_agent_id, authorization)
        policy_version = auth.policy_version
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
            deny_reason=exc.detail,
        )
        raise exc

    # --- Scope enforcement ---
    deny_reason = _check_scope(auth, body.cap)
    if deny_reason:
        latency_ms = int((time.monotonic() - t0) * 1000)
        _log_audit(
            agent_id=auth.agent_id,
            user_id=body.ctx.user_id,
            cap=body.cap,
            request_id=body.request_id,
            trace_id=trace_id,
            decision="deny",
            latency_ms=latency_ms,
            policy_version=policy_version,
            deny_reason=deny_reason,
        )
        return JSONResponse(
            status_code=403,
            content={
                "ok": False,
                "error": deny_reason,
                "meta": {"cap": body.cap, "trace_id": trace_id, "asof": None, "cache_hit": None},
            },
        )

    # --- Unknown cap ---
    handler = CAPABILITY_HANDLERS.get(body.cap)
    if handler is None:
        latency_ms = int((time.monotonic() - t0) * 1000)
        _log_audit(
            agent_id=auth.agent_id,
            user_id=body.ctx.user_id,
            cap=body.cap,
            request_id=body.request_id,
            trace_id=trace_id,
            decision="deny",
            latency_ms=latency_ms,
            policy_version=policy_version,
            deny_reason="unknown_cap",
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

    # --- Cache lookup ---
    default_ttl = CAP_DEFAULT_TTL.get(body.cap, 0)
    cache_hit: Optional[bool] = None
    result: Optional[Dict[str, Any]] = None
    asof: Optional[str] = None

    if default_ttl > 0 and not _CACHE_DISABLED:
        freshness = body.opts.freshness_s
        if freshness is not None:
            max_age = min(max(freshness, 0), default_ttl)
        else:
            max_age = default_ttl

        ck = _cache_key(body.cap, body.input)

        if max_age > 0:
            cached = _cache_get(ck, float(max_age))
            if cached is not None:
                result, asof = cached
                cache_hit = True

    # --- Dispatch (on miss) ---
    if result is None:
        cache_hit = False
        try:
            result = handler(body.input)
        except Exception:
            logger.exception("Capability handler failed: cap=%s", body.cap)
            latency_ms = int((time.monotonic() - t0) * 1000)
            _log_audit(
                agent_id=auth.agent_id,
                user_id=body.ctx.user_id,
                cap=body.cap,
                request_id=body.request_id,
                trace_id=trace_id,
                decision="error",
                latency_ms=latency_ms,
                policy_version=policy_version,
            )
            return JSONResponse(
                status_code=500,
                content={
                    "ok": False,
                    "error": "Internal capability error",
                    "meta": {"cap": body.cap, "trace_id": trace_id, "asof": None, "cache_hit": False},
                },
            )

        asof = result.get("asof") if isinstance(result, dict) else None

        # Store in cache
        if default_ttl > 0 and not _CACHE_DISABLED and asof:
            _cache_put(_cache_key(body.cap, body.input), result, asof)

    # --- Success audit + response ---
    latency_ms = int((time.monotonic() - t0) * 1000)
    _log_audit(
        agent_id=auth.agent_id,
        user_id=body.ctx.user_id,
        cap=body.cap,
        request_id=body.request_id,
        trace_id=trace_id,
        decision="allow",
        latency_ms=latency_ms,
        policy_version=policy_version,
        asof=asof,
        cache_hit=cache_hit,
    )

    return JSONResponse(
        content={
            "ok": True,
            "data": result,
            "meta": {"cap": body.cap, "trace_id": trace_id, "asof": asof, "cache_hit": cache_hit},
        },
        headers={"Cache-Control": "no-store"},
    )
