"""P5-ALERTS-003: Admin alert-settings read/write.

Reads and writes router_alert_prefs.json — the same file used by the bot
and the P5-ALERTS-001 tick script.  Atomic writes via tempfile + rename.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PREFS_FILE = Path(
    os.getenv("ROUTER_ALERT_PREFS_FILE",
              "/home/eventedge/alerts/router_alert_prefs.json")
)

ALL_ASSETS = ["BTC", "ETH", "SOL", "HYPE"]
ALL_TRIGGERS = ["NEW_ENTRANT", "SCORE_SPIKE", "REGIME_FLIP", "COVERAGE_DROP"]

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "assets": ["BTC"],
    "triggers": list(ALL_TRIGGERS),
    "max_per_hour": 5,
    "paused_until": None,
    "channel_telegram": True,
}


# ---------------------------------------------------------------------------
#  File I/O (same atomic pattern as bot.py)
# ---------------------------------------------------------------------------

def _load_all() -> dict[str, dict]:
    try:
        return json.loads(PREFS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_all(data: dict[str, dict]) -> None:
    PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(PREFS_FILE.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, str(PREFS_FILE))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _get_prefs(user_id: str) -> dict:
    all_prefs = _load_all()
    prefs = all_prefs.get(str(user_id))
    if prefs is None:
        return dict(DEFAULTS)
    merged = dict(DEFAULTS)
    merged.update(prefs)
    return merged


# ---------------------------------------------------------------------------
#  Builders (called from main.py)
# ---------------------------------------------------------------------------

def build_alert_settings_list() -> dict[str, Any]:
    """GET — list all subscriber prefs."""
    all_prefs = _load_all()
    now = datetime.now(timezone.utc).isoformat()
    subscribers = []
    for uid, prefs in all_prefs.items():
        merged = dict(DEFAULTS)
        merged.update(prefs)
        merged["user_id"] = uid
        subscribers.append(merged)
    return {
        "ok": True,
        "generated_at": now,
        "subscribers": subscribers,
        "total": len(subscribers),
        "all_assets": ALL_ASSETS,
        "all_triggers": ALL_TRIGGERS,
        "defaults": DEFAULTS,
    }


def build_alert_settings_get(user_id: str) -> dict[str, Any]:
    """GET — single user prefs."""
    now = datetime.now(timezone.utc).isoformat()
    prefs = _get_prefs(user_id)
    prefs["user_id"] = user_id
    return {
        "ok": True,
        "generated_at": now,
        "settings": prefs,
        "all_assets": ALL_ASSETS,
        "all_triggers": ALL_TRIGGERS,
    }


def build_alert_settings_update(user_id: str, body: dict) -> dict[str, Any]:
    """POST — update one user's prefs.  Returns saved state."""
    now = datetime.now(timezone.utc).isoformat()
    all_prefs = _load_all()
    current = dict(DEFAULTS)
    current.update(all_prefs.get(str(user_id), {}))

    errors: list[str] = []

    # Validate & apply fields
    if "enabled" in body:
        current["enabled"] = bool(body["enabled"])

    if "assets" in body:
        raw = body["assets"]
        if isinstance(raw, list):
            valid = [a.upper() for a in raw if a.upper() in ALL_ASSETS]
            if not valid:
                errors.append("assets: at least one valid asset required")
            else:
                current["assets"] = valid
        else:
            errors.append("assets: must be a list")

    if "triggers" in body:
        raw = body["triggers"]
        if isinstance(raw, list):
            valid = [t for t in raw if t in ALL_TRIGGERS]
            if not valid:
                errors.append("triggers: at least one valid trigger required")
            else:
                current["triggers"] = valid
        else:
            errors.append("triggers: must be a list")

    if "max_per_hour" in body:
        mph = body["max_per_hour"]
        if isinstance(mph, int) and 1 <= mph <= 30:
            current["max_per_hour"] = mph
        else:
            errors.append("max_per_hour: integer 1-30")

    if "channel_telegram" in body:
        current["channel_telegram"] = bool(body["channel_telegram"])

    # Admin quick actions (ALERTS-POLISH-001)
    if "paused_until" in body:
        pu = body["paused_until"]
        if pu is None:
            current["paused_until"] = None
        else:
            try:
                current["paused_until"] = int(pu)
            except (ValueError, TypeError):
                errors.append("paused_until: must be int or null")

    if "fail_streak" in body:
        try:
            fs = int(body["fail_streak"])
            current["fail_streak"] = max(0, fs)
            if fs == 0:
                current.pop("last_fail_ts", None)
        except (ValueError, TypeError):
            errors.append("fail_streak: must be int")

    if errors:
        return {"ok": False, "generated_at": now, "errors": errors}

    all_prefs[str(user_id)] = current
    _save_all(all_prefs)

    current["user_id"] = user_id
    return {
        "ok": True,
        "generated_at": now,
        "settings": current,
    }
