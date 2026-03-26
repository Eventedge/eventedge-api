"""Workstation audit log — JSONL append-only event store.

Bundle 68: Captures alert-related events for auditability.
Events are never modified — they form an immutable timeline.

Bundle 72: Adds summary analytics over the audit log (deterministic,
computed at read time from JSONL — no separate pipeline).

Bundle 73: Adds fatigue analysis — deterministic rules that identify
noisy conditions, over-triggering, and suggest threshold adjustments.

Bundle 76: Adds retention/pruning so the log stays bounded.
Policy: keep last 14 days. Auto-prune triggers when file exceeds 2MB.
Explicit prune endpoint for manual/cron use.

Bundle 77: Adds per-asset/per-category drilldown, fatigue mutation audit,
and condition effectiveness scoring baseline.

Event categories:
  - watch_triggered: A watch condition evaluated to true
  - watch_expired: A watch condition expired by TTL
  - delivery_sent: Telegram notification successfully delivered
  - delivery_failed: Telegram notification failed
  - significant_change: A significant market change was detected
  - fatigue_mutation: Operator applied a fatigue-driven threshold/profile change

Endpoints:
  GET /api/v1/workstation/audit-log?limit=50&category=...
  GET /api/v1/workstation/audit-log/summary?hours=24
  GET /api/v1/workstation/audit-log/fatigue?hours=24
  POST /api/v1/workstation/audit-log/prune — explicit retention prune
  GET /api/v1/workstation/audit-log/drilldown?asset=BTC&category=L1&hours=24
  GET /api/v1/workstation/audit-log/effectiveness?hours=48
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

ALERTS_DIR = Path(os.getenv("ROUTER_ALERT_DIR", "/home/eventedge/alerts"))
AUDIT_FILE = ALERTS_DIR / "workstation_audit_log.jsonl"

MAX_READ_LIMIT = 200
RETENTION_DAYS = 14
AUTO_PRUNE_SIZE_BYTES = 2 * 1024 * 1024  # 2MB
VALID_CATEGORIES = {
    "watch_triggered",
    "watch_expired",
    "delivery_sent",
    "delivery_failed",
    "significant_change",
    "fatigue_mutation",
}

# Asset → category mapping (mirrors client-side asset-coverage.ts)
ASSET_CATEGORIES: dict[str, str] = {
    "BTC": "L1", "ETH": "L1", "SOL": "L1", "AVAX": "L1", "ADA": "L1",
    "SUI": "L1", "NEAR": "L1", "SEI": "L1",
    "ARB": "L2", "OP": "L2",
    "AAVE": "DeFi", "UNI": "DeFi", "INJ": "DeFi",
    "DOGE": "Meme", "PEPE": "Meme", "WIF": "Meme",
    "LINK": "Infra", "TIA": "Infra",
    "BNB": "Exchange", "HYPE": "Exchange",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_audit_event(
    category: str,
    payload: dict[str, Any],
    *,
    condition_id: str | None = None,
    asset: str | None = None,
) -> None:
    """Append a single audit event to the JSONL log.

    Called internally from evaluator/notify modules — not an endpoint.
    Auto-prunes when file exceeds AUTO_PRUNE_SIZE_BYTES.
    """
    if category not in VALID_CATEGORIES:
        return
    entry = {
        "ts": _now_iso(),
        "category": category,
        "conditionId": condition_id,
        "asset": asset,
        "payload": payload,
    }
    try:
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # Never block caller on audit failure

    # Auto-prune when file gets large — fire-and-forget, never blocks
    try:
        if AUDIT_FILE.exists() and AUDIT_FILE.stat().st_size > AUTO_PRUNE_SIZE_BYTES:
            _prune_to_retention()
    except Exception:
        pass


def _prune_to_retention(retention_days: int = RETENTION_DAYS) -> dict[str, Any]:
    """Prune events older than retention_days. Atomic rewrite.

    Returns stats about what was pruned. Safe to call concurrently —
    the atomic rename means readers never see a partial file.
    """
    if not AUDIT_FILE.exists():
        return {"pruned": False, "reason": "no_file"}

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    cutoff_ts = cutoff.timestamp()

    original_size = AUDIT_FILE.stat().st_size
    kept: list[str] = []
    total_lines = 0
    parse_errors = 0

    try:
        with open(AUDIT_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                total_lines += 1
                try:
                    evt = json.loads(line)
                    ts_str = evt.get("ts", "")
                    if ts_str:
                        ts_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if ts_dt.timestamp() >= cutoff_ts:
                            kept.append(line)
                            continue
                    # No timestamp or too old — drop
                except (json.JSONDecodeError, ValueError):
                    parse_errors += 1
                    # Drop unparseable lines during prune
    except Exception as exc:
        return {"pruned": False, "reason": "read_error", "error": str(exc)}

    pruned_count = total_lines - len(kept)
    if pruned_count == 0 and parse_errors == 0:
        return {
            "pruned": False,
            "reason": "nothing_to_prune",
            "total_events": total_lines,
            "retention_days": retention_days,
        }

    # Atomic rewrite: temp file in same directory → rename
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(ALERTS_DIR), prefix="audit_prune_", suffix=".jsonl"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            for line in kept:
                tmp.write(line + "\n")
        os.replace(tmp_path, str(AUDIT_FILE))
    except Exception as exc:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return {"pruned": False, "reason": "write_error", "error": str(exc)}

    new_size = AUDIT_FILE.stat().st_size if AUDIT_FILE.exists() else 0
    return {
        "pruned": True,
        "retention_days": retention_days,
        "events_before": total_lines,
        "events_after": len(kept),
        "events_pruned": pruned_count,
        "parse_errors_dropped": parse_errors,
        "bytes_before": original_size,
        "bytes_after": new_size,
        "bytes_freed": original_size - new_size,
    }


def _read_recent(limit: int = 50, category: str | None = None) -> list[dict[str, Any]]:
    """Read most recent audit events (tail of JSONL)."""
    if not AUDIT_FILE.exists():
        return []
    limit = min(limit, MAX_READ_LIMIT)

    results: list[dict[str, Any]] = []
    try:
        with open(AUDIT_FILE, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return []
            read_size = min(size, 150_000)
            f.seek(size - read_size)
            chunk = f.read().decode("utf-8", errors="replace")

        lines = chunk.strip().split("\n")
        for line in reversed(lines):
            if len(results) >= limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
                if category and evt.get("category") != category:
                    continue
                results.append(evt)
            except json.JSONDecodeError:
                continue
    except Exception:
        pass

    return results


def _get_stats() -> dict[str, Any]:
    """Quick stats without reading entire file."""
    if not AUDIT_FILE.exists():
        return {"totalEvents": 0, "fileSizeBytes": 0}
    stat = AUDIT_FILE.stat()
    est_count = max(1, stat.st_size // 180)
    return {
        "totalEvents": est_count,
        "fileSizeBytes": stat.st_size,
        "lastModified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "retentionDays": RETENTION_DAYS,
        "autoPruneThresholdBytes": AUTO_PRUNE_SIZE_BYTES,
    }


# ── Endpoints ──

# Categories accepted via POST from clients (subset — delivery/trigger are server-only)
CLIENT_CATEGORIES = {"significant_change", "fatigue_mutation"}


async def post_audit_event(request: Request):
    """POST /api/v1/workstation/audit-log — append a single client-side event."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(content={"error": "Invalid JSON"}, status_code=400)

    category = body.get("category", "")
    if category not in CLIENT_CATEGORIES:
        return JSONResponse(content={"error": f"Invalid category: {category}"}, status_code=400)

    append_audit_event(
        category,
        body.get("payload", {}),
        condition_id=body.get("conditionId"),
        asset=body.get("asset"),
    )
    return JSONResponse(content={"ok": True, "ts": _now_iso()})


def get_audit_log(limit: int = 50, category: str | None = None):
    """GET /api/v1/workstation/audit-log"""
    events = _read_recent(limit, category)
    stats = _get_stats()
    return JSONResponse(content={
        "events": events,
        "count": len(events),
        "stats": stats,
    })


# ── Summary Analytics (Bundle 72) ──

def _read_all_within(hours: int = 24) -> list[dict[str, Any]]:
    """Read all events within the last N hours.

    Reads from the tail of the JSONL file (up to 500KB) and filters
    by timestamp. This is bounded and fast for typical audit volumes.
    """
    if not AUDIT_FILE.exists():
        return []
    cutoff = datetime.now(timezone.utc).timestamp() - (hours * 3600)
    results: list[dict[str, Any]] = []
    try:
        with open(AUDIT_FILE, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return []
            read_size = min(size, 500_000)
            f.seek(size - read_size)
            chunk = f.read().decode("utf-8", errors="replace")

        for line in chunk.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
                ts_str = evt.get("ts", "")
                if ts_str:
                    # Parse ISO timestamp
                    ts_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if ts_dt.timestamp() >= cutoff:
                        results.append(evt)
            except (json.JSONDecodeError, ValueError):
                continue
    except Exception:
        pass
    return results


def get_audit_summary(hours: int = 24):
    """GET /api/v1/workstation/audit-log/summary — deterministic analytics.

    Computes summary cuts over the audit log within the given time window:
      - category_counts: events per category
      - asset_counts: events per asset
      - condition_type_counts: trigger mix (price_move vs signal_change vs regime_shift)
      - delivery_health: sent/failed/rate
      - noisy_conditions: condition IDs that fired more than once
      - most_recent: latest event per category
      - hourly_volume: event count per hour (last 12 buckets max)
    """
    hours = min(hours, 168)  # cap at 7 days
    events = _read_all_within(hours)

    if not events:
        return JSONResponse(content={
            "window_hours": hours,
            "total_events": 0,
            "category_counts": {},
            "asset_counts": {},
            "condition_type_counts": {},
            "delivery_health": {"sent": 0, "failed": 0, "rate": None},
            "noisy_conditions": [],
            "most_recent": {},
            "hourly_volume": [],
        })

    # ── Category counts ──
    category_counts: dict[str, int] = {}
    for evt in events:
        cat = evt.get("category", "unknown")
        category_counts[cat] = category_counts.get(cat, 0) + 1

    # ── Asset counts ──
    asset_counts: dict[str, int] = {}
    for evt in events:
        asset = evt.get("asset")
        if asset:
            asset_counts[asset] = asset_counts.get(asset, 0) + 1

    # ── Condition type counts (from watch_triggered payloads) ──
    condition_type_counts: dict[str, int] = {}
    for evt in events:
        if evt.get("category") == "watch_triggered":
            payload = evt.get("payload", {})
            ctype = payload.get("conditionType", "unknown")
            condition_type_counts[ctype] = condition_type_counts.get(ctype, 0) + 1

    # ── Delivery health ──
    sent = category_counts.get("delivery_sent", 0)
    failed = category_counts.get("delivery_failed", 0)
    total_delivery = sent + failed
    delivery_rate = round(sent / total_delivery, 3) if total_delivery > 0 else None

    # ── Noisy conditions (conditionId that appears 2+ times in triggers) ──
    cond_fire_counts: dict[str, int] = {}
    cond_assets: dict[str, str] = {}
    for evt in events:
        if evt.get("category") == "watch_triggered":
            cid = evt.get("conditionId")
            if cid:
                cond_fire_counts[cid] = cond_fire_counts.get(cid, 0) + 1
                if evt.get("asset"):
                    cond_assets[cid] = evt["asset"]
    noisy = [
        {"conditionId": cid, "fires": count, "asset": cond_assets.get(cid)}
        for cid, count in sorted(cond_fire_counts.items(), key=lambda x: -x[1])
        if count >= 2
    ][:10]

    # ── Most recent per category ──
    most_recent: dict[str, dict[str, Any]] = {}
    for evt in reversed(events):
        cat = evt.get("category", "unknown")
        if cat not in most_recent:
            most_recent[cat] = {
                "ts": evt.get("ts"),
                "asset": evt.get("asset"),
                "summary": (evt.get("payload") or {}).get("reason")
                    or (evt.get("payload") or {}).get("summary")
                    or (evt.get("payload") or {}).get("error"),
            }

    # ── Hourly volume (last 12 buckets max) ──
    now_ts = datetime.now(timezone.utc).timestamp()
    bucket_count = min(12, hours)
    bucket_size = 3600  # 1 hour
    buckets = [0] * bucket_count
    for evt in events:
        ts_str = evt.get("ts", "")
        if not ts_str:
            continue
        try:
            ts_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            age_seconds = now_ts - ts_dt.timestamp()
            bucket_idx = int(age_seconds / bucket_size)
            if 0 <= bucket_idx < bucket_count:
                buckets[bucket_idx] += 1
        except (ValueError, OverflowError):
            continue

    # buckets[0] = most recent hour, buckets[-1] = oldest
    hourly_volume = [
        {"hours_ago": i, "count": buckets[i]}
        for i in range(bucket_count)
    ]

    return JSONResponse(content={
        "window_hours": hours,
        "total_events": len(events),
        "category_counts": category_counts,
        "asset_counts": dict(sorted(asset_counts.items(), key=lambda x: -x[1])),
        "condition_type_counts": condition_type_counts,
        "delivery_health": {"sent": sent, "failed": failed, "rate": delivery_rate},
        "noisy_conditions": noisy,
        "most_recent": most_recent,
        "hourly_volume": hourly_volume,
    })


# ── Fatigue Analysis (Bundle 73) ──


def get_fatigue_analysis(hours: int = 24):
    """GET /api/v1/workstation/audit-log/fatigue — alert fatigue detection.

    Applies deterministic rules over the audit log to identify noisy
    conditions, over-triggering patterns, and generate actionable
    threshold adjustment suggestions.

    Rules:
      1. rapid_refire: conditionId fires 3+ times in window
      2. asset_concentration: single asset > 40% of triggers
      3. high_volume: > 20 triggers in 24h (scaled by window)
      4. delivery_failures: > 20% delivery failure rate
      5. expire_without_trigger: many conditions expiring unused
      6. type_imbalance: > 80% triggers from one condition type

    Each finding has: rule, severity, asset, message, suggestion.
    """
    hours = min(hours, 168)
    events = _read_all_within(hours)

    findings: list[dict[str, Any]] = []

    if not events:
        return JSONResponse(content={
            "window_hours": hours,
            "total_events": 0,
            "findings": [],
            "health": "no_data",
        })

    # Classify events
    triggers = [e for e in events if e.get("category") == "watch_triggered"]
    expired = [e for e in events if e.get("category") == "watch_expired"]
    delivery_sent = [e for e in events if e.get("category") == "delivery_sent"]
    delivery_failed = [e for e in events if e.get("category") == "delivery_failed"]

    # ── Rule 1: Rapid re-fire ──
    cond_fires: dict[str, list[dict[str, Any]]] = {}
    for evt in triggers:
        cid = evt.get("conditionId")
        if cid:
            cond_fires.setdefault(cid, []).append(evt)

    for cid, fires in cond_fires.items():
        if len(fires) >= 3:
            asset = fires[0].get("asset", "unknown")
            ctype = (fires[0].get("payload") or {}).get("conditionType", "unknown")
            findings.append({
                "rule": "rapid_refire",
                "severity": "high" if len(fires) >= 5 else "medium",
                "conditionId": cid,
                "asset": asset,
                "conditionType": ctype,
                "fires": len(fires),
                "message": f"Condition {cid} ({asset}, {ctype}) fired {len(fires)}× in {hours}h",
                "suggestion": f"This condition is re-triggering repeatedly. Consider dismissing it, or tighten the threshold (e.g., widen the price band or raise the convergence delta).",
            })

    # ── Rule 2: Asset concentration ──
    if len(triggers) >= 5:
        asset_trigger_counts: dict[str, int] = {}
        for evt in triggers:
            a = evt.get("asset")
            if a:
                asset_trigger_counts[a] = asset_trigger_counts.get(a, 0) + 1
        total_triggers = len(triggers)
        for asset, count in sorted(asset_trigger_counts.items(), key=lambda x: -x[1]):
            ratio = count / total_triggers
            if ratio > 0.4:
                findings.append({
                    "rule": "asset_concentration",
                    "severity": "medium",
                    "asset": asset,
                    "fires": count,
                    "ratio": round(ratio, 2),
                    "message": f"{asset} accounts for {count}/{total_triggers} triggers ({round(ratio * 100)}%)",
                    "suggestion": f"Most alerts are coming from {asset}. Review {asset}-specific watch conditions — some may be too loose or redundant.",
                })
                break  # only report top

    # ── Rule 3: High volume ──
    volume_threshold = max(20, int(20 * (hours / 24)))  # scale by window
    if len(triggers) > volume_threshold:
        findings.append({
            "rule": "high_volume",
            "severity": "high" if len(triggers) > volume_threshold * 2 else "medium",
            "fires": len(triggers),
            "threshold": volume_threshold,
            "message": f"{len(triggers)} triggers in {hours}h exceeds the expected volume of ~{volume_threshold}",
            "suggestion": "Alert volume is high. Consider switching to the conservative sensitivity profile, or review active conditions to remove low-value ones.",
        })

    # ── Rule 4: Delivery failures ──
    total_delivery = len(delivery_sent) + len(delivery_failed)
    if total_delivery >= 3 and len(delivery_failed) > 0:
        fail_ratio = len(delivery_failed) / total_delivery
        if fail_ratio > 0.2:
            findings.append({
                "rule": "delivery_failures",
                "severity": "high" if fail_ratio > 0.5 else "medium",
                "sent": len(delivery_sent),
                "failed": len(delivery_failed),
                "ratio": round(fail_ratio, 2),
                "message": f"{len(delivery_failed)}/{total_delivery} deliveries failed ({round(fail_ratio * 100)}%)",
                "suggestion": "Telegram delivery is unreliable. Check bot token, chat ID, and network connectivity. Failed deliveries mean triggered alerts are being lost.",
            })

    # ── Rule 5: Expire without trigger ──
    if len(expired) >= 3 and len(triggers) < len(expired):
        findings.append({
            "rule": "expire_without_trigger",
            "severity": "low",
            "expired": len(expired),
            "triggered": len(triggers),
            "message": f"{len(expired)} conditions expired vs {len(triggers)} triggered — most watches are expiring unused",
            "suggestion": "Many conditions expire before firing. Consider increasing TTL (currently configured in threshold settings), or lowering thresholds so conditions actually fire before they expire.",
        })

    # ── Rule 6: Type imbalance ──
    if len(triggers) >= 5:
        type_counts: dict[str, int] = {}
        for evt in triggers:
            ctype = (evt.get("payload") or {}).get("conditionType", "unknown")
            type_counts[ctype] = type_counts.get(ctype, 0) + 1
        for ctype, count in type_counts.items():
            ratio = count / len(triggers)
            if ratio > 0.8 and len(type_counts) > 1:
                findings.append({
                    "rule": "type_imbalance",
                    "severity": "low",
                    "conditionType": ctype,
                    "ratio": round(ratio, 2),
                    "message": f"{round(ratio * 100)}% of triggers are {ctype} — other condition types are barely firing",
                    "suggestion": f"Almost all triggers are {ctype}. If you want more balanced monitoring, consider adding signal_change or regime_shift conditions for better coverage.",
                })
                break

    # Overall health assessment
    high_count = sum(1 for f in findings if f["severity"] == "high")
    medium_count = sum(1 for f in findings if f["severity"] == "medium")
    if high_count > 0:
        health = "unhealthy"
    elif medium_count > 0:
        health = "noisy"
    elif len(findings) > 0:
        health = "minor_issues"
    elif len(triggers) > 0:
        health = "healthy"
    else:
        health = "quiet"

    return JSONResponse(content={
        "window_hours": hours,
        "total_events": len(events),
        "trigger_count": len(triggers),
        "findings": findings,
        "health": health,
    })


# ── Retention / Pruning (Bundle 76) ──


async def prune_audit_log(request: Request):
    """POST /api/v1/workstation/audit-log/prune — explicit retention prune.

    Removes events older than RETENTION_DAYS (default 14).
    Returns stats about what was pruned. Safe to call from cron or manually.
    Optional body: {"retention_days": N} to override (capped at 90).
    """
    retention = RETENTION_DAYS
    try:
        body = await request.json()
        if isinstance(body, dict) and "retention_days" in body:
            retention = max(1, min(90, int(body["retention_days"])))
    except Exception:
        pass  # Use default if no body or invalid

    result = _prune_to_retention(retention)
    return JSONResponse(content=result)


# ── Per-Asset / Per-Category Drilldown (Bundle 77) ──


def _get_asset_category(asset: str) -> str | None:
    """Look up category for an asset symbol."""
    return ASSET_CATEGORIES.get(asset.upper()) if asset else None


def _get_category_assets(category: str) -> list[str]:
    """Get all assets in a category."""
    cat_upper = category.upper()
    # Normalize common aliases
    cat_map = {"L1": "L1", "L2": "L2", "DEFI": "DeFi", "MEME": "Meme",
               "INFRA": "Infra", "EXCHANGE": "Exchange"}
    normalized = cat_map.get(cat_upper, category)
    return [a for a, c in ASSET_CATEGORIES.items() if c == normalized]


def get_alert_drilldown(
    asset: str | None = None,
    category: str | None = None,
    hours: int = 24,
):
    """GET /api/v1/workstation/audit-log/drilldown — per-asset or per-category cuts.

    Supports:
      ?asset=BTC&hours=24 — all audit events for BTC
      ?category=Meme&hours=24 — all audit events for Meme assets
      ?hours=48 — category-level breakdown (no filter)

    Returns per-entity trigger/expire/delivery counts, condition types,
    noisy conditions, and recent events.
    """
    hours = min(hours, 168)
    events = _read_all_within(hours)

    # Filter events by asset or category
    if asset:
        asset = asset.upper()
        filtered = [e for e in events if (e.get("asset") or "").upper() == asset]
        scope_label = asset
        scope_category = _get_asset_category(asset)
    elif category:
        cat_assets = set(_get_category_assets(category))
        filtered = [e for e in events if (e.get("asset") or "").upper() in cat_assets]
        scope_label = category
        scope_category = category
    else:
        filtered = events
        scope_label = "all"
        scope_category = None

    if not filtered:
        # Build category breakdown even when no filtered results
        cat_breakdown = _build_category_breakdown(events) if not asset and not category else {}
        return JSONResponse(content={
            "window_hours": hours,
            "scope": scope_label,
            "scope_category": scope_category,
            "total_events": 0,
            "category_counts": {},
            "condition_types": {},
            "noisy_conditions": [],
            "recent_events": [],
            "category_breakdown": cat_breakdown,
        })

    # Category counts for filtered events
    cat_counts: dict[str, int] = {}
    for evt in filtered:
        c = evt.get("category", "unknown")
        cat_counts[c] = cat_counts.get(c, 0) + 1

    # Condition type counts (from triggers)
    ctype_counts: dict[str, int] = {}
    cond_fire_map: dict[str, int] = {}
    cond_asset_map: dict[str, str] = {}
    cond_type_map: dict[str, str] = {}
    for evt in filtered:
        if evt.get("category") == "watch_triggered":
            payload = evt.get("payload", {})
            ct = payload.get("conditionType", "unknown")
            ctype_counts[ct] = ctype_counts.get(ct, 0) + 1
            cid = evt.get("conditionId")
            if cid:
                cond_fire_map[cid] = cond_fire_map.get(cid, 0) + 1
                cond_asset_map[cid] = evt.get("asset", "")
                cond_type_map[cid] = ct

    # Noisy conditions (2+ fires)
    noisy = [
        {
            "conditionId": cid,
            "fires": count,
            "asset": cond_asset_map.get(cid),
            "conditionType": cond_type_map.get(cid),
        }
        for cid, count in sorted(cond_fire_map.items(), key=lambda x: -x[1])
        if count >= 2
    ][:10]

    # Recent events (last 10)
    recent = []
    for evt in reversed(filtered):
        if len(recent) >= 10:
            break
        recent.append({
            "ts": evt.get("ts"),
            "category": evt.get("category"),
            "asset": evt.get("asset"),
            "conditionId": evt.get("conditionId"),
            "summary": (evt.get("payload") or {}).get("reason")
                or (evt.get("payload") or {}).get("summary")
                or (evt.get("payload") or {}).get("error")
                or (evt.get("payload") or {}).get("action"),
        })

    # Delivery health for this scope
    sent = cat_counts.get("delivery_sent", 0)
    failed = cat_counts.get("delivery_failed", 0)
    total_del = sent + failed
    del_rate = round(sent / total_del, 3) if total_del > 0 else None

    # Category breakdown (when no filter — shows which categories generate most)
    cat_breakdown = _build_category_breakdown(events) if not asset else {}

    return JSONResponse(content={
        "window_hours": hours,
        "scope": scope_label,
        "scope_category": scope_category,
        "total_events": len(filtered),
        "category_counts": cat_counts,
        "condition_types": ctype_counts,
        "delivery_health": {"sent": sent, "failed": failed, "rate": del_rate},
        "noisy_conditions": noisy,
        "recent_events": recent,
        "category_breakdown": cat_breakdown,
    })


def _build_category_breakdown(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Build per-category trigger/expire counts from all events."""
    cat_triggers: dict[str, int] = {}
    cat_expired: dict[str, int] = {}
    cat_total: dict[str, int] = {}

    for evt in events:
        asset = (evt.get("asset") or "").upper()
        cat = ASSET_CATEGORIES.get(asset)
        if not cat:
            continue
        cat_total[cat] = cat_total.get(cat, 0) + 1
        if evt.get("category") == "watch_triggered":
            cat_triggers[cat] = cat_triggers.get(cat, 0) + 1
        elif evt.get("category") == "watch_expired":
            cat_expired[cat] = cat_expired.get(cat, 0) + 1

    result = {}
    for cat in sorted(cat_total, key=lambda c: -cat_total[c]):
        result[cat] = {
            "total_events": cat_total[cat],
            "triggers": cat_triggers.get(cat, 0),
            "expired": cat_expired.get(cat, 0),
        }
    return result


# ── Condition Effectiveness Scoring (Bundle 77) ──


def get_condition_effectiveness(hours: int = 48):
    """GET /api/v1/workstation/audit-log/effectiveness — condition quality baseline.

    Deterministic scoring of each condition seen in the audit window:
      - fire_count: how many times it triggered
      - delivered: how many triggers led to successful delivery
      - expired_unused: whether it expired without ever triggering
      - refire_rate: fires per 24h (normalized)
      - effectiveness: "useful" | "noisy" | "wasted" | "unknown"

    A condition is:
      - "useful":  1-2 fires with good delivery, or single decisive trigger
      - "noisy":   3+ fires in window (rapid refire pattern)
      - "wasted":  expired without any trigger
      - "unknown": too few events to score

    Also provides asset-level and category-level quality summaries.
    """
    hours = min(hours, 168)
    events = _read_all_within(hours)

    if not events:
        return JSONResponse(content={
            "window_hours": hours,
            "total_conditions": 0,
            "conditions": [],
            "asset_quality": {},
            "category_quality": {},
            "summary": {"useful": 0, "noisy": 0, "wasted": 0, "unknown": 0},
        })

    # Index events by conditionId
    cond_triggers: dict[str, list[dict]] = {}
    cond_deliveries: dict[str, int] = {}
    cond_failures: dict[str, int] = {}
    cond_expired: set[str] = set()
    cond_assets: dict[str, str] = {}
    cond_types: dict[str, str] = {}
    cond_first_ts: dict[str, str] = {}
    cond_last_ts: dict[str, str] = {}

    for evt in events:
        cid = evt.get("conditionId")
        if not cid:
            continue
        cat = evt.get("category")
        ts = evt.get("ts", "")

        # Track first/last seen
        if cid not in cond_first_ts or ts < cond_first_ts[cid]:
            cond_first_ts[cid] = ts
        if cid not in cond_last_ts or ts > cond_last_ts[cid]:
            cond_last_ts[cid] = ts

        if cat == "watch_triggered":
            cond_triggers.setdefault(cid, []).append(evt)
            if evt.get("asset"):
                cond_assets[cid] = evt["asset"]
            payload = evt.get("payload", {})
            if payload.get("conditionType"):
                cond_types[cid] = payload["conditionType"]
        elif cat == "delivery_sent":
            cond_deliveries[cid] = cond_deliveries.get(cid, 0) + 1
            if evt.get("asset"):
                cond_assets[cid] = evt["asset"]
        elif cat == "delivery_failed":
            cond_failures[cid] = cond_failures.get(cid, 0) + 1
        elif cat == "watch_expired":
            cond_expired.add(cid)
            if evt.get("asset"):
                cond_assets[cid] = evt["asset"]

    # Score each condition
    all_cids = set(cond_triggers) | cond_expired | set(cond_deliveries) | set(cond_failures)
    conditions: list[dict[str, Any]] = []
    summary = {"useful": 0, "noisy": 0, "wasted": 0, "unknown": 0}

    for cid in all_cids:
        fires = len(cond_triggers.get(cid, []))
        delivered = cond_deliveries.get(cid, 0)
        failed = cond_failures.get(cid, 0)
        expired = cid in cond_expired
        asset = cond_assets.get(cid)
        ctype = cond_types.get(cid, "unknown")

        # Normalized refire rate (fires per 24h)
        refire_rate = round(fires * 24 / max(hours, 1), 2) if fires > 0 else 0

        # Effectiveness classification
        if fires == 0 and expired:
            effectiveness = "wasted"
        elif fires >= 3:
            effectiveness = "noisy"
        elif fires >= 1 and fires <= 2:
            effectiveness = "useful"
        else:
            effectiveness = "unknown"

        conditions.append({
            "conditionId": cid,
            "asset": asset,
            "category": ASSET_CATEGORIES.get((asset or "").upper()),
            "conditionType": ctype,
            "fires": fires,
            "delivered": delivered,
            "failed": failed,
            "expired": expired,
            "refire_rate": refire_rate,
            "effectiveness": effectiveness,
        })
        summary[effectiveness] = summary.get(effectiveness, 0) + 1

    # Sort: noisy first, then wasted, then useful, then unknown
    eff_order = {"noisy": 0, "wasted": 1, "useful": 2, "unknown": 3}
    conditions.sort(key=lambda c: (eff_order.get(c["effectiveness"], 9), -c["fires"]))

    # Asset-level quality
    asset_quality: dict[str, dict[str, int]] = {}
    for c in conditions:
        a = c.get("asset")
        if not a:
            continue
        aq = asset_quality.setdefault(a, {"useful": 0, "noisy": 0, "wasted": 0, "total": 0})
        aq["total"] += 1
        eff = c["effectiveness"]
        if eff in aq:
            aq[eff] += 1

    # Category-level quality
    category_quality: dict[str, dict[str, int]] = {}
    for c in conditions:
        cat = c.get("category")
        if not cat:
            continue
        cq = category_quality.setdefault(cat, {"useful": 0, "noisy": 0, "wasted": 0, "total": 0})
        cq["total"] += 1
        eff = c["effectiveness"]
        if eff in cq:
            cq[eff] += 1

    return JSONResponse(content={
        "window_hours": hours,
        "total_conditions": len(conditions),
        "conditions": conditions[:30],  # cap at top 30
        "asset_quality": dict(sorted(asset_quality.items(),
                                      key=lambda x: -(x[1].get("noisy", 0) + x[1].get("wasted", 0)))),
        "category_quality": dict(sorted(category_quality.items(),
                                         key=lambda x: -(x[1].get("noisy", 0) + x[1].get("wasted", 0)))),
        "summary": summary,
    })
