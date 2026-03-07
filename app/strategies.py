"""SERVER-STRATEGIES-001(A): File-backed strategy CRUD + import/export.

Stores strategies in /home/eventedge/alerts/strategies.json.
Atomic writes via tempfile + rename. No DB required.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response

ALERTS_DIR = Path(os.getenv("ROUTER_ALERT_DIR", "/home/eventedge/alerts"))
STRATEGIES_FILE = ALERTS_DIR / "strategies.json"

MAX_STRATEGIES = 100
MAX_NAME_LEN = 80
MAX_PAYLOAD_SIZE = 50_000  # bytes when serialized

VALID_PAYLOAD_KEYS = {"asset_defaults", "preset_id", "family_filter", "pinned", "hidden"}
VALID_SOURCES = {"web", "bot", "import", "api", "template"}


# ---------------------------------------------------------------------------
# File I/O (atomic)
# ---------------------------------------------------------------------------

def _load_store() -> dict[str, Any]:
    """Load the strategies store from disk. Returns empty store if missing."""
    if not STRATEGIES_FILE.exists():
        return {"version": 1, "items": []}
    try:
        data = json.loads(STRATEGIES_FILE.read_text())
        if not isinstance(data, dict) or "items" not in data:
            return {"version": 1, "items": []}
        return data
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "items": []}


def _save_store(store: dict[str, Any]) -> None:
    """Atomic write: tempfile + rename."""
    STRATEGIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(store, indent=2, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(dir=str(STRATEGIES_FILE.parent), suffix=".tmp")
    try:
        os.write(fd, raw.encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        os.rename(tmp, str(STRATEGIES_FILE))
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _store_etag() -> str | None:
    try:
        st = STRATEGIES_FILE.stat()
        raw = f"{st.st_mtime_ns}:{st.st_size}".encode()
        return f'W/"{hashlib.md5(raw).hexdigest()}"'
    except OSError:
        return None


def _store_mtime() -> datetime | None:
    try:
        return datetime.fromtimestamp(STRATEGIES_FILE.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _cache_headers() -> dict[str, str]:
    headers: dict[str, str] = {"Cache-Control": "no-store"}
    etag = _store_etag()
    mtime = _store_mtime()
    if etag:
        headers["ETag"] = etag
    if mtime:
        headers["Last-Modified"] = format_datetime(mtime, usegmt=True)
    return headers


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_payload(payload: Any) -> str | None:
    """Validate strategy payload shape. Returns error string or None."""
    if not isinstance(payload, dict):
        return "payload must be a dict"
    raw = json.dumps(payload, ensure_ascii=False)
    if len(raw) > MAX_PAYLOAD_SIZE:
        return f"payload too large ({len(raw)} bytes, max {MAX_PAYLOAD_SIZE})"
    # Check known keys are correct types when present
    ad = payload.get("asset_defaults")
    if ad is not None and not isinstance(ad, dict):
        return "payload.asset_defaults must be a dict"
    pid = payload.get("preset_id")
    if pid is not None and not isinstance(pid, str):
        return "payload.preset_id must be a string"
    ff = payload.get("family_filter")
    if ff is not None and not isinstance(ff, (str, type(None))):
        return "payload.family_filter must be a string or null"
    for key in ("pinned", "hidden"):
        val = payload.get(key)
        if val is not None and not isinstance(val, dict):
            return f"payload.{key} must be a dict (asset:horizon -> [feature_ids])"
    return None


def _validate_name(name: Any) -> str | None:
    if not name or not isinstance(name, str):
        return "name is required"
    if len(name.strip()) == 0:
        return "name cannot be empty"
    if len(name) > MAX_NAME_LEN:
        return f"name too long (max {MAX_NAME_LEN} chars)"
    return None


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def build_strategies_list(request: Request) -> JSONResponse:
    """GET /api/v1/strategies — list all strategies."""
    store = _load_store()
    return JSONResponse(
        content={
            "ok": True,
            "generated_at": _now_iso(),
            "count": len(store["items"]),
            "items": store["items"],
        },
        headers=_cache_headers(),
    )


def build_strategy_get(request: Request, strategy_id: str) -> JSONResponse:
    """GET /api/v1/strategies/{id} — get one strategy."""
    store = _load_store()
    for item in store["items"]:
        if item["id"] == strategy_id:
            return JSONResponse(
                content={"ok": True, "generated_at": _now_iso(), "item": item},
                headers=_cache_headers(),
            )
    return JSONResponse(
        content={"ok": False, "generated_at": _now_iso(), "error": f"Strategy {strategy_id} not found"},
        status_code=404,
        headers={"Cache-Control": "no-store"},
    )


async def build_strategy_create(request: Request) -> JSONResponse:
    """POST /api/v1/strategies — create a new strategy."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            content={"ok": False, "error": "Invalid JSON body"},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )

    name = body.get("name")
    err = _validate_name(name)
    if err:
        return JSONResponse(
            content={"ok": False, "error": err},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )

    payload = body.get("payload", {})
    err = _validate_payload(payload)
    if err:
        return JSONResponse(
            content={"ok": False, "error": err},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )

    source = body.get("source", "api")
    if source not in VALID_SOURCES:
        source = "api"

    store = _load_store()
    if len(store["items"]) >= MAX_STRATEGIES:
        return JSONResponse(
            content={"ok": False, "error": f"Max {MAX_STRATEGIES} strategies reached"},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )

    now = _now_iso()
    item = {
        "id": str(uuid.uuid4()),
        "name": name.strip(),
        "created_at": now,
        "updated_at": now,
        "source": source,
        "payload": payload,
    }
    store["items"].append(item)
    _save_store(store)

    return JSONResponse(
        content={"ok": True, "generated_at": now, "item": item},
        status_code=201,
        headers={"Cache-Control": "no-store"},
    )


async def build_strategy_update(request: Request, strategy_id: str) -> JSONResponse:
    """PUT /api/v1/strategies/{id} — update a strategy."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            content={"ok": False, "error": "Invalid JSON body"},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )

    store = _load_store()
    target = None
    for item in store["items"]:
        if item["id"] == strategy_id:
            target = item
            break

    if not target:
        return JSONResponse(
            content={"ok": False, "error": f"Strategy {strategy_id} not found"},
            status_code=404,
            headers={"Cache-Control": "no-store"},
        )

    # Update name if provided
    if "name" in body:
        err = _validate_name(body["name"])
        if err:
            return JSONResponse(
                content={"ok": False, "error": err},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        target["name"] = body["name"].strip()

    # Update payload if provided
    if "payload" in body:
        err = _validate_payload(body["payload"])
        if err:
            return JSONResponse(
                content={"ok": False, "error": err},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        target["payload"] = body["payload"]

    # Update source if provided
    if "source" in body and body["source"] in VALID_SOURCES:
        target["source"] = body["source"]

    target["updated_at"] = _now_iso()
    _save_store(store)

    return JSONResponse(
        content={"ok": True, "generated_at": _now_iso(), "item": target},
        headers={"Cache-Control": "no-store"},
    )


def build_strategy_delete(strategy_id: str) -> JSONResponse:
    """DELETE /api/v1/strategies/{id} — delete a strategy."""
    store = _load_store()
    original_len = len(store["items"])
    store["items"] = [i for i in store["items"] if i["id"] != strategy_id]

    if len(store["items"]) == original_len:
        return JSONResponse(
            content={"ok": False, "error": f"Strategy {strategy_id} not found"},
            status_code=404,
            headers={"Cache-Control": "no-store"},
        )

    _save_store(store)
    return JSONResponse(
        content={"ok": True, "generated_at": _now_iso(), "deleted": strategy_id},
        headers={"Cache-Control": "no-store"},
    )


async def build_strategy_import(request: Request) -> JSONResponse:
    """POST /api/v1/strategies/import — import a strategy from JSON export."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            content={"ok": False, "error": "Invalid JSON body"},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )

    # Accept either a single export object or just the relevant fields
    name = body.get("name")
    err = _validate_name(name)
    if err:
        return JSONResponse(
            content={"ok": False, "error": f"Import failed: {err}"},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )

    payload = body.get("payload", {})
    err = _validate_payload(payload)
    if err:
        return JSONResponse(
            content={"ok": False, "error": f"Import failed: {err}"},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )

    store = _load_store()
    if len(store["items"]) >= MAX_STRATEGIES:
        return JSONResponse(
            content={"ok": False, "error": f"Max {MAX_STRATEGIES} strategies reached"},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )

    now = _now_iso()
    item = {
        "id": str(uuid.uuid4()),
        "name": name.strip(),
        "created_at": now,
        "updated_at": now,
        "source": "import",
        "payload": payload,
    }
    store["items"].append(item)
    _save_store(store)

    return JSONResponse(
        content={"ok": True, "generated_at": now, "item": item},
        status_code=201,
        headers={"Cache-Control": "no-store"},
    )


def build_strategy_export(strategy_id: str) -> JSONResponse:
    """GET /api/v1/strategies/{id}/export — export a strategy as JSON."""
    store = _load_store()
    for item in store["items"]:
        if item["id"] == strategy_id:
            export = {
                "name": item["name"],
                "payload": item["payload"],
                "exported_at": _now_iso(),
                "source_id": item["id"],
            }
            return JSONResponse(
                content={"ok": True, "generated_at": _now_iso(), "export": export},
                headers={"Cache-Control": "no-store"},
            )
    return JSONResponse(
        content={"ok": False, "generated_at": _now_iso(), "error": f"Strategy {strategy_id} not found"},
        status_code=404,
        headers={"Cache-Control": "no-store"},
    )
