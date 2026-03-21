"""Workstation proposal outcome attribution — file-backed JSON store.

Stores outcome attribution records linking proposal source/conviction to
trade results (win/loss, PnL, close reason). Persists across sessions so
adaptive proposal quality can be measured over time.

Endpoints:
  GET  /api/v1/workstation/proposal-outcomes  — load outcome store
  PUT  /api/v1/workstation/proposal-outcomes  — save outcome records (full replace)
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
OUTCOMES_FILE = ALERTS_DIR / "workstation_proposal_outcomes.json"

OUTCOMES_VERSION = 1
MAX_RECORDS = 200


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_outcomes() -> dict[str, Any]:
    if not OUTCOMES_FILE.exists():
        return {"version": OUTCOMES_VERSION, "records": [], "updatedAt": _now_iso()}
    try:
        return json.loads(OUTCOMES_FILE.read_text())
    except Exception:
        return {"version": OUTCOMES_VERSION, "records": [], "updatedAt": _now_iso()}


def _save_outcomes(data: dict[str, Any]) -> None:
    data["updatedAt"] = _now_iso()
    records = data.get("records", [])
    if len(records) > MAX_RECORDS:
        data["records"] = records[-MAX_RECORDS:]  # Keep most recent

    raw = json.dumps(data, indent=2, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(dir=str(ALERTS_DIR), suffix=".tmp")
    try:
        os.write(fd, raw.encode())
        os.close(fd)
        os.rename(tmp, str(OUTCOMES_FILE))
    except Exception:
        os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


VALID_SOURCES = {"pattern", "synth", "deter"}
VALID_CONVICTIONS = {"low", "medium", "high"}
VALID_OUTCOMES = {"pending", "approved", "rejected"}


def _validate_record(r: dict) -> bool:
    """Validate a single outcome record."""
    if not isinstance(r, dict):
        return False
    if not isinstance(r.get("proposalId"), str):
        return False
    if r.get("source") not in VALID_SOURCES:
        return False
    # conviction is nullable
    if r.get("conviction") is not None and r["conviction"] not in VALID_CONVICTIONS:
        return False
    if r.get("outcome") not in VALID_OUTCOMES:
        return False
    return True


def get_proposal_outcomes():
    """GET /api/v1/workstation/proposal-outcomes"""
    return JSONResponse(content=_load_outcomes())


async def put_proposal_outcomes(request: Request):
    """PUT /api/v1/workstation/proposal-outcomes — full replace with validation"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(content={"error": "Invalid JSON"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse(content={"error": "Expected object"}, status_code=400)

    raw_records = body.get("records", [])
    if not isinstance(raw_records, list):
        return JSONResponse(content={"error": "records must be array"}, status_code=400)

    valid = [r for r in raw_records if _validate_record(r)]

    data = {
        "version": OUTCOMES_VERSION,
        "records": valid,
    }
    _save_outcomes(data)
    return JSONResponse(content={"ok": True, "stored": len(valid), "updatedAt": data.get("updatedAt")})
