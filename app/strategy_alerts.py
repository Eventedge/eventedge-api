"""SERVER-STRATEGIES-ALERTS-001: Strategy alert subscriptions + strategy diff.

File-backed alert subscriptions at /home/eventedge/alerts/strategy_alerts.json.
Atomic writes via tempfile + rename. No DB required.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from .strategies import _load_store as _load_strategies_store

ALERTS_DIR = Path(os.getenv("ROUTER_ALERT_DIR", "/home/eventedge/alerts"))
STRATEGY_ALERTS_FILE = ALERTS_DIR / "strategy_alerts.json"
RELEVANCE_FILE = ALERTS_DIR / "relevance_now.json"

MAX_SUBSCRIPTIONS = 500
VALID_CHANNELS = {"telegram"}

DEFAULT_RULES = {
    "feature_enter": True,
    "feature_exit": True,
    "rank_delta_min": 3,
    "score_delta_min": 0.10,
    "regime_change": True,
    "scoring_mode_flip": True,
    "drift_warning": True,
    "daily_digest": False,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# File I/O (atomic)
# ---------------------------------------------------------------------------

def _load_alert_store() -> dict[str, Any]:
    if not STRATEGY_ALERTS_FILE.exists():
        return {"version": 1, "items": []}
    try:
        data = json.loads(STRATEGY_ALERTS_FILE.read_text())
        if not isinstance(data, dict) or "items" not in data:
            return {"version": 1, "items": []}
        return data
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "items": []}


def _save_alert_store(store: dict[str, Any]) -> None:
    STRATEGY_ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(store, indent=2, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(dir=str(STRATEGY_ALERTS_FILE.parent), suffix=".tmp")
    try:
        os.write(fd, raw.encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        os.rename(tmp, str(STRATEGY_ALERTS_FILE))
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _strategy_exists(strategy_id: str) -> bool:
    store = _load_strategies_store()
    return any(i["id"] == strategy_id for i in store.get("items", []))


def _get_strategy(strategy_id: str) -> dict | None:
    store = _load_strategies_store()
    for i in store.get("items", []):
        if i["id"] == strategy_id:
            return i
    return None


def _validate_rules(rules: Any) -> tuple[dict, str | None]:
    """Validate and merge with defaults. Returns (merged_rules, error)."""
    if rules is None:
        return dict(DEFAULT_RULES), None
    if not isinstance(rules, dict):
        return {}, "rules must be a dict"
    merged = dict(DEFAULT_RULES)
    for k, v in rules.items():
        if k in DEFAULT_RULES:
            merged[k] = v
    return merged, None


# ---------------------------------------------------------------------------
# Alert CRUD builders
# ---------------------------------------------------------------------------

def build_alert_list() -> JSONResponse:
    store = _load_alert_store()
    return JSONResponse(
        content={
            "ok": True,
            "generated_at": _now_iso(),
            "count": len(store["items"]),
            "items": store["items"],
        },
        headers={"Cache-Control": "no-store"},
    )


def build_alert_get(alert_id: str) -> JSONResponse:
    store = _load_alert_store()
    for item in store["items"]:
        if item["id"] == alert_id:
            return JSONResponse(
                content={"ok": True, "generated_at": _now_iso(), "item": item},
                headers={"Cache-Control": "no-store"},
            )
    return JSONResponse(
        content={"ok": False, "generated_at": _now_iso(), "error": f"Alert {alert_id} not found"},
        status_code=404,
        headers={"Cache-Control": "no-store"},
    )


async def build_alert_create(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            content={"ok": False, "error": "Invalid JSON body"},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )

    strategy_id = body.get("strategy_id")
    if not strategy_id or not isinstance(strategy_id, str):
        return JSONResponse(
            content={"ok": False, "error": "strategy_id is required"},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )
    if not _strategy_exists(strategy_id):
        return JSONResponse(
            content={"ok": False, "error": f"Strategy {strategy_id} not found"},
            status_code=404,
            headers={"Cache-Control": "no-store"},
        )

    channel = body.get("channel", "telegram")
    if channel not in VALID_CHANNELS:
        return JSONResponse(
            content={"ok": False, "error": f"Invalid channel. Valid: {sorted(VALID_CHANNELS)}"},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )

    target = body.get("target", "")
    if not target or not isinstance(target, str):
        return JSONResponse(
            content={"ok": False, "error": "target is required"},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )

    rules, err = _validate_rules(body.get("rules"))
    if err:
        return JSONResponse(
            content={"ok": False, "error": err},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )

    cooldown_min = body.get("cooldown_min", 180)
    if not isinstance(cooldown_min, (int, float)) or cooldown_min < 0:
        cooldown_min = 180

    store = _load_alert_store()
    if len(store["items"]) >= MAX_SUBSCRIPTIONS:
        return JSONResponse(
            content={"ok": False, "error": f"Max {MAX_SUBSCRIPTIONS} subscriptions reached"},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )

    now = _now_iso()
    item = {
        "id": str(uuid.uuid4()),
        "strategy_id": strategy_id,
        "channel": channel,
        "target": target,
        "is_enabled": body.get("is_enabled", True),
        "rules": rules,
        "cooldown_min": int(cooldown_min),
        "last_sent_at": None,
        "created_at": now,
        "updated_at": now,
    }
    store["items"].append(item)
    _save_alert_store(store)

    return JSONResponse(
        content={"ok": True, "generated_at": now, "item": item},
        status_code=201,
        headers={"Cache-Control": "no-store"},
    )


async def build_alert_update(request: Request, alert_id: str) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            content={"ok": False, "error": "Invalid JSON body"},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )

    store = _load_alert_store()
    target_item = None
    for item in store["items"]:
        if item["id"] == alert_id:
            target_item = item
            break

    if not target_item:
        return JSONResponse(
            content={"ok": False, "error": f"Alert {alert_id} not found"},
            status_code=404,
            headers={"Cache-Control": "no-store"},
        )

    if "is_enabled" in body and isinstance(body["is_enabled"], bool):
        target_item["is_enabled"] = body["is_enabled"]

    if "rules" in body:
        rules, err = _validate_rules(body["rules"])
        if err:
            return JSONResponse(
                content={"ok": False, "error": err},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        target_item["rules"] = rules

    if "cooldown_min" in body:
        cd = body["cooldown_min"]
        if isinstance(cd, (int, float)) and cd >= 0:
            target_item["cooldown_min"] = int(cd)

    if "target" in body and isinstance(body["target"], str) and body["target"]:
        target_item["target"] = body["target"]

    target_item["updated_at"] = _now_iso()
    _save_alert_store(store)

    return JSONResponse(
        content={"ok": True, "generated_at": _now_iso(), "item": target_item},
        headers={"Cache-Control": "no-store"},
    )


def build_alert_delete(alert_id: str) -> JSONResponse:
    store = _load_alert_store()
    original_len = len(store["items"])
    store["items"] = [i for i in store["items"] if i["id"] != alert_id]

    if len(store["items"]) == original_len:
        return JSONResponse(
            content={"ok": False, "error": f"Alert {alert_id} not found"},
            status_code=404,
            headers={"Cache-Control": "no-store"},
        )

    _save_alert_store(store)
    return JSONResponse(
        content={"ok": True, "generated_at": _now_iso(), "deleted": alert_id},
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# Strategy diff builder
# ---------------------------------------------------------------------------

def _load_relevance(day: str | None = None) -> dict | None:
    """Load relevance_now.json or dated variant."""
    import re
    _DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    if day and _DAY_RE.match(day):
        dated = ALERTS_DIR / f"relevance_now.{day}.json"
        if dated.exists():
            try:
                return json.loads(dated.read_text())
            except (json.JSONDecodeError, OSError):
                return None
    if RELEVANCE_FILE.exists():
        try:
            return json.loads(RELEVANCE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return None
    return None


def _yesterday_str(day: str) -> str:
    """Return YYYY-MM-DD for the day before."""
    try:
        dt = datetime.strptime(day, "%Y-%m-%d")
        prev = dt.replace(day=dt.day) - __import__("datetime").timedelta(days=1)
        return prev.strftime("%Y-%m-%d")
    except Exception:
        return ""


def _extract_top_ids(data: dict, asset: str, horizon: str) -> list[dict]:
    """Extract top feature list for asset/horizon from relevance data."""
    asset_data = data.get("assets", {}).get(asset.upper(), {})
    h_data = asset_data.get("horizons", {}).get(horizon.lower(), {})
    return h_data.get("top", [])


def build_strategy_diff(strategy_id: str) -> JSONResponse:
    """GET /api/v1/strategies/{id}/diff — compare today vs yesterday for strategy context."""
    now = _now_iso()

    strategy = _get_strategy(strategy_id)
    if not strategy:
        return JSONResponse(
            content={"ok": False, "generated_at": now, "error": f"Strategy {strategy_id} not found"},
            status_code=404,
            headers={"Cache-Control": "no-store"},
        )

    payload = strategy.get("payload", {})
    defaults = payload.get("asset_defaults", {})
    asset = defaults.get("asset", "BTC")
    horizon = defaults.get("horizon", "24h")

    today_data = _load_relevance()
    if not today_data:
        return JSONResponse(
            content={"ok": False, "generated_at": now, "error": "No relevance data available"},
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    today_day = today_data.get("day", "")
    yesterday_day = _yesterday_str(today_day) if today_day else ""

    yesterday_data = _load_relevance(yesterday_day) if yesterday_day else None

    today_top = _extract_top_ids(today_data, asset, horizon)
    yesterday_top = _extract_top_ids(yesterday_data, asset, horizon) if yesterday_data else []

    today_map = {f.get("feature_id"): f for f in today_top}
    yesterday_map = {f.get("feature_id"): f for f in yesterday_top}

    today_ids = set(today_map.keys())
    yesterday_ids = set(yesterday_map.keys())

    added = [
        {"feature_id": fid, "rank": today_map[fid].get("rank"), "score": today_map[fid].get("score")}
        for fid in sorted(today_ids - yesterday_ids, key=lambda x: today_map[x].get("rank", 99))
    ]
    dropped = [
        {"feature_id": fid, "rank": yesterday_map[fid].get("rank"), "score": yesterday_map[fid].get("score")}
        for fid in sorted(yesterday_ids - today_ids, key=lambda x: yesterday_map[x].get("rank", 99))
    ]

    rank_movers = []
    score_movers = []
    for fid in today_ids & yesterday_ids:
        t = today_map[fid]
        y = yesterday_map[fid]
        rank_delta = (y.get("rank") or 99) - (t.get("rank") or 99)
        score_delta = (t.get("score") or 0) - (y.get("score") or 0)
        if abs(rank_delta) >= 2:
            rank_movers.append({"feature_id": fid, "rank_delta": rank_delta, "rank": t.get("rank")})
        if abs(score_delta) >= 0.05:
            score_movers.append({"feature_id": fid, "score_delta": round(score_delta, 3), "score": t.get("score")})

    rank_movers.sort(key=lambda x: abs(x["rank_delta"]), reverse=True)
    score_movers.sort(key=lambda x: abs(x["score_delta"]), reverse=True)

    # Regime + scoring mode
    today_asset = today_data.get("assets", {}).get(asset.upper(), {})
    yesterday_asset = (yesterday_data or {}).get("assets", {}).get(asset.upper(), {})
    today_regime = today_asset.get("regime_bucket", "unknown")
    yesterday_regime = yesterday_asset.get("regime_bucket", "unknown")
    today_scoring = today_data.get("scoring_mode", "health")
    yesterday_scoring = (yesterday_data or {}).get("scoring_mode", "health")

    regime_changed = yesterday_data is not None and today_regime != yesterday_regime
    scoring_mode_changed = yesterday_data is not None and today_scoring != yesterday_scoring

    result = {
        "ok": True,
        "generated_at": now,
        "strategy_id": strategy_id,
        "strategy_name": strategy.get("name", ""),
        "asset": asset,
        "horizon": horizon,
        "today": today_day,
        "yesterday": yesterday_day,
        "yesterday_available": yesterday_data is not None,
        "regime": {
            "today": today_regime,
            "yesterday": yesterday_regime,
            "changed": regime_changed,
        },
        "scoring_mode": {
            "today": today_scoring,
            "yesterday": yesterday_scoring,
            "changed": scoring_mode_changed,
        },
        "added": added,
        "dropped": dropped,
        "rank_movers": rank_movers[:10],
        "score_movers": score_movers[:10],
        "summary": {
            "n_added": len(added),
            "n_dropped": len(dropped),
            "n_rank_movers": len(rank_movers),
            "n_score_movers": len(score_movers),
            "regime_changed": regime_changed,
            "scoring_mode_changed": scoring_mode_changed,
        },
    }
    return JSONResponse(content=result, headers={"Cache-Control": "no-store"})
