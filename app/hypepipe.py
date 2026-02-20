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
from .regime import build_regime
from .snapshots import (
    extract_funding,
    extract_liquidations,
    extract_price,
    fmt_pct,
    fmt_usd,
    get_snapshot,
)

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
    "macro.regime.snapshot": "read:macro.regime.snapshot",
    "macro.pillars.status": "read:macro.pillars.status",
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


def _dispatch_macro_regime_snapshot(input_data: Dict[str, Any]) -> Dict[str, Any]:
    """Derive macro regime from live EdgeCore snapshots via build_regime().

    Heuristic (v1, deterministic):
      - build_regime() reads BTC price, funding, OI, liquidations, Fear & Greed
        from edge_dataset_registry and classifies into buckets.
      - We map its axes/regime label into the HypePipe response shape.

    Regime mapping (build_regime label -> HypePipe regime):
      Risk-On  -> risk_on
      Risk-Off -> risk_off
      Trend    -> neutral   (directional but not risk-classified)
      Chop     -> neutral
    """
    regime_data = build_regime()
    regime_raw = regime_data.get("regime", {})
    label = regime_raw.get("label", "Chop")
    axes = {a["key"]: a["value"] for a in regime_data.get("axes", [])}

    # Map composite regime label
    regime_map = {"Risk-On": "risk_on", "Risk-Off": "risk_off"}
    regime = regime_map.get(label, "neutral")

    # Vol regime from volatility axis
    vol_map = {"Calm": "low", "Chop": "mid", "Shock": "high"}
    vol_regime = vol_map.get(axes.get("volatility", ""), "mid")

    # Liquidity regime from liquidity axis
    liq_map = {"Loose": "loose", "Normal": "neutral", "Tight": "tight"}
    liquidity_regime = liq_map.get(axes.get("liquidity", ""), "neutral")

    notes = regime_data.get("drivers", [])
    asof = _now_iso()

    return {
        "regime": regime,
        "vol_regime": vol_regime,
        "liquidity_regime": liquidity_regime,
        "notes": notes,
        "asof": asof,
    }


def _dispatch_macro_pillars_status(input_data: Dict[str, Any]) -> Dict[str, Any]:
    """Macro pillars status — deterministic proxy from live snapshots.

    Five pillars: rates, usd, liquidity, risk, crypto.
    Returns "unknown" for pillars without a live data source (rates, usd).
    """
    # Fetch available snapshots
    price_snap = get_snapshot("coingecko:price_simple:usd:bitcoin")
    funding_snap = get_snapshot("coinglass:oi_weighted_funding:BTC")
    liq_snap = get_snapshot("coinglass:liquidations:BTC")

    price, chg24 = extract_price(price_snap["payload"]) if price_snap else (None, None)
    funding_pct = extract_funding(funding_snap["payload"]) if funding_snap else None
    liq = extract_liquidations(liq_snap["payload"]) if liq_snap else {}
    liq_total = liq.get("total_usd")
    liq_long_pct = liq.get("long_pct")

    # ---- rates pillar: no data source yet ----
    rates = {
        "key": "rates",
        "label": "Rates",
        "status": "unknown",
        "value": None,
        "delta": None,
        "note": "No rates data source available",
    }

    # ---- usd pillar: no data source yet ----
    usd = {
        "key": "usd",
        "label": "USD",
        "status": "unknown",
        "value": None,
        "delta": None,
        "note": "No USD index data source available",
    }

    # ---- liquidity pillar: liq skew + vol proxy ----
    if liq_total is not None and liq_long_pct is not None:
        if liq_long_pct >= 70:
            liq_status = "red"
            liq_note = "Fragile — heavy long liquidations"
        elif liq_long_pct <= 40:
            liq_status = "green"
            liq_note = "Healthy — balanced or short-heavy liqs"
        else:
            liq_status = "yellow"
            liq_note = "Moderate liq skew"
        liquidity = {
            "key": "liquidity",
            "label": "Liquidity",
            "status": liq_status,
            "value": fmt_usd(liq_total),
            "delta": f"{fmt_pct(liq_long_pct, 0, signed=False)} long",
            "note": liq_note,
        }
    else:
        liquidity = {
            "key": "liquidity",
            "label": "Liquidity",
            "status": "unknown",
            "value": None,
            "delta": None,
            "note": "Liquidation data unavailable",
        }

    # ---- risk pillar: BTC 24h change as risk-on/off proxy ----
    if chg24 is not None:
        if chg24 >= 2.0:
            risk_status = "green"
            risk_note = "Risk-on — strong BTC momentum"
        elif chg24 <= -2.0:
            risk_status = "red"
            risk_note = "Risk-off — significant BTC drawdown"
        elif chg24 <= -0.5:
            risk_status = "yellow"
            risk_note = "Mild risk-off tone"
        else:
            risk_status = "yellow"
            risk_note = "Neutral risk posture"
        risk = {
            "key": "risk",
            "label": "Risk",
            "status": risk_status,
            "value": fmt_usd(price) if price else None,
            "delta": fmt_pct(chg24),
            "note": risk_note,
        }
    else:
        risk = {
            "key": "risk",
            "label": "Risk",
            "status": "unknown",
            "value": None,
            "delta": None,
            "note": "BTC price data unavailable",
        }

    # ---- crypto pillar: funding + liq skew as crowding/fragility proxy ----
    if funding_pct is not None:
        if funding_pct >= 0.10:
            crypto_status = "red"
            crypto_note = "Crowded longs — elevated funding"
        elif funding_pct <= -0.02:
            crypto_status = "green"
            crypto_note = "Shorts dominant — contrarian bullish"
        else:
            crypto_status = "yellow"
            crypto_note = "Neutral funding"
        crypto = {
            "key": "crypto",
            "label": "Crypto",
            "status": crypto_status,
            "value": fmt_pct(funding_pct, 3),
            "delta": f"{fmt_pct(liq_long_pct, 0, signed=False)} long liqs" if liq_long_pct is not None else None,
            "note": crypto_note,
        }
    else:
        crypto = {
            "key": "crypto",
            "label": "Crypto",
            "status": "unknown",
            "value": None,
            "delta": None,
            "note": "Funding data unavailable",
        }

    pillars = [rates, usd, liquidity, risk, crypto]

    # ---- summary: deterministic 1-2 line text ----
    known = [p for p in pillars if p["status"] != "unknown"]
    red_count = sum(1 for p in known if p["status"] == "red")
    green_count = sum(1 for p in known if p["status"] == "green")

    if not known:
        summary = "Insufficient data — all pillars unknown."
    elif red_count >= 2:
        summary = f"{red_count} pillars flagged red — macro headwinds."
    elif green_count >= 2:
        summary = f"{green_count} pillars green — macro tailwinds."
    else:
        summary = "Mixed signals — no dominant macro bias."

    asof = _now_iso()
    return {
        "pillars": pillars,
        "summary": summary,
        "asof": asof,
    }


CAPABILITY_HANDLERS = {
    "core.asset.snapshot": _dispatch_core_asset_snapshot,
    "macro.regime.snapshot": _dispatch_macro_regime_snapshot,
    "macro.pillars.status": _dispatch_macro_pillars_status,
}

# Default TTL per cap (seconds).  Caps not listed here are not cached.
CAP_DEFAULT_TTL: Dict[str, int] = {
    "core.asset.snapshot": 30,
    "macro.regime.snapshot": 120,
    "macro.pillars.status": 300,
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
