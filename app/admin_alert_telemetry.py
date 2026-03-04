"""P5-ALERTS-007B: Router alert delivery telemetry reader.

Reads router_alert_delivery.jsonl and aggregates stats per day/asset/horizon.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DELIVERY_JSONL = Path(
    os.getenv("ROUTER_ALERT_DELIVERY_JSONL",
              "/home/eventedge/alerts/router_alert_delivery.jsonl")
)
DELIVERY_LAST = Path(
    os.getenv("ROUTER_ALERT_DELIVERY_LAST",
              "/home/eventedge/alerts/router_alert_delivery_last.json")
)


def build_delivery_telemetry(day: str | None = None) -> dict[str, Any]:
    """Aggregate delivery stats for a given UTC day (YYYY-MM-DD).

    Returns grouped by asset:horizon with totals.
    """
    now = datetime.now(timezone.utc)
    if not day:
        day = now.strftime("%Y-%m-%d")

    # Parse target date range
    try:
        day_start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return {"ok": False, "generated_at": now.isoformat(), "error": f"bad date: {day}"}

    ts_start = int(day_start.timestamp())
    ts_end = ts_start + 86400

    # Read JSONL lines for the target day
    buckets: dict[str, dict] = {}  # "BTC:24h" -> aggregated
    total_lines = 0

    if DELIVERY_JSONL.exists():
        try:
            with open(DELIVERY_JSONL) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = entry.get("ts", 0)
                    if ts < ts_start or ts >= ts_end:
                        continue
                    total_lines += 1
                    key = f"{entry.get('asset', '?')}:{entry.get('horizon', '?')}"
                    if key not in buckets:
                        buckets[key] = {
                            "asset": entry.get("asset", "?"),
                            "horizon": entry.get("horizon", "?"),
                            "ticks": 0,
                            "triggers": 0,
                            "attempted": 0,
                            "sent": 0,
                            "suppressed": 0,
                            "rate_limited": 0,
                            "failed": 0,
                        }
                    b = buckets[key]
                    b["ticks"] += 1
                    b["triggers"] += entry.get("triggers", 0)
                    b["attempted"] += entry.get("attempted", 0)
                    b["sent"] += entry.get("sent", 0)
                    b["suppressed"] += entry.get("suppressed", 0)
                    b["rate_limited"] += entry.get("rate_limited", 0)
                    b["failed"] += entry.get("failed", 0)
        except OSError:
            pass

    # Add failure rate
    rows = []
    for b in buckets.values():
        attempted = b["attempted"]
        b["failure_pct"] = round(b["failed"] / attempted * 100, 1) if attempted > 0 else 0.0
        rows.append(b)

    # Last status
    last_status = {}
    if DELIVERY_LAST.exists():
        try:
            last_status = json.loads(DELIVERY_LAST.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    return {
        "ok": True,
        "generated_at": now.isoformat(),
        "day": day,
        "total_lines": total_lines,
        "rows": rows,
        "last_status": last_status,
    }
