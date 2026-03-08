"""SERVER-STRATEGIES-ALERTS-001/002: Strategy alert subscriptions + strategy diff.

File-backed alert subscriptions at /home/eventedge/alerts/strategy_alerts.json.
Atomic writes via tempfile + rename. No DB required.

v002: digest_mode, severity, snoozed_until fields; snooze/unsnooze endpoints.
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
VALID_SEVERITIES = {"low", "medium", "high"}

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


def _ensure_alert_metadata(item: dict) -> dict:
    """Backfill v002 alert fields on legacy items (in-place)."""
    item.setdefault("digest_mode", False)
    item.setdefault("severity", "medium")
    item.setdefault("snoozed_until", None)
    return item


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
    items = [_ensure_alert_metadata(i) for i in store["items"]]
    return JSONResponse(
        content={
            "ok": True,
            "generated_at": _now_iso(),
            "count": len(items),
            "items": items,
        },
        headers={"Cache-Control": "no-store"},
    )


def build_alert_get(alert_id: str) -> JSONResponse:
    store = _load_alert_store()
    for item in store["items"]:
        if item["id"] == alert_id:
            _ensure_alert_metadata(item)
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

    severity = body.get("severity", "medium")
    if severity not in VALID_SEVERITIES:
        severity = "medium"

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
        "digest_mode": bool(body.get("digest_mode", False)),
        "severity": severity,
        "snoozed_until": None,
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

    # v002 fields
    _ensure_alert_metadata(target_item)
    if "digest_mode" in body and isinstance(body["digest_mode"], bool):
        target_item["digest_mode"] = body["digest_mode"]
    if "severity" in body and body["severity"] in VALID_SEVERITIES:
        target_item["severity"] = body["severity"]

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


def _family_of(feature_id: str) -> str:
    """Extract family from feature_id like 'ta.scanner_dir@1.0' -> 'ta'."""
    return feature_id.split(".")[0] if "." in feature_id else feature_id.split("@")[0]


def _build_family_changes(
    added: list[dict], dropped: list[dict],
    rank_movers: list[dict], score_movers: list[dict],
) -> list[dict]:
    """Group feature changes by family. Returns list of {family, added, dropped, movers, net_direction}."""
    from collections import defaultdict
    fam_added: dict[str, list] = defaultdict(list)
    fam_dropped: dict[str, list] = defaultdict(list)
    fam_movers: dict[str, list] = defaultdict(list)

    for f in added:
        fam_added[_family_of(f["feature_id"])].append(f["feature_id"])
    for f in dropped:
        fam_dropped[_family_of(f["feature_id"])].append(f["feature_id"])
    for f in rank_movers + score_movers:
        fam = _family_of(f["feature_id"])
        if f["feature_id"] not in fam_movers[fam]:
            fam_movers[fam].append(f["feature_id"])

    all_families = sorted(set(list(fam_added) + list(fam_dropped) + list(fam_movers)))
    results = []
    for fam in all_families:
        a = fam_added.get(fam, [])
        d = fam_dropped.get(fam, [])
        m = fam_movers.get(fam, [])
        net = len(a) - len(d)
        direction = "gaining" if net > 0 else "losing" if net < 0 else "stable"
        results.append({
            "family": fam,
            "added": a,
            "dropped": d,
            "movers": m,
            "n_added": len(a),
            "n_dropped": len(d),
            "n_movers": len(m),
            "net_direction": direction,
        })
    return results


def _build_family_rank_changes(
    today_families: list[dict], yesterday_families: list[dict],
) -> list[dict]:
    """Compare top_families between today and yesterday."""
    today_map = {f.get("family", ""): i for i, f in enumerate(today_families)}
    yesterday_map = {f.get("family", ""): i for i, f in enumerate(yesterday_families)}
    all_fams = sorted(set(list(today_map) + list(yesterday_map)))
    changes = []
    for fam in all_fams:
        if not fam:
            continue
        t_rank = today_map.get(fam)
        y_rank = yesterday_map.get(fam)
        t_entry = today_families[t_rank] if t_rank is not None else {}
        if t_rank is not None and y_rank is not None:
            delta = y_rank - t_rank  # positive = improved
            if delta != 0:
                changes.append({
                    "family": fam,
                    "rank_today": t_rank + 1,
                    "rank_yesterday": y_rank + 1,
                    "rank_delta": delta,
                    "boost_direction": t_entry.get("boost_direction", "neutral"),
                })
        elif t_rank is not None and y_rank is None:
            changes.append({
                "family": fam,
                "rank_today": t_rank + 1,
                "rank_yesterday": None,
                "rank_delta": None,
                "status": "new",
                "boost_direction": t_entry.get("boost_direction", "neutral"),
            })
        elif y_rank is not None and t_rank is None:
            changes.append({
                "family": fam,
                "rank_today": None,
                "rank_yesterday": y_rank + 1,
                "rank_delta": None,
                "status": "removed",
            })
    changes.sort(key=lambda x: abs(x.get("rank_delta") or 0), reverse=True)
    return changes


def _build_top_reasons(
    added: list, dropped: list, rank_movers: list, score_movers: list,
    regime_changed: bool, today_regime: str, yesterday_regime: str,
    scoring_changed: bool, today_scoring: str, yesterday_scoring: str,
    family_rank_changes: list,
) -> list[str]:
    """Generate human-readable top reasons for the diff."""
    reasons: list[str] = []
    if regime_changed:
        reasons.append(f"Regime shifted: {yesterday_regime} → {today_regime}")
    if scoring_changed:
        reasons.append(f"Scoring mode: {yesterday_scoring} → {today_scoring}")
    if added:
        families = sorted(set(_family_of(f["feature_id"]) for f in added))
        reasons.append(f"{len(added)} features entered ({', '.join(families)})")
    if dropped:
        families = sorted(set(_family_of(f["feature_id"]) for f in dropped))
        reasons.append(f"{len(dropped)} features exited ({', '.join(families)})")
    for fc in family_rank_changes[:3]:
        fam = fc["family"]
        if fc.get("status") == "new":
            reasons.append(f"{fam} family newly ranked (#{fc['rank_today']})")
        elif fc.get("rank_delta") and abs(fc["rank_delta"]) >= 2:
            direction = "rose" if fc["rank_delta"] > 0 else "fell"
            reasons.append(f"{fam} family {direction} {abs(fc['rank_delta'])} ranks")
    if rank_movers:
        top = rank_movers[0]
        direction = "up" if top["rank_delta"] > 0 else "down"
        reasons.append(f"Biggest mover: {top['feature_id'].split('@')[0]} ({direction} {abs(top['rank_delta'])})")
    return reasons[:6]


def _build_alert_digest(
    asset: str, horizon: str,
    added: list, dropped: list, rank_movers: list, score_movers: list,
    regime_changed: bool, today_regime: str, yesterday_regime: str,
    scoring_changed: bool, today_scoring: str, yesterday_scoring: str,
    family_changes: list, family_rank_changes: list,
) -> dict:
    """Build compact alert digest with headline + bullets for bot/notification use."""
    parts = []
    if regime_changed:
        parts.append(f"regime {yesterday_regime}→{today_regime}")
    if scoring_changed:
        parts.append(f"scoring {yesterday_scoring}→{today_scoring}")
    if added:
        parts.append(f"+{len(added)} features")
    if dropped:
        parts.append(f"-{len(dropped)} features")
    if rank_movers:
        parts.append(f"{len(rank_movers)} rank moves")

    headline = f"{asset}/{horizon}: " + (", ".join(parts) if parts else "no changes")

    bullets: list[str] = []
    if regime_changed:
        bullets.append(f"Regime: {yesterday_regime} → {today_regime}")
    if scoring_changed:
        bullets.append(f"Scoring: {yesterday_scoring} → {today_scoring}")

    # Family-level bullets
    gaining = [fc for fc in family_changes if fc["net_direction"] == "gaining"]
    losing = [fc for fc in family_changes if fc["net_direction"] == "losing"]
    if gaining:
        names = ", ".join(fc["family"] for fc in gaining[:3])
        bullets.append(f"Gaining families: {names}")
    if losing:
        names = ", ".join(fc["family"] for fc in losing[:3])
        bullets.append(f"Losing families: {names}")

    # Top added/dropped
    if added:
        top3 = [f["feature_id"].split("@")[0] for f in added[:3]]
        bullets.append(f"Entered: {', '.join(top3)}")
    if dropped:
        top3 = [f["feature_id"].split("@")[0] for f in dropped[:3]]
        bullets.append(f"Exited: {', '.join(top3)}")

    return {"headline": headline, "bullets": bullets[:8]}


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

    # --- Family-aware grouping ---
    family_changes = _build_family_changes(added, dropped, rank_movers, score_movers)

    # Top families from relevance horizons
    today_h = today_data.get("assets", {}).get(asset.upper(), {}).get("horizons", {}).get(horizon.lower(), {})
    yesterday_h = (yesterday_data or {}).get("assets", {}).get(asset.upper(), {}).get("horizons", {}).get(horizon.lower(), {})
    today_families = today_h.get("meta", {}).get("top_families", [])
    yesterday_families = yesterday_h.get("meta", {}).get("top_families", [])
    family_rank_changes = _build_family_rank_changes(today_families, yesterday_families)

    top_reasons = _build_top_reasons(
        added, dropped, rank_movers, score_movers,
        regime_changed, today_regime, yesterday_regime,
        scoring_mode_changed, today_scoring, yesterday_scoring,
        family_rank_changes,
    )
    alert_digest = _build_alert_digest(
        asset, horizon, added, dropped, rank_movers, score_movers,
        regime_changed, today_regime, yesterday_regime,
        scoring_mode_changed, today_scoring, yesterday_scoring,
        family_changes, family_rank_changes,
    )

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
        "family_changes": family_changes,
        "family_rank_changes": family_rank_changes,
        "top_reasons": top_reasons,
        "alert_digest": alert_digest,
        "summary": {
            "n_added": len(added),
            "n_dropped": len(dropped),
            "n_rank_movers": len(rank_movers),
            "n_score_movers": len(score_movers),
            "regime_changed": regime_changed,
            "scoring_mode_changed": scoring_mode_changed,
            "n_family_changes": len(family_changes),
        },
    }
    return JSONResponse(content=result, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# Alert preview — what WOULD trigger right now
# ---------------------------------------------------------------------------

LEDGER_FILE = ALERTS_DIR / "strategy_alerts_ledger.jsonl"

_RULE_LABELS = {
    "feature_enter": "Feature entered Top-K",
    "feature_exit": "Feature exited Top-K",
    "rank_delta_min": "Large rank move",
    "score_delta_min": "Large score change",
    "regime_change": "Regime changed",
    "scoring_mode_flip": "Scoring mode flipped",
    "drift_warning": "Drift warning",
    "daily_digest": "Daily digest",
}


def _evaluate_rules_preview(diff: dict, rules: dict) -> list[dict]:
    """Evaluate rules against a diff. Returns list of {rule, label, detail, triggered}."""
    results = []
    summary = diff.get("summary", {})

    def _add(rule_key: str, triggered: bool, detail: str = ""):
        results.append({
            "rule": rule_key,
            "label": _RULE_LABELS.get(rule_key, rule_key),
            "enabled": bool(rules.get(rule_key, False)),
            "triggered": triggered,
            "detail": detail,
        })

    # Regime change
    regime = diff.get("regime", {})
    regime_triggered = bool(summary.get("regime_changed"))
    regime_detail = f"{regime.get('yesterday')} → {regime.get('today')}" if regime_triggered else ""
    _add("regime_change", regime_triggered, regime_detail)

    # Scoring mode flip
    sm = diff.get("scoring_mode", {})
    sm_triggered = bool(summary.get("scoring_mode_changed"))
    sm_detail = f"{sm.get('yesterday')} → {sm.get('today')}" if sm_triggered else ""
    _add("scoring_mode_flip", sm_triggered, sm_detail)

    # Feature enter
    n_added = summary.get("n_added", 0)
    added_names = ", ".join(f.get("feature_id", "?").split("@")[0] for f in diff.get("added", [])[:5])
    _add("feature_enter", n_added > 0, f"{n_added} added: {added_names}" if n_added else "")

    # Feature exit
    n_dropped = summary.get("n_dropped", 0)
    dropped_names = ", ".join(f.get("feature_id", "?").split("@")[0] for f in diff.get("dropped", [])[:5])
    _add("feature_exit", n_dropped > 0, f"{n_dropped} dropped: {dropped_names}" if n_dropped else "")

    # Rank movers
    rank_min = rules.get("rank_delta_min", 3)
    big_movers = [m for m in diff.get("rank_movers", []) if abs(m.get("rank_delta", 0)) >= rank_min]
    _add("rank_delta_min", len(big_movers) > 0,
         f"{len(big_movers)} movers (threshold ≥{rank_min})" if big_movers else f"threshold ≥{rank_min}")

    # Score movers
    score_min = rules.get("score_delta_min", 0.10)
    big_scores = [m for m in diff.get("score_movers", []) if abs(m.get("score_delta", 0)) >= score_min]
    _add("score_delta_min", len(big_scores) > 0,
         f"{len(big_scores)} changes (threshold ≥{score_min})" if big_scores else f"threshold ≥{score_min}")

    return results


def _check_cooldown(alert: dict) -> dict:
    """Check cooldown status. Returns {elapsed, suppressed, snoozed, last_sent_at, cooldown_min}."""
    last = alert.get("last_sent_at")
    cooldown_min = alert.get("cooldown_min", 180)

    # Check snooze
    snoozed_until = alert.get("snoozed_until")
    snoozed = False
    if snoozed_until:
        try:
            snooze_dt = datetime.fromisoformat(snoozed_until)
            snoozed = datetime.now(timezone.utc) < snooze_dt
        except Exception:
            pass

    if not last:
        return {
            "elapsed_min": None, "suppressed": snoozed, "snoozed": snoozed,
            "snoozed_until": snoozed_until, "last_sent_at": None, "cooldown_min": cooldown_min,
        }
    try:
        last_dt = datetime.fromisoformat(last)
        elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
        return {
            "elapsed_min": round(elapsed, 1),
            "suppressed": (elapsed < cooldown_min) or snoozed,
            "snoozed": snoozed,
            "snoozed_until": snoozed_until,
            "last_sent_at": last,
            "cooldown_min": cooldown_min,
        }
    except Exception:
        return {
            "elapsed_min": None, "suppressed": snoozed, "snoozed": snoozed,
            "snoozed_until": snoozed_until, "last_sent_at": last, "cooldown_min": cooldown_min,
        }


def build_alert_preview(alert_id: str) -> JSONResponse:
    """GET /api/v1/strategy-alerts/{id}/preview — what would trigger right now."""
    now = _now_iso()

    store = _load_alert_store()
    alert = None
    for item in store["items"]:
        if item["id"] == alert_id:
            alert = item
            break
    if not alert:
        return JSONResponse(
            content={"ok": False, "generated_at": now, "error": f"Alert {alert_id} not found"},
            status_code=404, headers={"Cache-Control": "no-store"},
        )

    strategy_id = alert["strategy_id"]
    strategy = _get_strategy(strategy_id)
    if not strategy:
        return JSONResponse(
            content={"ok": False, "generated_at": now, "error": f"Strategy {strategy_id} not found"},
            status_code=404, headers={"Cache-Control": "no-store"},
        )

    # Get diff (reuse existing logic)
    diff_resp = build_strategy_diff(strategy_id)
    try:
        diff_data = json.loads(diff_resp.body.decode("utf-8"))
    except Exception:
        diff_data = {}

    if not diff_data.get("ok"):
        return JSONResponse(
            content={"ok": False, "generated_at": now, "error": "Could not compute diff", "detail": diff_data.get("error", "")},
            status_code=200, headers={"Cache-Control": "no-store"},
        )

    rules = alert.get("rules", {})
    rule_results = _evaluate_rules_preview(diff_data, rules)
    cooldown = _check_cooldown(alert)
    triggered = [r for r in rule_results if r["triggered"] and r["enabled"]]
    would_send = len(triggered) > 0 and not cooldown["suppressed"]

    return JSONResponse(content={
        "ok": True,
        "generated_at": now,
        "alert_id": alert_id,
        "strategy_id": strategy_id,
        "strategy_name": strategy.get("name", ""),
        "is_enabled": alert.get("is_enabled", False),
        "cooldown": cooldown,
        "would_send": would_send,
        "n_triggered": len(triggered),
        "rules": rule_results,
        "diff_summary": diff_data.get("summary", {}),
        "family_changes": diff_data.get("family_changes", []),
        "top_reasons": diff_data.get("top_reasons", []),
        "alert_digest": diff_data.get("alert_digest", {}),
    }, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# Alert history — recent sent alerts from ledger
# ---------------------------------------------------------------------------

def _tail_file(path: Path, n: int = 200) -> list[str]:
    """Read last N lines from a file."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, n * 500)
            f.seek(max(0, size - chunk))
            data = f.read().decode("utf-8", errors="replace")
            return data.strip().split("\n")[-n:]
    except (FileNotFoundError, OSError):
        return []


def build_alert_history(alert_id: str, limit: int = 20) -> JSONResponse:
    """GET /api/v1/strategy-alerts/{id}/history — recent sent alerts from ledger."""
    now = _now_iso()

    store = _load_alert_store()
    alert = None
    for item in store["items"]:
        if item["id"] == alert_id:
            alert = item
            break
    if not alert:
        return JSONResponse(
            content={"ok": False, "generated_at": now, "error": f"Alert {alert_id} not found"},
            status_code=404, headers={"Cache-Control": "no-store"},
        )

    lines = _tail_file(LEDGER_FILE, 500)
    entries = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("alert_id") == alert_id:
            entries.append({
                "ts": obj.get("ts"),
                "strategy_name": obj.get("strategy_name", ""),
                "triggers": obj.get("triggers", []),
                "matched_rules": obj.get("matched_rules", []),
                "added_count": obj.get("added_count"),
                "dropped_count": obj.get("dropped_count"),
                "regime_changed": obj.get("regime_changed"),
            })
            if len(entries) >= limit:
                break

    return JSONResponse(content={
        "ok": True,
        "generated_at": now,
        "alert_id": alert_id,
        "strategy_id": alert["strategy_id"],
        "count": len(entries),
        "items": entries,
    }, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# Snooze / Unsnooze
# ---------------------------------------------------------------------------

async def build_alert_snooze(request: Request, alert_id: str) -> JSONResponse:
    """POST /api/v1/strategy-alerts/{id}/snooze — snooze alert for N minutes."""
    now = _now_iso()

    try:
        body = await request.json()
    except Exception:
        body = {}

    minutes = body.get("minutes", 60)
    if not isinstance(minutes, (int, float)) or minutes <= 0:
        minutes = 60
    minutes = min(minutes, 1440 * 7)  # cap at 7 days

    store = _load_alert_store()
    for item in store["items"]:
        if item["id"] == alert_id:
            _ensure_alert_metadata(item)
            from datetime import timedelta
            until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
            item["snoozed_until"] = until.isoformat()
            item["updated_at"] = _now_iso()
            _save_alert_store(store)
            return JSONResponse(
                content={
                    "ok": True, "generated_at": now, "alert_id": alert_id,
                    "snoozed_until": item["snoozed_until"],
                    "minutes": minutes,
                },
                headers={"Cache-Control": "no-store"},
            )

    return JSONResponse(
        content={"ok": False, "error": f"Alert {alert_id} not found"},
        status_code=404, headers={"Cache-Control": "no-store"},
    )


def build_alert_unsnooze(alert_id: str) -> JSONResponse:
    """POST /api/v1/strategy-alerts/{id}/unsnooze — clear snooze."""
    now = _now_iso()

    store = _load_alert_store()
    for item in store["items"]:
        if item["id"] == alert_id:
            _ensure_alert_metadata(item)
            item["snoozed_until"] = None
            item["updated_at"] = _now_iso()
            _save_alert_store(store)
            return JSONResponse(
                content={"ok": True, "generated_at": now, "alert_id": alert_id, "snoozed_until": None},
                headers={"Cache-Control": "no-store"},
            )

    return JSONResponse(
        content={"ok": False, "error": f"Alert {alert_id} not found"},
        status_code=404, headers={"Cache-Control": "no-store"},
    )
