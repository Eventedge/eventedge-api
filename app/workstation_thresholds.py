"""Workstation threshold config persistence — file-backed JSON store.

Stores configurable watch/alert thresholds for the EdgeBlocks Agent Workstation.
Atomic writes via tempfile + rename.

Bundle 69: configurable thresholds / watch tuning.

Endpoints:
  GET  /api/v1/workstation/thresholds   — load threshold config
  POST /api/v1/workstation/thresholds   — save threshold config (full replace)
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
THRESHOLDS_FILE = ALERTS_DIR / "workstation_thresholds.json"

STORE_VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_thresholds() -> dict[str, Any]:
    if not THRESHOLDS_FILE.exists():
        return {
            "version": STORE_VERSION,
            "activeProfile": "default",
            "overrides": {},
            "updatedAt": _now_iso(),
        }
    try:
        return json.loads(THRESHOLDS_FILE.read_text())
    except Exception:
        return {
            "version": STORE_VERSION,
            "activeProfile": "default",
            "overrides": {},
            "updatedAt": _now_iso(),
        }


def _save_thresholds(data: dict[str, Any]) -> None:
    ALERTS_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(ALERTS_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, str(THRESHOLDS_FILE))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


async def get_thresholds(_request: Request) -> JSONResponse:
    """GET /api/v1/workstation/thresholds"""
    data = _load_thresholds()
    return JSONResponse(data)


async def post_thresholds(request: Request) -> JSONResponse:
    """POST /api/v1/workstation/thresholds"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": "Expected object"}, status_code=400)

    # Validate version
    if body.get("version") != STORE_VERSION:
        return JSONResponse({"error": f"Version mismatch (expected {STORE_VERSION})"}, status_code=400)

    body["updatedAt"] = _now_iso()
    _save_thresholds(body)
    return JSONResponse({"ok": True, "updatedAt": body["updatedAt"]})
