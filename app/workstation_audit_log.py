"""Workstation audit log — JSONL append-only event store.

Bundle 68: Captures alert-related events for auditability.
Events are never modified — they form an immutable timeline.

Event categories:
  - watch_triggered: A watch condition evaluated to true
  - watch_expired: A watch condition expired by TTL
  - delivery_sent: Telegram notification successfully delivered
  - delivery_failed: Telegram notification failed
  - significant_change: A significant market change was detected

Endpoints:
  GET /api/v1/workstation/audit-log?limit=50&category=...
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

ALERTS_DIR = Path(os.getenv("ROUTER_ALERT_DIR", "/home/eventedge/alerts"))
AUDIT_FILE = ALERTS_DIR / "workstation_audit_log.jsonl"

MAX_READ_LIMIT = 200
VALID_CATEGORIES = {
    "watch_triggered",
    "watch_expired",
    "delivery_sent",
    "delivery_failed",
    "significant_change",
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
    }


# ── Endpoints ──

# Categories accepted via POST from clients (subset — delivery/trigger are server-only)
CLIENT_CATEGORIES = {"significant_change"}


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
