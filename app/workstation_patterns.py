"""Workstation persistent patterns — file-backed JSON store.

Stores aggregate cross-session pattern insights derived from trade reviews.
Each pattern is a normalized insight with recency metadata (firstSeen, lastSeen,
tradeCount) so the workstation can apply recency gating and avoid stale overfitting.

Endpoints:
  GET  /api/v1/workstation/patterns  — load patterns
  PUT  /api/v1/workstation/patterns  — save patterns (full replace)
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

ALERTS_DIR = Path(os.getenv("ROUTER_ALERT_DIR", "/home/eventedge/alerts"))
PATTERNS_FILE = ALERTS_DIR / "workstation_patterns.json"

PATTERNS_VERSION = 1
MAX_PATTERNS = 40


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_patterns() -> dict[str, Any]:
    if not PATTERNS_FILE.exists():
        return {"version": PATTERNS_VERSION, "patterns": [], "updatedAt": _now_iso()}
    try:
        return json.loads(PATTERNS_FILE.read_text())
    except Exception:
        return {"version": PATTERNS_VERSION, "patterns": [], "updatedAt": _now_iso()}


def _save_patterns(data: dict[str, Any]) -> None:
    data["updatedAt"] = _now_iso()
    patterns = data.get("patterns", [])
    if len(patterns) > MAX_PATTERNS:
        data["patterns"] = patterns[:MAX_PATTERNS]

    raw = json.dumps(data, indent=2, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(dir=str(ALERTS_DIR), suffix=".tmp")
    try:
        os.write(fd, raw.encode())
        os.close(fd)
        os.rename(tmp, str(PATTERNS_FILE))
    except Exception:
        os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


VALID_CATEGORIES = {"strength", "weakness", "observation"}
VALID_CONFIDENCES = {"high", "medium", "low"}


def _validate_pattern(p: dict) -> bool:
    """Validate a single pattern entry."""
    if not isinstance(p, dict):
        return False
    if p.get("category") not in VALID_CATEGORIES:
        return False
    if p.get("confidence") not in VALID_CONFIDENCES:
        return False
    if not isinstance(p.get("text"), str) or len(p["text"]) < 5:
        return False
    return True


def get_patterns():
    """GET /api/v1/workstation/patterns"""
    return JSONResponse(content=_load_patterns())


async def put_patterns(request: Request):
    """PUT /api/v1/workstation/patterns — full replace with validation"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(content={"error": "Invalid JSON"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse(content={"error": "Expected object"}, status_code=400)

    raw_patterns = body.get("patterns", [])
    if not isinstance(raw_patterns, list):
        return JSONResponse(content={"error": "patterns must be array"}, status_code=400)

    # Filter to valid patterns only
    valid = [p for p in raw_patterns if _validate_pattern(p)]

    data = {
        "version": PATTERNS_VERSION,
        "patterns": valid,
    }
    _save_patterns(data)
    return JSONResponse(content={"ok": True, "stored": len(valid), "updatedAt": data.get("updatedAt")})
