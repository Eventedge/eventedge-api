"""Workstation watch condition evaluation — server-side background engine.

Bundle 63: Evaluates active watch conditions against live market data
and updates triggered status in the file-backed condition store.

Supported condition types:
  - price_move: Binance futures price vs threshold (above/below/move/percent)
  - signal_change: feature appears/disappears in top relevance signals
  - regime_shift: regime label matches target or changed from baseline

Called via:
  POST /api/v1/workstation/watch-conditions/evaluate
  Triggered by cron (* * * * *) and optionally by client.

Data sources (all local):
  - Binance futures API for price (direct)
  - /api/v1/relevance/now for signals (local HTTP)
  - /api/v1/edge/regime for regime (local HTTP)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi.responses import JSONResponse

logger = logging.getLogger("workstation.watch_evaluate")

ALERTS_DIR = Path(os.getenv("ROUTER_ALERT_DIR", "/home/eventedge/alerts"))
CONDITIONS_FILE = ALERTS_DIR / "workstation_watch_conditions.json"
STORE_VERSION = 1
MAX_CONDITIONS = 30

API_BASE = os.getenv("EVENTEDGE_API_URL", "http://localhost:8080")
BINANCE_FUTURES = "https://fapi.binance.com/fapi/v1/ticker/24hr"

BINANCE_SYMBOLS: dict[str, str] = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
    "HYPE": "HYPEUSDT", "BNB": "BNBUSDT", "XRP": "XRPUSDT", "DOGE": "DOGEUSDT",
    "AVAX": "AVAXUSDT", "LINK": "LINKUSDT", "ADA": "ADAUSDT", "SUI": "SUIUSDT",
    "ARB": "ARBUSDT", "OP": "OPUSDT", "PEPE": "PEPEUSDT",
    "WIF": "WIFUSDT", "NEAR": "NEARUSDT", "AAVE": "AAVEUSDT", "UNI": "UNIUSDT",
    "INJ": "INJUSDT", "TIA": "TIAUSDT", "SEI": "SEIUSDT",
}

TIMEOUT = 6.0  # seconds


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── File I/O ──

def _load_conditions() -> dict[str, Any]:
    if not CONDITIONS_FILE.exists():
        return {"version": STORE_VERSION, "conditions": [], "updatedAt": _now_iso()}
    try:
        return json.loads(CONDITIONS_FILE.read_text())
    except Exception:
        return {"version": STORE_VERSION, "conditions": [], "updatedAt": _now_iso()}


def _save_conditions(data: dict[str, Any]) -> None:
    import tempfile as _tf
    data["updatedAt"] = _now_iso()
    conditions = data.get("conditions", [])
    if len(conditions) > MAX_CONDITIONS:
        data["conditions"] = conditions[:MAX_CONDITIONS]
    raw = json.dumps(data, indent=2, ensure_ascii=False)
    fd, tmp = _tf.mkstemp(dir=str(ALERTS_DIR), suffix=".tmp")
    try:
        os.write(fd, raw.encode())
        os.close(fd)
        os.rename(tmp, str(CONDITIONS_FILE))
    except Exception:
        os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── Data Fetchers ──

async def _fetch_price(client: httpx.AsyncClient, asset: str) -> float | None:
    """Fetch current price from Binance futures."""
    symbol = BINANCE_SYMBOLS.get(asset)
    if not symbol:
        return None
    try:
        r = await client.get(BINANCE_FUTURES, params={"symbol": symbol}, timeout=TIMEOUT)
        if r.status_code == 200:
            return float(r.json().get("lastPrice", 0))
    except Exception:
        pass
    return None


async def _fetch_regime(client: httpx.AsyncClient) -> dict[str, str] | None:
    """Fetch current regime label from local API."""
    try:
        r = await client.get(f"{API_BASE}/api/v1/edge/regime", timeout=TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            return {
                "label": data.get("regime", {}).get("label", "unknown"),
                "confidence": data.get("regime", {}).get("confidence", "low"),
            }
    except Exception:
        pass
    return None


async def _fetch_signals(client: httpx.AsyncClient, asset: str) -> list[dict] | None:
    """Fetch top relevance signals for an asset from local API."""
    try:
        r = await client.get(f"{API_BASE}/api/v1/relevance/now", timeout=8.0)
        if r.status_code == 200:
            data = r.json()
            return data.get("assets", {}).get(asset, {}).get("horizons", {}).get("24h", {}).get("top", [])
    except Exception:
        pass
    return None


# ── Evaluators ──

def _evaluate_price_move(cond: dict, current_price: float) -> dict | None:
    """Evaluate price_move condition. Returns trigger metadata or None."""
    details = cond.get("details", {})
    direction = details.get("direction", "move")
    price_threshold = details.get("priceThreshold")
    percent_threshold = details.get("percentThreshold")
    reference_price = cond.get("priceAtCreation")

    # Absolute price threshold
    if price_threshold is not None and price_threshold > 0:
        if direction == "above" and current_price >= price_threshold:
            return {
                "reason": f"Price moved above ${price_threshold:,.2f} (now ${current_price:,.2f})",
                "currentValue": current_price,
                "referenceValue": price_threshold,
            }
        if direction == "below" and current_price <= price_threshold:
            return {
                "reason": f"Price moved below ${price_threshold:,.2f} (now ${current_price:,.2f})",
                "currentValue": current_price,
                "referenceValue": price_threshold,
            }
        if direction == "move":
            if current_price >= price_threshold or current_price <= price_threshold * 0.98:
                return {
                    "reason": f"Price reached ${current_price:,.2f} (threshold: ${price_threshold:,.2f})",
                    "currentValue": current_price,
                    "referenceValue": price_threshold,
                }

    # Percent threshold from creation price
    if (
        percent_threshold is not None
        and percent_threshold > 0
        and reference_price is not None
        and reference_price > 0
    ):
        change_pct = ((current_price - reference_price) / reference_price) * 100
        abs_change = abs(change_pct)

        if direction == "above" and change_pct >= percent_threshold:
            return {
                "reason": f"Price up {change_pct:.2f}% from ${reference_price:,.2f} (threshold: +{percent_threshold}%)",
                "currentValue": current_price,
                "referenceValue": reference_price,
            }
        if direction == "below" and change_pct <= -percent_threshold:
            return {
                "reason": f"Price down {change_pct:.2f}% from ${reference_price:,.2f} (threshold: -{percent_threshold}%)",
                "currentValue": current_price,
                "referenceValue": reference_price,
            }
        if direction == "move" and abs_change >= percent_threshold:
            sign = "+" if change_pct >= 0 else ""
            return {
                "reason": f"Price moved {sign}{change_pct:.2f}% from ${reference_price:,.2f} (threshold: ±{percent_threshold}%)",
                "currentValue": current_price,
                "referenceValue": reference_price,
            }

    return None


def _evaluate_signal_change(cond: dict, features: list[dict]) -> dict | None:
    """Evaluate signal_change condition. Returns trigger metadata or None."""
    details = cond.get("details", {})
    feature_id = details.get("featureId")
    signal_dir = details.get("signalDirection")

    if not feature_id or not signal_dir:
        return None

    found = next((f for f in features if f.get("feature_id") == feature_id), None)

    if signal_dir == "appears" and found:
        return {
            "reason": f'Signal "{feature_id}" appeared in {cond["asset"]} top signals ({found.get("family", "?")} score {found.get("score", 0):.2f})',
            "currentValue": found.get("score"),
            "referenceValue": None,
        }

    if signal_dir == "disappears" and not found:
        return {
            "reason": f'Signal "{feature_id}" dropped from {cond["asset"]} top signals',
            "currentValue": 0,
            "referenceValue": details.get("signalCountAtCreation"),
        }

    return None


def _evaluate_regime_shift(cond: dict, regime: dict[str, str]) -> dict | None:
    """Evaluate regime_shift condition. Returns trigger metadata or None."""
    details = cond.get("details", {})
    target_regime = details.get("targetRegime")
    regime_at_creation = details.get("regimeAtCreation")
    current_label = regime.get("label", "unknown").lower()
    confidence = regime.get("confidence", "unknown")

    # Target regime: triggers when label matches target
    if target_regime:
        target = target_regime.lower()
        if current_label == target or target in current_label:
            return {
                "reason": f'Regime shifted to "{regime["label"]}" (target: "{target_regime}", confidence: {confidence})',
                "currentValue": None,
                "referenceValue": None,
            }
        return None

    # Any change: triggers when label differs from creation baseline
    if regime_at_creation:
        baseline = regime_at_creation.lower()
        if current_label != baseline and baseline not in current_label:
            return {
                "reason": f'Regime changed from "{regime_at_creation}" to "{regime["label"]}" (confidence: {confidence})',
                "currentValue": None,
                "referenceValue": None,
            }
        return None

    return None


# ── TTL Expiry ──

def _expire_conditions(conditions: list[dict]) -> int:
    """Mark active conditions as expired if past TTL. Returns count expired."""
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    expired = 0
    for c in conditions:
        if c.get("status") != "active":
            continue
        created_at = c.get("createdAt", "")
        ttl_hours = c.get("ttlHours", 72)
        try:
            created_ms = datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp() * 1000
        except Exception:
            continue
        if (now_ms - created_ms) > ttl_hours * 3600 * 1000:
            c["status"] = "expired"
            c["updatedAt"] = _now_iso()
            expired += 1
    return expired


# ── Main Evaluation ──

async def evaluate_watch_conditions():
    """POST /api/v1/workstation/watch-conditions/evaluate

    Evaluates all active conditions against live data.
    Returns summary of what was evaluated and triggered.
    """
    store = _load_conditions()
    conditions = store.get("conditions", [])

    # Run expiry first
    expired_count = _expire_conditions(conditions)

    # Filter active conditions by type
    active = [c for c in conditions if c.get("status") == "active"]
    if not active:
        if expired_count > 0:
            _save_conditions(store)
        return JSONResponse(content={
            "ok": True,
            "evaluated": 0,
            "triggered": 0,
            "expired": expired_count,
            "details": [],
        })

    price_conds = [c for c in active if c.get("type") == "price_move"]
    signal_conds = [
        c for c in active
        if c.get("type") == "signal_change"
        and c.get("details", {}).get("featureId")
        and c.get("details", {}).get("signalDirection")
    ]
    regime_conds = [
        c for c in active
        if c.get("type") == "regime_shift"
        and (c.get("details", {}).get("targetRegime") or c.get("details", {}).get("regimeAtCreation"))
    ]

    evaluable = price_conds + signal_conds + regime_conds

    if not evaluable:
        if expired_count > 0:
            _save_conditions(store)
        return JSONResponse(content={
            "ok": True,
            "evaluated": 0,
            "triggered": 0,
            "expired": expired_count,
            "details": [],
        })

    # Fetch data
    triggered_ids: list[str] = []
    trigger_details: list[dict] = []

    async with httpx.AsyncClient() as client:
        # Prices
        price_assets = list({c["asset"] for c in price_conds})
        prices: dict[str, float | None] = {}
        for asset in price_assets:
            prices[asset] = await _fetch_price(client, asset)

        # Signals
        signal_assets = list({c["asset"] for c in signal_conds})
        signals: dict[str, list[dict] | None] = {}
        for asset in signal_assets:
            signals[asset] = await _fetch_signals(client, asset)

        # Regime (single fetch, shared)
        regime: dict[str, str] | None = None
        if regime_conds:
            regime = await _fetch_regime(client)

    # Evaluate
    now_iso = _now_iso()

    for cond in evaluable:
        result = None
        cond_type = cond.get("type")

        if cond_type == "price_move":
            price = prices.get(cond["asset"])
            if price is not None:
                result = _evaluate_price_move(cond, price)

        elif cond_type == "signal_change":
            feats = signals.get(cond["asset"])
            if feats is not None:
                result = _evaluate_signal_change(cond, feats)

        elif cond_type == "regime_shift":
            if regime is not None:
                result = _evaluate_regime_shift(cond, regime)

        if result:
            cond["status"] = "triggered"
            cond["updatedAt"] = now_iso
            cond["trigger"] = {
                "triggeredAt": now_iso,
                "reason": result["reason"],
                "currentValue": result.get("currentValue"),
                "referenceValue": result.get("referenceValue"),
            }
            triggered_ids.append(cond["conditionId"])
            trigger_details.append({
                "conditionId": cond["conditionId"],
                "type": cond_type,
                "asset": cond["asset"],
                "reason": result["reason"],
            })

    # Save if anything changed
    if triggered_ids or expired_count > 0:
        _save_conditions(store)

    evaluated_count = len(evaluable)
    triggered_count = len(triggered_ids)

    if triggered_count > 0:
        logger.info(
            "Watch eval: %d evaluated, %d triggered (%s)",
            evaluated_count, triggered_count, ", ".join(triggered_ids),
        )

    return JSONResponse(content={
        "ok": True,
        "evaluated": evaluated_count,
        "triggered": triggered_count,
        "expired": expired_count,
        "triggeredIds": triggered_ids,
        "details": trigger_details,
    })
