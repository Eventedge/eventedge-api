"""Snapshot reader + extractors.

Payload shapes are based on EE-API-004 discovery against edge_dataset_registry.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Tuple

from .db import get_conn

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DB reader
# ---------------------------------------------------------------------------

def get_snapshot(dataset_key: str) -> Optional[Dict[str, Any]]:
    """Return {"payload": dict, "updated_at": datetime} or None."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT payload, updated_at FROM edge_dataset_registry WHERE dataset_key = %s",
                    (dataset_key,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                payload_raw, updated_at = row
                payload = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
                return {"payload": payload, "updated_at": updated_at}
    except Exception:
        logger.warning("get_snapshot failed for key=%s", dataset_key, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _num(x: Any) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fmt_usd(n: Optional[float]) -> str:
    if n is None:
        return "—"
    if abs(n) >= 1_000_000_000:
        return f"${n / 1_000_000_000:.1f}B"
    if abs(n) >= 1_000_000:
        return f"${n / 1_000_000:.1f}M"
    if abs(n) >= 1_000:
        return f"${n:,.0f}"
    return f"${n:,.2f}"


def fmt_pct(p: Optional[float], digits: int = 2) -> str:
    if p is None:
        return "—"
    sign = "+" if p > 0 else ""
    return f"{sign}{p:.{digits}f}%"


# ---------------------------------------------------------------------------
# Extractors — matched to ACTUAL payload shapes from EE-API-004 discovery
# ---------------------------------------------------------------------------

def extract_price(payload: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    """coingecko:price_simple:usd:{coin_id}
    Shape: {"data": {"price": 68819, "change_24h": -2.06}, ...}
    """
    data = payload.get("data", {})
    return _num(data.get("price")), _num(data.get("change_24h"))


def extract_global(payload: Dict[str, Any]) -> Dict[str, Any]:
    """coingecko:global
    Shape: {"data": {"btc_dominance": 56.7, "eth_dominance": 9.8,
            "total_volume_usd": 103B, "total_market_cap_usd": 2.4T, ...}}
    """
    data = payload.get("data", {})
    return {
        "btc_dominance": _num(data.get("btc_dominance")),
        "eth_dominance": _num(data.get("eth_dominance")),
        "total_mcap_usd": _num(data.get("total_market_cap_usd")),
        "total_vol_usd": _num(data.get("total_volume_usd")),
        "mcap_change_24h": _num(data.get("market_cap_change_24h_pct")),
    }


def extract_funding(payload: Dict[str, Any]) -> Optional[float]:
    """coinglass:oi_weighted_funding:{SYM}
    Shape: {"data": {"rate": 0.001178, "symbol": "BTC", "prev_rate": 0.003825}, ...}
    Rate is a fraction (0.001178 = 0.1178%).
    """
    rate = _num(payload.get("data", {}).get("rate"))
    if rate is not None:
        return rate * 100  # convert to percentage
    return None


def extract_oi(payload: Dict[str, Any]) -> Dict[str, Any]:
    """coinglass:open_interest:{SYM}
    Shape: {"data": {"oi_usd": 43.8B, "oi_change_24h": -1.91, "oi_billion": 43.8}, ...}
    """
    data = payload.get("data", {})
    return {
        "oi_usd": _num(data.get("oi_usd")),
        "oi_change_24h": _num(data.get("oi_change_24h")),
    }


def extract_liquidations(payload: Dict[str, Any]) -> Dict[str, Any]:
    """coinglass:liquidations:{SYM}
    Shape: {"raw": [{"exchange": "All", "liquidation_usd": 63M,
            "longLiquidation_usd": 51M, "shortLiquidation_usd": 11M}, ...]}
    First entry with exchange="All" has totals.
    """
    raw = payload.get("raw", [])
    # Find the "All" exchange row
    all_row = None
    for r in raw:
        if isinstance(r, dict) and r.get("exchange") == "All":
            all_row = r
            break
    if all_row is None and raw:
        all_row = raw[0]  # fallback to first row

    if all_row is None:
        return {}

    total = _num(all_row.get("liquidation_usd"))
    long_usd = _num(all_row.get("longLiquidation_usd"))
    short_usd = _num(all_row.get("shortLiquidation_usd"))

    long_pct = (long_usd / total * 100) if (total and long_usd is not None) else None
    short_pct = (short_usd / total * 100) if (total and short_usd is not None) else None

    return {
        "total_usd": total,
        "long_usd": long_usd,
        "short_usd": short_usd,
        "long_pct": long_pct,
        "short_pct": short_pct,
    }
