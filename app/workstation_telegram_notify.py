"""Workstation watch condition Telegram notifications — Bundle 67.

Delivers triggered watch condition alerts via Telegram Bot API.
Uses httpx (already a dependency) with the raw Telegram HTTP API.

Deduplication: tracks sent condition IDs in a small file to prevent
double-sending when the cron evaluator re-reads a triggered condition
before the client syncs.

Environment:
  TELEGRAM_BOT_TOKEN: Bot API token (required for delivery)
  WATCH_NOTIFY_CHAT_ID: Telegram chat ID for notifications
    Falls back to ADMIN_CHAT_ID if not set.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("workstation.telegram_notify")

ALERTS_DIR = Path(os.getenv("ROUTER_ALERT_DIR", "/home/eventedge/alerts"))
SENT_FILE = ALERTS_DIR / "workstation_notify_sent.json"
MAX_SENT_IDS = 200  # Rolling limit on dedup tracking

TELEGRAM_API = "https://api.telegram.org"
SEND_TIMEOUT = 10.0


def _get_config() -> tuple[str | None, str | None]:
    """Return (bot_token, chat_id) or (None, None) if not configured."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("WATCH_NOTIFY_CHAT_ID") or os.getenv("ADMIN_CHAT_ID")
    return token, chat_id


def _load_sent_ids() -> set[str]:
    """Load previously sent condition IDs."""
    try:
        if SENT_FILE.exists():
            data = json.loads(SENT_FILE.read_text())
            return set(data.get("sent", []))
    except Exception:
        pass
    return set()


def _save_sent_ids(sent: set[str]) -> None:
    """Persist sent condition IDs (rolling limit)."""
    ids = sorted(sent)[-MAX_SENT_IDS:]
    try:
        SENT_FILE.write_text(json.dumps({"sent": ids, "updatedAt": datetime.now(timezone.utc).isoformat()}))
    except Exception as e:
        logger.warning("Failed to save sent IDs: %s", e)


def _format_message(detail: dict[str, Any], condition: dict[str, Any] | None = None) -> str:
    """Format a trigger detail into a Telegram message.

    Uses Markdown parse mode. Keeps messages concise and structured.
    """
    asset = detail.get("asset", "?")
    cond_type = detail.get("type", "?")
    reason = detail.get("reason", "Condition triggered")

    # Type label
    type_labels = {
        "price_move": "Price Alert",
        "signal_change": "Signal Alert",
        "regime_shift": "Regime Alert",
    }
    type_label = type_labels.get(cond_type, "Watch Alert")

    # Condition summary (from full condition if available)
    summary = ""
    if condition:
        summary = condition.get("summary", "")
        source = condition.get("source", "")
        if source == "discovery.significant_changes":
            summary = f"[auto-watch] {summary}"

    lines = [
        f"*{type_label}: {asset}*",
        "",
        reason,
    ]
    if summary:
        lines.append(f"_{summary}_")

    lines.append("")
    lines.append(f"ID: `{detail.get('conditionId', '?')}`")

    return "\n".join(lines)


async def send_triggered_notifications(
    triggered_ids: list[str],
    trigger_details: list[dict[str, Any]],
    conditions: list[dict[str, Any]] | None = None,
) -> int:
    """Send Telegram notifications for newly triggered watch conditions.

    Args:
        triggered_ids: List of condition IDs that just triggered.
        trigger_details: List of {conditionId, type, asset, reason} dicts.
        conditions: Full condition list (for summary lookup). Optional.

    Returns:
        Number of messages successfully sent.
    """
    token, chat_id = _get_config()
    if not token or not chat_id:
        logger.debug("Telegram notify: not configured (missing token or chat_id)")
        return 0

    if not triggered_ids:
        return 0

    # Dedup: skip already-sent IDs
    sent_ids = _load_sent_ids()
    new_ids = [cid for cid in triggered_ids if cid not in sent_ids]
    if not new_ids:
        logger.debug("Telegram notify: all %d triggers already sent", len(triggered_ids))
        return 0

    # Build condition lookup
    cond_map: dict[str, dict] = {}
    if conditions:
        for c in conditions:
            cond_map[c.get("conditionId", "")] = c

    # Build detail lookup
    detail_map: dict[str, dict] = {}
    for d in trigger_details:
        detail_map[d.get("conditionId", "")] = d

    sent_count = 0

    async with httpx.AsyncClient() as client:
        for cid in new_ids:
            detail = detail_map.get(cid)
            if not detail:
                continue

            condition = cond_map.get(cid)
            text = _format_message(detail, condition)

            try:
                r = await client.post(
                    f"{TELEGRAM_API}/bot{token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": "Markdown",
                        "disable_web_page_preview": True,
                    },
                    timeout=SEND_TIMEOUT,
                )
                resp = r.json()
                if r.status_code == 200 and resp.get("ok"):
                    sent_ids.add(cid)
                    sent_count += 1
                    logger.info("Telegram notify: sent %s (%s %s)", cid, detail.get("asset"), detail.get("type"))
                elif resp.get("parameters", {}).get("migrate_to_chat_id"):
                    # Chat upgraded to supergroup — retry with new ID
                    new_chat = str(resp["parameters"]["migrate_to_chat_id"])
                    logger.info("Telegram notify: chat migrated to %s, retrying", new_chat)
                    r2 = await client.post(
                        f"{TELEGRAM_API}/bot{token}/sendMessage",
                        json={"chat_id": new_chat, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True},
                        timeout=SEND_TIMEOUT,
                    )
                    if r2.status_code == 200 and r2.json().get("ok"):
                        sent_ids.add(cid)
                        sent_count += 1
                        logger.info("Telegram notify: sent %s via migrated chat", cid)
                    else:
                        logger.warning("Telegram notify: failed %s after migration — %s", cid, r2.text[:200])
                else:
                    logger.warning("Telegram notify: failed %s — %d %s", cid, r.status_code, r.text[:200])
            except Exception as e:
                logger.warning("Telegram notify: error sending %s — %s", cid, e)

    # Persist sent IDs
    _save_sent_ids(sent_ids)

    if sent_count > 0:
        logger.info("Telegram notify: %d/%d messages sent", sent_count, len(new_ids))

    return sent_count
