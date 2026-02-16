"""Regime classifier — heuristic buckets from live snapshots.

Outputs an explainable regime label + 4 axes + 3 drivers.
No formulas disclosed — only interpretive buckets.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .snapshots import (
    extract_funding,
    extract_liquidations,
    extract_oi,
    extract_price,
    fmt_pct,
    fmt_usd,
    get_snapshot,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fg_value() -> Optional[int]:
    """Read current Fear & Greed value from DB snapshot."""
    ds = get_snapshot("altme:fear_greed")
    if not ds:
        return None
    payload = ds.get("payload") or {}
    data = payload.get("data") or []
    if not data or not isinstance(data[0], dict):
        return None
    try:
        return int(data[0].get("value"))
    except (TypeError, ValueError):
        return None


def _bucket(
    x: Optional[float], lo: float, hi: float,
    labels: Tuple[str, str, str] = ("low", "neutral", "high"),
) -> str:
    if x is None:
        return labels[1]
    if x <= lo:
        return labels[0]
    if x >= hi:
        return labels[2]
    return labels[1]


def _confidence(parts_ok: int) -> str:
    if parts_ok >= 4:
        return "high"
    if parts_ok >= 2:
        return "medium"
    return "low"


def _regime_label(
    trend_bkt: str, vol_bkt: str, lev_bkt: str, liq_bkt: str,
    fg: Optional[int],
) -> str:
    """Coarse regime mapping — buckets only, no formulas."""
    # Risk-Off: negative trend + high leverage/fragility or extreme fear
    if trend_bkt == "down" and (
        lev_bkt == "high" or liq_bkt == "tight" or (fg is not None and fg <= 25)
    ):
        return "Risk-Off"
    # Trend: strong directional move + non-choppy volatility
    if trend_bkt in ("up", "down") and vol_bkt != "chop":
        return "Trend"
    # Risk-On: up trend + not extreme fear + leverage not crowded
    if trend_bkt == "up" and lev_bkt != "high" and not (fg is not None and fg <= 25):
        return "Risk-On"
    # Default: Chop / range
    return "Chop"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_regime() -> Dict[str, Any]:
    # ---- fetch snapshots (BTC) ----
    price_snap = get_snapshot("coingecko:price_simple:usd:bitcoin")
    oi_snap = get_snapshot("coinglass:open_interest:BTC")
    funding_snap = get_snapshot("coinglass:oi_weighted_funding:BTC")
    liq_snap = get_snapshot("coinglass:liquidations:BTC")

    parts_ok = 0

    # ---- extract signals ----
    price, chg24 = extract_price(price_snap["payload"]) if price_snap else (None, None)
    if chg24 is not None:
        parts_ok += 1

    # extract_funding returns percentage float (rate * 100)
    funding_pct = extract_funding(funding_snap["payload"]) if funding_snap else None
    if funding_pct is not None:
        parts_ok += 1

    oi = extract_oi(oi_snap["payload"]) if oi_snap else {}
    oi_chg24 = oi.get("oi_change_24h")
    if oi_chg24 is not None:
        parts_ok += 1

    liq = extract_liquidations(liq_snap["payload"]) if liq_snap else {}
    liq_total = liq.get("total_usd")
    liq_long_pct = liq.get("long_pct")
    if liq_total is not None:
        parts_ok += 1

    fg = _fg_value()

    # ---- axes (simple buckets) ----

    # Trend: based on 24h price change
    if chg24 is None:
        trend_label, trend_bkt = "\u2014", "flat"
    elif chg24 >= 1.0:
        trend_label, trend_bkt = "Up", "up"
    elif chg24 <= -1.0:
        trend_label, trend_bkt = "Down", "down"
    else:
        trend_label, trend_bkt = "Flat", "flat"

    # Volatility: liquidation total as "shockiness" proxy
    vol_bkt = _bucket(liq_total, 25_000_000.0, 120_000_000.0, ("calm", "chop", "shock"))
    vol_label = {"calm": "Calm", "chop": "Chop", "shock": "Shock"}.get(vol_bkt, "\u2014")

    # Leverage: funding as crowding hint
    lev_bkt = _bucket(funding_pct, -0.02, 0.10, ("low", "neutral", "high"))
    lev_label = {"low": "Light", "neutral": "Normal", "high": "Crowded"}.get(lev_bkt, "\u2014")

    # Liquidity: liquidation skew as fragility proxy
    liq_bkt = "normal"
    if liq_long_pct is not None:
        if liq_long_pct >= 70:
            liq_bkt = "tight"
        elif liq_long_pct <= 40:
            liq_bkt = "loose"
    liq_label = {"loose": "Loose", "normal": "Normal", "tight": "Tight"}.get(liq_bkt, "\u2014")

    # ---- regime + drivers ----
    label = _regime_label(trend_bkt, vol_bkt, lev_bkt, liq_bkt, fg)
    conf = _confidence(parts_ok)

    drivers: List[str] = []
    if chg24 is not None and price is not None:
        drivers.append(f"BTC {fmt_usd(price)} \u2022 {fmt_pct(chg24)} 24h (trend axis)")
    if funding_pct is not None:
        drivers.append(f"Funding {fmt_pct(funding_pct, 3)} (crowding proxy)")
    if liq_total is not None and liq_long_pct is not None:
        drivers.append(
            f"Liqs {fmt_usd(liq_total)} \u2022 {fmt_pct(liq_long_pct, 0, signed=False)} long (fragility proxy)"
        )
    if fg is not None:
        drivers.append(f"Fear & Greed {fg} (sentiment context)")
    while len(drivers) < 3:
        drivers.append("\u2014")
    drivers = drivers[:3]

    return {
        "ts": None,  # filled by caller
        "version": "v0.2-live",
        "regime": {"label": label, "confidence": conf, "since": None},
        "axes": [
            {"key": "trend", "label": "Trend", "value": trend_label},
            {"key": "volatility", "label": "Volatility", "value": vol_label},
            {"key": "leverage", "label": "Leverage", "value": lev_label},
            {"key": "liquidity", "label": "Liquidity", "value": liq_label},
        ],
        "drivers": drivers,
        "disclaimer": (
            "Heuristic regime classifier derived from live snapshots. "
            "Outputs are buckets and drivers (no model disclosure)."
        ),
    }
